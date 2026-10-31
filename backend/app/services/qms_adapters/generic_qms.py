"""Generic REST QMS adapter — skeleton.

Many QMS / LIMS platforms (and integration middleware like Merge.dev's
QMS category) expose a REST list endpoint that returns inspection records
as JSON. This adapter pins that intended shape but ships as a SKELETON:
it does NOT make a real HTTP call in any default path.

It FAILS CLOSED — without both a ``base_url`` and an ``api_key`` in
``Organization.settings.qms`` every method raises (``fetch_inspections``)
or returns False (``test_connection``), exactly like the
``financing_adapters/c2fo`` skeleton. There is no hardcoded credential
fallback (project invariant — secrets via sops + KMS, never a literal
default). A live integration fills in the HTTP body behind the
credential guard and maps the provider's JSON into
``QMSInspectionRecord``s.

``base_url`` is a per-org host (any tenant admin writes ``settings.qms``), so
the live call MUST go through ``url_safety.org_host_client`` — the up-front
SSRF check plus the pinned connect (decisions §339) — never a plain
``httpx.AsyncClient``. ``tests/test_url_safety.py`` holds this module in
``_ORG_HOST_NOT_YET_SENT`` and fails the day it builds an HTTP client, so the
module moves to ``_ORG_HOST_MODULES`` in the same change.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.services.qms_adapters.base import QMSInspectionRecord
from app.services.qms_adapters.dispatcher import register_qms_adapter

logger = logging.getLogger(__name__)


@register_qms_adapter("generic")
class GenericQMSAdapter:
    provider_name = "generic"

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        # No hardcoded fallback — a real key only ever arrives via
        # sops-decrypted org config in a deployed env.
        self.base_url: str = (cfg.get("base_url") or "").rstrip("/")
        self.api_key: str = cfg.get("api_key", "")
        self.timeout = float(cfg.get("timeout_seconds", 10.0))

    def _require_credentials(self) -> None:
        if not self.base_url or not self.api_key:
            raise RuntimeError(
                "generic QMS adapter requires `base_url` and `api_key` in qms config"
            )

    async def fetch_inspections(
        self, *, since: datetime | None = None
    ) -> list[QMSInspectionRecord]:
        # Fail closed: no credentials → no records. The sync service
        # surfaces this as an unconfigured-provider error rather than
        # silently landing fabricated inspection rows.
        self._require_credentials()
        # Live implementation: GET {base_url}/inspections?since={since} with
        # an Authorization header, parse each JSON record into a
        # QMSInspectionRecord (mapping the provider's field names + its
        # disposition vocabulary onto pass/fail/partial). Not implemented in
        # the skeleton.
        #
        #   from app.utils.url_safety import org_host_client
        #
        #   client = await org_host_client(
        #       self.base_url,
        #       allow_private=not settings.is_deployed,  # a local QMS emulator
        #       timeout=self.timeout,
        #       follow_redirects=False,
        #   )
        #   async with client:
        #       resp = await client.get(
        #           f"{self.base_url}/inspections",
        #           params={"since": since.isoformat()} if since else None,
        #           headers={"Authorization": f"Bearer {self.api_key}"},
        #       )
        #       resp.raise_for_status()
        #       return [self._map(r) for r in resp.json()["data"]]
        raise NotImplementedError(
            "generic QMS adapter is a skeleton — live REST integration required"
        )

    async def test_connection(self) -> bool:
        # Fail closed without credentials; never raise from the probe.
        if not self.base_url or not self.api_key:
            return False
        # Live implementation: cheap authenticated GET (health / ping).
        return False
