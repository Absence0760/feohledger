"""Re-seal every stored ERP credential under the ACTIVE credential key.

The rotation step for ``FEOH_CREDENTIAL_ENCRYPTION_KEYS``
(``docs/secrets-rotation.md`` § Credential encryption keyring): after a new key
is prepended to the keyring and deployed, this rewrites every
``organizations.settings.erp`` secret and OAuth token that is still under an
older key id (or, belt and braces, still in plaintext) so the old key can be
dropped. Values already under the active key are left byte-for-byte unchanged,
so a re-run is a no-op.

Each organization is rewritten under its own ``SELECT … FOR UPDATE`` and
committed on its own: the OAuth refresher's compare-and-swap compares the
DECRYPTED refresh token, so a concurrent refresh and a re-seal cannot spend or
overwrite each other's token.

Usage (from ``backend/``, with the deployment's keyring in the environment):

    python scripts/reencrypt_erp_credentials.py --dry-run   # count what would change
    python scripts/reencrypt_erp_credentials.py

Exit codes: 0 done (or nothing to do), 1 a stored value could not be decrypted
under any configured key (nothing for that organization was changed; the
others were), 2 no keyring configured.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# See the identical preamble in create_tenant.py for why (a worktree reusing the
# primary checkout's venv otherwise resolves `app` to the WRONG tree, silently).
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.database import control_engine, control_session_factory
from app.models.organization import Organization
from app.services.erp_credentials import reencrypt_erp_config
from app.utils.credential_crypto import (
    CredentialCryptoError,
    CredentialKeyMissingError,
    active_key_id,
)


async def run(*, dry_run: bool) -> int:
    try:
        active_key_id()
    except CredentialKeyMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async with control_session_factory() as session:
        org_ids = (await session.execute(select(Organization.id))).scalars().all()

    changed = failed = 0
    for org_id in org_ids:
        async with control_session_factory() as session:
            org = (
                await session.execute(
                    select(Organization).where(Organization.id == org_id).with_for_update()
                )
            ).scalar_one_or_none()
            erp = (org.settings or {}).get("erp") if org is not None else None
            if not isinstance(erp, dict):
                await session.rollback()
                continue
            try:
                resealed = reencrypt_erp_config(erp)
            except CredentialCryptoError as exc:
                # Fixed text naming the field and the key id, never a value.
                print(f"{org.slug}: {exc}", file=sys.stderr)
                failed += 1
                await session.rollback()
                continue
            if resealed == erp:
                await session.rollback()
                continue
            changed += 1
            if dry_run:
                await session.rollback()
                continue
            settings = dict(org.settings or {})
            settings["erp"] = resealed
            org.settings = settings
            flag_modified(org, "settings")
            await session.commit()

    verb = "would re-seal" if dry_run else "re-sealed"
    print(f"{verb} ERP credentials for {changed} organization(s); {failed} unreadable.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Count, change nothing.")
    args = parser.parse_args()

    async def _main() -> int:
        try:
            return await run(dry_run=args.dry_run)
        finally:
            await control_engine.dispose()

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
