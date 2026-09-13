"""Re-hash a just-verified credential that is still stored under an older scheme.

`pwd_context` verifies three schemes and writes exactly one (`bcrypt_sha256`
v2). `needs_update` names the gap between them; this module is the only thing
that closes it, and it has to run here because **the only moment a stored
password's plaintext exists is the instant a login has just verified it**.
There is no offline migration for a password column, and asking every account
to reset is the user-visible cost `docs/decisions.md` §151 was written to avoid.
Until this existed, a user who had not changed their password since c6a91396
kept authenticating against a raw `$2b$` hash — bcrypt's 72-byte truncation and
all — for as long as they never reset.

**One call per login surface, immediately after `verify_password` returns
True**: `api/auth.py::login` (control-plane `User`) and
`api/portal_auth.py::portal_login` (tenant-scoped `VendorUser`). It runs BEFORE
either handler's MFA branch, which returns a challenge instead of a token: the
password is already proven at that point, and deferring the upgrade until a
second factor completes would skip it for exactly the accounts that have one.

Four properties, each a decision rather than an implementation detail
(`docs/decisions.md` §165 and §166):

* **A hash we cannot classify is left alone.** `needs_update` is true for an
  unrecognised string too — correctly, since "replace it" is the right answer
  for corruption — but *this* is the one place that would act on it, and the
  action would be to write a working credential over a row that had none. In
  production the case is unreachable (a string `identify` cannot name is a
  string `verify` cannot match, so the login already failed), which is exactly
  why the guard costs nothing and why omitting it would be a silent hazard.
* **It never fails a login.** A credential-store maintenance step that turns a
  valid password into a 401 — or a 500 — is strictly worse than the stale hash
  it was trying to replace. Every failure is swallowed, logged without the
  secret, and the sign-in continues on the hash that already verified. Two
  things make that true rather than intended: the write runs inside a SAVEPOINT
  (a statement the server rejects poisons its transaction, and the handler's own
  commit would have raised afterwards), and the identity used in the log lines
  is read into locals up front (a rolled-back transaction expires the loaded
  instance, and the next attribute read on an expired instance is a *synchronous*
  lazy SELECT — `MissingGreenlet` from a coroutine, i.e. the error report
  becoming the outage).
* **The write is a compare-and-swap on the hash we verified**, not an ORM
  assignment. The handler holds no row lock across its ~400 ms of bcrypt, so a
  password change committed inside that window would otherwise be overwritten
  by a re-hash of the *old* plaintext — resurrecting the credential its owner
  had just retired.
* **Nothing is retained and nothing is logged.** The plaintext is a parameter,
  the new digest a local; neither outlives the call, and the log line carries an
  account id and two scheme names, never a secret, a digest, or an email.

No audit row: a status change gets one, and this is not one. The credential is
unchanged — same secret, same owner, same validity — only its storage encoding
moved, and the sign-in that triggered it is already on the trail as
`auth.login.success` / `portal.login.success` in the same request.
"""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.user import User
from app.models.vendor_user import VendorUser
from app.utils.passwords import hash_password, pwd_context

logger = logging.getLogger(__name__)

#: The two account models that carry a password. Both expose `id` and
#: `hashed_password`, which is all this module touches.
Credential = User | VendorUser


async def upgrade_password_hash(db: AsyncSession, account: Credential, password: str) -> bool:
    """Re-hash `password` onto `account` if its stored hash is on a scheme we
    no longer write. Returns True only when a row was actually rewritten.

    `password` MUST have just verified against `account.hashed_password`. This
    function deliberately does not re-verify — a second bcrypt on the login
    path to re-establish what the caller already knows — so calling it with an
    unverified secret would overwrite the credential with whatever was
    submitted. The two call sites are the two login handlers, one line below
    their `verify_password`.

    **It commits `db`**, so call it before the handler stages any other write.
    That is deliberate on both counts: the upgrade is independent of whether
    the sign-in goes on to succeed (a Redis blip in `register_session` should
    not undo it), and committing immediately means the row lock it takes is not
    held across the audit dispatch that follows — the rule
    `services/post_commit` exists to enforce elsewhere.
    """
    stored = account.hashed_password
    if not stored:
        return False
    scheme = pwd_context.identify(stored)
    # Two conditions, and the first is not redundant: `needs_update` is also
    # true for a string `identify` returns None for, and rewriting THAT would
    # mint a usable credential where the column held unusable bytes.
    if scheme is None or not pwd_context.needs_update(stored):
        return False

    model = type(account)  # the mapped class — `User` or `VendorUser`
    # Read the identity NOW, into locals. Every log line below uses these
    # rather than `account.<attr>`, because a rolled-back transaction expires
    # the loaded instance and the next attribute read on an expired instance is
    # a *synchronous* lazy SELECT — which from a coroutine raises
    # `MissingGreenlet`. Logging the failure would then be the thing that turned
    # a survivable hiccup into a 500 on a correct password.
    who, account_id = model.__name__, account.id

    try:
        fresh = await hash_password(password)
    except (ValueError, RuntimeError) as exc:
        # `hash` refuses a secret over MAX_SECRET_BYTES, and that ceiling is
        # reachable HERE where it is nowhere else: `LoginRequest.password` has
        # no maximum, and a legacy `$2b$` hash matches on its first 72 bytes
        # alone — so a 5 KB password can legitimately sign in and then be too
        # long to re-hash. The login stands on the hash that verified. Only the
        # exception's type is logged: the message is ours and carries no secret,
        # but interpolating an exception is how a secret eventually reaches a
        # log line (`.claude/hooks/security-patterns.sh` rule `exception-in-log`).
        logger.warning(
            "password hash upgrade skipped for %s %s (%s): the verified secret "
            "could not be re-hashed",
            who,
            account_id,
            type(exc).__name__,
        )
        return False

    stmt = (
        update(model)
        .where(model.id == account_id, model.hashed_password == stored)
        .values(hashed_password=fresh)
        # We synchronise the loaded instance ourselves below, so the ORM does
        # not need to evaluate the criteria against the identity map (or fall
        # back to a second SELECT to do it).
        .execution_options(synchronize_session=False)
    )
    try:
        # A SAVEPOINT, not the caller's transaction directly. A statement that
        # the server rejects poisons whatever transaction it ran in, so without
        # this the handler's own commit would raise afterwards and the
        # maintenance write would have become a 500 on a correct password. A
        # nested rollback unwinds only the savepoint — nothing was flushed
        # inside it, so no loaded object is expired and the login carries on
        # against the hash that already verified.
        async with db.begin_nested():
            result = await db.execute(stmt)
        await db.commit()
    except SQLAlchemyError as exc:
        logger.warning(
            "password hash upgrade failed for %s %s (%s): the sign-in stands",
            who,
            account_id,
            type(exc).__name__,
        )
        return False

    if not result.rowcount:
        # The column changed between the verify and here — a password change,
        # or another concurrent login's own upgrade. Whatever is there now is
        # newer than what we hashed, so leaving it is the only correct answer.
        logger.info(
            "password hash upgrade superseded for %s %s: the stored hash changed mid-login",
            who,
            account_id,
        )
        return False

    # Keep the loaded instance in step with its row. `set_committed_value`
    # rather than a plain assignment, which would mark the attribute dirty and
    # let the next flush re-emit the write WITHOUT the compare-and-swap guard.
    set_committed_value(account, "hashed_password", fresh)
    logger.info(
        "upgraded the stored password hash for %s %s: %s -> %s",
        who,
        account_id,
        scheme,
        pwd_context.scheme,
    )
    return True
