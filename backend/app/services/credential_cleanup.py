"""Forget what deleted accounts left in Redis.

When a workspace is closed (`services/workspace_closure`, decisions §306) its
user rows are deleted, which already makes every token and link they held inert:
each resolves to a user that no longer exists. What stays behind until its TTL
is the per-user bookkeeping — the live-session set and its metadata hash (IP,
device label), a half-finished TOTP enrollment, an email OTP, passkey
challenges, and any unspent forgot-password / set-password link. This removes
it, so a deleted account leaves nothing personal behind in the cache.

The tenant's suppliers left links too (`portal:pwreset:`): a closure needs
every portal account already deleted, but a link minted before that deletion
lives on until its TTL. :func:`forget_portal_links` removes them by the slug
each one is bound to.

Every live session is blocklisted first (`app.redis.revoke_all_sessions`), so a
token still in someone's browser is refused by the blocklist as well as by the
missing user row.
"""

from __future__ import annotations

import logging
import uuid

from app.redis import _session_key, _session_meta_key, get_redis, revoke_all_sessions
from app.services import password_reset
from app.services.mfa import EMAIL_OTP_PREFIX, PENDING_ENROLL_PREFIX
from app.services.session_management import blocklist_ttl_for

logger = logging.getLogger(__name__)

#: Passkey ceremony keys, per user. The step-up slot is per operation, so it is
#: matched by pattern rather than built.
_WEBAUTHN_KEYS = ("webauthn:reg_challenge:{uid}", "webauthn:auth_challenge:{uid}")
_WEBAUTHN_STEP_UP_PATTERN = "webauthn:stepup_challenge:*:{uid}"


async def forget_users(user_ids: list[uuid.UUID]) -> int:
    """Revoke and delete every per-user key of ``user_ids``. Returns keys removed.

    Reset tokens are stored under a keyed digest of the token, not the user, so
    they are found by scanning the reset prefix and comparing the stored user
    id — bounded by the number of outstanding links platform-wide, which is
    small, and run only on a closure.
    """
    if not user_ids:
        return 0
    r = await get_redis()
    removed = 0
    wanted = {str(uid) for uid in user_ids}
    for uid in user_ids:
        ttl = await blocklist_ttl_for(uid)
        await revoke_all_sessions(uid, ttl)
        keys = [
            _session_key(uid),
            _session_meta_key(uid),
            f"{PENDING_ENROLL_PREFIX}{uid}",
            f"{EMAIL_OTP_PREFIX}{uid}",
            *(k.format(uid=uid) for k in _WEBAUTHN_KEYS),
        ]
        async for key in r.scan_iter(match=_WEBAUTHN_STEP_UP_PATTERN.format(uid=uid)):
            keys.append(key)
        for key in keys:
            removed += await r.delete(key)

    async for key in r.scan_iter(match=f"{password_reset.PASSWORD_RESET_PREFIX}*"):
        if password_reset.is_generation_key(key):
            continue
        value = await r.get(key)
        if password_reset.stored_owner(value) in wanted:
            removed += await r.delete(key)
    # The per-user generation counters go too: they name the deleted account.
    for uid in user_ids:
        removed += await r.delete(password_reset.generation_key(uid))
    logger.info("[credential-cleanup] cleared %d key(s) for %d user(s)", removed, len(user_ids))
    return removed


async def forget_portal_links(slug: str) -> int:
    """Delete every supplier-portal link bound to tenant ``slug``, and the
    generation counter of each supplier those links name. Returns keys removed.

    Portal tokens are stored under a keyed digest of the token, with the value
    ``<slug>:<vendor_user_id>#<generation>``, so they are found by scanning the
    portal prefix and comparing the stored slug — the same bounded scan
    :func:`forget_users` makes. A counter is keyed by the supplier id alone, so
    only a link can tie it to this tenant: one whose links were all redeemed or
    expired is left to its own TTL. It holds a number under a random id whose
    row is gone, and names nobody.

    Run at closure, before the slug can be taken again (its cooldown,
    `utils/slug.ensure_slug_not_held`), so every link bound to it is this
    tenant's.
    """
    r = await get_redis()
    removed = 0
    suppliers: set[str] = set()
    async for key in r.scan_iter(match=f"{password_reset.PORTAL_PASSWORD_RESET_PREFIX}*"):
        if password_reset.is_portal_generation_key(key):
            continue
        owner = password_reset.portal_stored_owner(await r.get(key))
        if owner is None or owner[0] != slug:
            continue
        removed += await r.delete(key)
        suppliers.add(owner[1])
    for vendor_user_id in suppliers:
        removed += await r.delete(password_reset.portal_generation_key(vendor_user_id))
    logger.info(
        "[credential-cleanup] cleared %d portal key(s) for %d supplier(s)",
        removed,
        len(suppliers),
    )
    return removed
