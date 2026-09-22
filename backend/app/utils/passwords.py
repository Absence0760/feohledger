"""Password generation, complexity checks, and the shared hash context.

The hash context lives here (not duplicated in every router that hashes
passwords) so we get one consistent algorithm choice across the codebase. The
scheme is **`bcrypt_sha256`**: the password is pre-hashed with HMAC-SHA256
before bcrypt sees it, which side-steps bcrypt's 72-byte truncation — a user
who picks a 100-char password is fully protected by the suffix, where raw
bcrypt would let any two passwords sharing the first 72 bytes verify against
each other's hash. Legacy `$2b$...` hashes (written before the upgrade in
c6a91396) still verify, so nobody is locked out; `needs_update` reports which
stored hashes are on an older scheme or below the configured bcrypt cost, and
`services/credential_upgrade` acts on it — the two login handlers re-hash such
a row onto the current scheme and cost the next time its owner signs in
successfully, which is the only moment the plaintext needed to do so exists.

**We implement `bcrypt_sha256` directly rather than through passlib.** passlib
owned this module until 2026-09 and pinned us to bcrypt 4.0.1: it reads
`bcrypt.__about__.__version__` (deleted in bcrypt 5.0) and its backend probe
hashes a >72-byte secret (a hard `ValueError` from bcrypt 4.1 on), so any bcrypt
newer than 4.0 broke at *import* time. passlib has been 1.7.4 since 2020, so
waiting for a fix was not a plan with a date on it. The digest is byte-for-byte
what passlib emitted — `tests/test_bcrypt_sha256_compat.py` pins it against
hashes passlib itself produced — so every stored credential keeps verifying.
The format is passlib's, written out here because we own it now:

    $bcrypt-sha256$v=2,t=2b,r=12$<22-char salt>$<31-char checksum>
    v=2   HMAC-SHA256(key=salt_ascii, msg=password_utf8) -> base64 -> bcrypt
    v=1   sha256(password_utf8) -> base64 -> bcrypt   (pre-1.7.3, read-only)

v1 is accepted on verify and never written: keying the pre-hash off the salt is
what stops a stolen `sha256(password)` lookup table being replayed against our
column. `t` is the bcrypt variant (`2b`; v1 hashes may carry `2a`), `r` the
bcrypt cost, and the 22-character salt enters the HMAC as its *encoded* ASCII
text, not as the raw bytes it decodes to.

**bcrypt is deliberately slow, so it never runs on the event loop.** A single
`pwd_context.verify` is ~200 ms of pure CPU at the configured cost — that is the
point of the algorithm, and it is also why calling it inline from a login
handler pins the whole worker for 200 ms while it serves nothing else. Use the
awaitable wrappers `verify_password` / `hash_password` / `dummy_verify`, which
run the work in a thread; `tests/test_password_hashing_offloaded.py` is the
drift guard. The timing-equalisation guarantee is unaffected: the real and the
dummy verification pay the same thread hop and the same bcrypt cost.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import string

import bcrypt

# Minimum constraints for a user-chosen password. The auto-generated
# temporary password (generate_temp_password) is constructed to satisfy
# these deterministically — see that function.
MIN_LENGTH = 12

#: bcrypt cost for newly written hashes. 12 is what passlib defaulted to and
#: what every hash in the column already carries, so this is a continuation
#: rather than a choice: lowering it silently weakens every password set after.
#: Raising it is safe and migrates the existing population on its own:
#: `needs_update` flags a stored hash below this cost, so each account is
#: re-hashed at the new cost on its owner's next successful login.
DEFAULT_ROUNDS = 12

#: Upper bound on a secret we will *write* a hash for, matching passlib's
#: `MAX_PASSWORD_SIZE`. Unreachable from any route — every password-setting
#: schema caps the field at 128 characters — so this is a programming-error
#: guard, not input validation.
#:
#: It deliberately does NOT apply to `verify`. Refusing an oversized secret
#: there would return in microseconds where a wrong password costs ~200 ms of
#: bcrypt, and `LoginRequest.password` has no maximum: an attacker could post a
#: 5 KB password and read "this account exists" off the fast refusal, which is
#: the enumeration oracle `dummy_verify` exists to close. (passlib leaked the
#: same fact more loudly — it raised, so the oversized case was a 500 against a
#: 401.) Verification cost therefore stays uniform in the hash, not the input.
MAX_SECRET_BYTES = 4096

#: What bcrypt itself consumes. Raw bcrypt ignores everything past this, and
#: from 4.1 on it raises instead of ignoring — see `_verify_legacy_bcrypt`.
_BCRYPT_SECRET_BYTES = 72

_PREFIX = "$bcrypt-sha256$"
_B64 = r"[./A-Za-z0-9]"  # bcrypt's base64 alphabet, used for the salt + checksum

# Current (v2) format. The writer does not zero-pad `r`, but a padded value
# decodes to the same cost, so it is accepted rather than called malformed.
_V2_RE = re.compile(
    rf"^\$bcrypt-sha256\$v=2,t=(?P<ident>2b),r=(?P<rounds>\d{{1,2}})"
    rf"\$(?P<salt>{_B64}{{22}})\$(?P<checksum>{_B64}{{31}})$"
)
# Pre-1.7.3 (v1) format: plain sha256 pre-hash, and `2a` was still allowed.
_V1_RE = re.compile(
    rf"^\$bcrypt-sha256\$(?P<ident>2[ab]),(?P<rounds>\d{{1,2}})"
    rf"\$(?P<salt>{_B64}{{22}})\$(?P<checksum>{_B64}{{31}})$"
)
# A raw bcrypt hash, the scheme this codebase wrote before c6a91396. Matched
# BEFORE handing anything to `bcrypt.checkpw`, which is not merely strict about
# the shape: a too-short salt panics out of its Rust extension as a
# `BaseException` no `except ValueError` can catch. `2x` is excluded
# deliberately — crypt_blowfish's buggy 8-bit variant, which passlib also
# refused and which nothing here ever wrote.
_BCRYPT_RE = re.compile(rf"^\$2[aby]\$\d{{2}}\${_B64}{{53}}$")

_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_DIGIT = re.compile(r"[0-9]")


def _prehash(secret: str, salt: str, version: int) -> bytes:
    """Reduce `secret` to the fixed-length key bcrypt actually hashes.

    base64 (44 bytes), not hex (64): bcrypt is documented not to mix entropy
    evenly past byte 55, so the key has to stay under that. The encoding also
    guarantees no NUL byte, which bcrypt would read as end-of-string.
    """
    raw = secret.encode("utf-8")
    if version == 1:
        digest = hashlib.sha256(raw).digest()
    else:
        digest = hmac.new(salt.encode("ascii"), raw, hashlib.sha256).digest()
    return base64.b64encode(digest)


def _bcrypt_checksum(key: bytes, ident: str, rounds: int, salt: str) -> str:
    """Run bcrypt over `key` with an explicit salt, returning just the digest."""
    config = f"${ident}${rounds:02d}${salt}".encode("ascii")
    result = bcrypt.hashpw(key, config)
    if not result.startswith(config) or len(result) != len(config) + 31:
        raise RuntimeError("bcrypt returned a hash in an unexpected shape")
    return result[-31:].decode("ascii")


class _BcryptSha256Context:
    """The codebase's single password hash context.

    Deliberately shaped like the `passlib.context.CryptContext` it replaced —
    `hash` / `verify` / `identify` / `needs_update` — because that is the API
    every call site, test and drift guard already speaks, and because the
    scheme-selection policy (which schemes verify, which one gets written)
    belongs in exactly one object. A second hash context anywhere is the defect
    `.claude/hooks/security-patterns.sh` rule `bcrypt-truncation` exists to
    catch.
    """

    #: The scheme new hashes are written with.
    scheme = "bcrypt_sha256"
    #: Schemes we still verify but never write — `needs_update` reports these
    #: (and, on the current scheme, any hash below the configured cost).
    deprecated_schemes = ("bcrypt_sha256_v1", "bcrypt")

    def __init__(self, rounds: int = DEFAULT_ROUNDS) -> None:
        self.rounds = rounds

    def hash(self, secret: str) -> str:
        """Hash `secret` as a v2 `bcrypt_sha256` string."""
        if len(secret.encode("utf-8")) > MAX_SECRET_BYTES:
            raise ValueError(f"password exceeds {MAX_SECRET_BYTES} bytes")
        # bcrypt's own salt generator: 16 random bytes in bcrypt's base64, so
        # the 22nd character's four unused bits are always zero — the property
        # passlib spent a `repair_unused` pass enforcing, and the one that keeps
        # independent bcrypt implementations agreeing on the digest.
        salt = bcrypt.gensalt(self.rounds, prefix=b"2b").decode("ascii")[-22:]
        checksum = _bcrypt_checksum(_prehash(secret, salt, 2), "2b", self.rounds, salt)
        return f"{_PREFIX}v=2,t=2b,r={self.rounds}${salt}${checksum}"

    def verify(self, secret: str, hashed: str) -> bool:
        """Check `secret` against `hashed`.

        Returns False — never raises — on an empty, malformed or
        unknown-scheme hash. Several call sites hand this whatever sits in the
        `hashed_password` column, including rows written by a scheme we no
        longer emit, and a raise there would be a 500 on the login path instead
        of a clean refusal.
        """
        if not isinstance(hashed, str) or not hashed:
            return False
        try:
            if hashed.startswith(_PREFIX):
                return self._verify_bcrypt_sha256(secret, hashed)
            if _BCRYPT_RE.match(hashed):
                return self._verify_legacy_bcrypt(secret, hashed)
        except ValueError:
            # bcrypt raises ValueError on a cost outside 4..31, which the shape
            # regex deliberately does not police — the cost is bcrypt's to judge.
            return False
        return False

    def _verify_bcrypt_sha256(self, secret: str, hashed: str) -> bool:
        match = _V2_RE.match(hashed)
        version = 2
        if match is None:
            match = _V1_RE.match(hashed)
            version = 1
        if match is None:
            return False
        salt = match.group("salt")
        key = _prehash(secret, salt, version)
        computed = _bcrypt_checksum(key, match.group("ident"), int(match.group("rounds")), salt)
        return hmac.compare_digest(computed, match.group("checksum"))

    def _verify_legacy_bcrypt(self, secret: str, hashed: str) -> bool:
        """Verify a raw `$2b$` hash from before the `bcrypt_sha256` upgrade.

        The truncation here is the whole reason the scheme was replaced, but it
        is also exactly what bcrypt 4.0 did internally, so reproducing it is
        what keeps a pre-upgrade password working: bcrypt 4.1+ raises on a
        >72-byte secret rather than ignoring the tail, which would lock those
        accounts out of their own credential. `checkpw` does its own
        constant-time comparison.
        """
        raw = secret.encode("utf-8")[:_BCRYPT_SECRET_BYTES]
        # Raw bcrypt reads a NUL as end-of-string, so a match on `"ab\0anything"`
        # proves only that `"ab"` matched. passlib refused NUL-bearing secrets
        # outright; keep refusing them rather than inherit the shortcut — but
        # refuse AFTER paying the same cost, because a fast refusal on a secret
        # the attacker chooses is an account-enumeration oracle (the fast answer
        # only comes back when the account exists). A NUL past byte 72 is
        # irrelevant: bcrypt never saw it when the hash was written either.
        return bcrypt.checkpw(raw, hashed.encode("ascii")) and b"\x00" not in raw

    def identify(self, hashed: str) -> str | None:
        """Name the scheme `hashed` was written with, or None if unrecognised."""
        if not isinstance(hashed, str) or not hashed:
            return None
        if hashed.startswith(_PREFIX):
            if _V2_RE.match(hashed):
                return "bcrypt_sha256"
            if _V1_RE.match(hashed):
                return "bcrypt_sha256_v1"
            return None
        if _BCRYPT_RE.match(hashed):
            return "bcrypt"
        return None

    def rounds_of(self, hashed: str) -> int | None:
        """The bcrypt cost `hashed` was written at, or None if unrecognised.

        Read from whichever format the hash is in — the `r=` of a v2 wrapper,
        the cost field of a v1 wrapper, or the `$NN$` of a raw bcrypt hash.
        """
        if not isinstance(hashed, str) or not hashed:
            return None
        match = _V2_RE.match(hashed) or _V1_RE.match(hashed)
        if match is not None:
            return int(match.group("rounds"))
        if _BCRYPT_RE.match(hashed):
            return int(hashed[4:6])
        return None

    def needs_update(self, hashed: str) -> bool:
        """True when `hashed` is not what `hash` would write today.

        Two ways to fall behind, and both are answered by re-hashing: a scheme
        we no longer write, **or** the current scheme at a bcrypt cost below
        `self.rounds`. The second is what makes raising `DEFAULT_ROUNDS` mean
        anything for credentials that already exist — `identify` names only the
        scheme, so a scheme-only check would leave every stored row at the old
        cost for good, while new passwords quietly got the new one. A hash
        *above* the configured cost is left alone: it is stronger than what we
        would write, and lowering it would be a downgrade.

        Only meaningful after a successful `verify` — the answer for an
        unrecognised string is "replace it", but nothing can verify against one
        to get there, so in production the unrecognised case never reaches a
        caller. `services/credential_upgrade` is the one consumer and pairs this
        with an `identify(...) is not None` check anyway, because it is the one
        place that would act on the answer: rewriting a row whose contents we
        cannot name would mint a working credential where there was none.
        """
        if self.identify(hashed) != self.scheme:
            return True
        cost = self.rounds_of(hashed)
        # `identify` naming the current scheme means the v2 pattern matched, so
        # a cost is always present here; the None arm is unreachable, and it
        # answers "replace it" like every other string we cannot fully read.
        return cost is None or cost < self.rounds


pwd_context = _BcryptSha256Context()

# A bcrypt_sha256 hash of a fixed throwaway secret, computed once at import.
# Used to equalize login timing — see `dummy_verify`.
_DUMMY_HASH = pwd_context.hash("timing-equalizer-not-a-real-secret")


async def verify_password(password: str, hashed: str) -> bool:
    """Verify `password` against `hashed`, off the event loop.

    The single entry point for checking a credential. bcrypt is ~200 ms of CPU
    by design; run inline from a coroutine that is 200 ms in which the worker
    answers no other request, and `/auth/login` is the most concurrently-hit
    endpoint there is. The thread hop costs microseconds and gives the loop back.
    """
    return await asyncio.to_thread(pwd_context.verify, password, hashed)


async def hash_password(password: str) -> str:
    """Hash `password` with the shared context, off the event loop.

    Same reasoning as `verify_password` — hashing costs the same ~200 ms.
    """
    return await asyncio.to_thread(pwd_context.hash, password)


async def dummy_verify() -> None:
    """Run a throwaway password verification to match the wall-clock cost of a
    real `verify_password`.

    Login handlers must call this on the user-not-found branch: otherwise the
    not-found path returns ~200ms faster than a wrong-password path (which runs
    bcrypt), letting an attacker time the difference to enumerate which emails
    have accounts. The result is intentionally ignored.

    It goes through the same `asyncio.to_thread` hop as the real verification,
    so the two paths stay indistinguishable end to end.
    """
    await asyncio.to_thread(pwd_context.verify, "x", _DUMMY_HASH)


class PasswordError(ValueError):
    """Raised when a user-supplied password doesn't meet complexity rules."""


def generate_temp_password() -> str:
    """Generate a 16-char temporary password for a new admin user.

    Sent in the welcome email and must be changed on first login (see
    User.must_change_password). Constructed to ALWAYS satisfy
    validate_password_complexity — at least one uppercase letter, one lowercase
    letter, and one digit, length 16 (>= MIN_LENGTH) — so a new user's first
    login is never bounced by the complexity gate. (The previous
    `secrets.token_urlsafe(12)` relied on random-byte distribution and could
    emit a password with no digit or no case mix.) The alphabet is alphanumeric,
    a subset of the URL-safe set, so the password stays safe in the welcome URL.
    """
    alphabet = string.ascii_letters + string.digits
    # Seed one character of each required class so complexity is guaranteed,
    # fill the rest from the full alphanumeric alphabet, then shuffle so the
    # seeded positions aren't predictable.
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(alphabet) for _ in range(13)),
    ]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def validate_password_complexity(password: str) -> None:
    """Enforce minimum complexity for user-chosen passwords. Raises PasswordError."""
    if len(password) < MIN_LENGTH:
        raise PasswordError(f"Password must be at least {MIN_LENGTH} characters.")
    if not _UPPER.search(password):
        raise PasswordError("Password must contain an uppercase letter.")
    if not _LOWER.search(password):
        raise PasswordError("Password must contain a lowercase letter.")
    if not _DIGIT.search(password):
        raise PasswordError("Password must contain a digit.")
