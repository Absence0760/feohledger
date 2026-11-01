"""AS4 gateway adapter — transmits via a hosted PEPPOL Access Point's HTTP API.

We are corner C1/C2-client: this adapter speaks the gateway's REST API, and
the gateway performs the AS4/ebMS3 handshake to the receiver's Access Point
(C3). We do NOT implement raw AS4 SOAP here.

The gateway base URL + API key come from config (per-org
``Organization.settings.peppol`` first, then the process-level
``settings.peppol_gateway_*``). The API key has NO hardcoded fallback — when
it is empty the adapter returns the PII-free ``peppol_not_configured`` outcome
(mirrors ``increase``'s ``increase_not_configured``) instead of attempting a
networked call. The live key is supplied via sops in deployed envs.

A per-org ``gateway_url`` is SSRF-guarded (``_client``); in a deployed
environment one that is not publicly routable yields the PII-free
``gateway_url_not_public`` outcome without a request being sent.

SBDH (Standard Business Document Header) wrapping happens HERE, in the adapter
layer — never in the e_invoice generator (which only emits clean UBL). Most
hosted APs accept the raw UBL plus the participant/doc-type/process metadata
and build the SBDH server-side; we pass that metadata in the request body and
let the gateway wrap. If a gateway requires a client-built SBDH, this is the
single place to add it.
"""

from __future__ import annotations

import base64
import logging

import httpx

from app.config import settings
from app.services.peppol_adapters.base import (
    ParticipantCapability,
    ParticipantId,
    PeppolAdapter,
    TransmissionRequest,
    TransmissionResult,
)
from app.services.peppol_adapters.dispatcher import register_peppol_adapter
from app.utils.url_safety import UnsafeUrlError, org_host_client

logger = logging.getLogger(__name__)

TIMEOUT = 20.0

#: The PII-free outcome when a per-org ``gateway_url`` is not publicly routable
#: (or was rebound to an internal address at connect) in a deployed environment.
GATEWAY_URL_NOT_PUBLIC = "gateway_url_not_public"


@register_peppol_adapter("as4_gateway")
class AS4GatewayAdapter(PeppolAdapter):
    provider_name = "as4_gateway"

    def __init__(self, config: dict):
        super().__init__(config)
        # Per-org config wins; fall back to the process-level setting. The API
        # key has NO hardcoded fallback — empty means "not configured".
        org_url = (config.get("gateway_url") or "").strip()
        # Who supplied the host decides how it is reached (`_client`): a tenant
        # admin's URL is untrusted, the operator's FEOH_PEPPOL_GATEWAY_URL is not.
        self.gateway_url_from_org: bool = bool(org_url)
        self.gateway_url: str = (org_url or settings.peppol_gateway_url).rstrip("/")
        self.api_key: str = config.get("api_key") or settings.peppol_gateway_api_key

    async def _client(self) -> httpx.AsyncClient:
        """The HTTP client for every gateway call.

        A per-org ``gateway_url`` is SSRF-guarded: checked up front and pinned at
        connect (``url_safety.org_host_client``), with loopback allowed outside a
        deployed environment so a local Access Point emulator still works. Every
        call carries the gateway API key, and ``resolve_participant`` reflects
        part of the answer back to the caller, so an internal target is both a
        credential leak and a read-back channel. The operator's process-level
        ``FEOH_PEPPOL_GATEWAY_URL`` is trusted and keeps a plain client
        (decisions §334). Raises :class:`UnsafeUrlError`.
        """
        if not self.gateway_url_from_org:
            return httpx.AsyncClient(timeout=TIMEOUT)
        return await org_host_client(
            self.gateway_url, allow_private=not settings.is_deployed, timeout=TIMEOUT
        )

    def _refused(self) -> None:
        # PII-free: names the field, never the URL or the address it resolved to.
        logger.warning("PEPPOL gateway_url is not publicly routable; refusing to call it")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def resolve_participant(self, participant_id: ParticipantId) -> ParticipantCapability:
        if not self.api_key or not self.gateway_url:
            return ParticipantCapability(
                participant_id=participant_id,
                registered=False,
                unregistered_reason="peppol_not_configured",
            )
        try:
            async with await self._client() as client:
                response = await client.get(
                    f"{self.gateway_url}/smp/participants/{participant_id.format()}",
                    headers=self._headers(),
                )
        except UnsafeUrlError:
            self._refused()
            return ParticipantCapability(
                participant_id=participant_id,
                registered=False,
                unregistered_reason=GATEWAY_URL_NOT_PUBLIC,
            )
        except httpx.RequestError as exc:
            # PII-free: class name only, never the participant value.
            return ParticipantCapability(
                participant_id=participant_id,
                registered=False,
                unregistered_reason=f"gateway_transport_error:{exc.__class__.__name__}",
            )
        if response.status_code == 404:
            return ParticipantCapability(
                participant_id=participant_id,
                registered=False,
                unregistered_reason="receiver_not_registered",
            )
        if response.status_code >= 400:
            return ParticipantCapability(
                participant_id=participant_id,
                registered=False,
                unregistered_reason=f"gateway_error:{response.status_code}",
            )
        data = response.json() or {}
        return ParticipantCapability(
            participant_id=participant_id,
            registered=bool(data.get("registered", True)),
            access_point_url=data.get("access_point_url"),
            supported_doc_types=tuple(data.get("supported_doc_types") or ()),
        )

    async def send(self, request: TransmissionRequest) -> TransmissionResult:
        if not self.api_key or not self.gateway_url:
            return TransmissionResult(
                success=False,
                status="failed",
                failure_reason="peppol_not_configured",
            )
        # The gateway builds the SBDH from this metadata + the UBL payload. We
        # base64 the UBL so it travels safely in JSON. business_message_id is
        # sent as the gateway-level idempotency key (defence in depth; the DB
        # unique index is the authoritative one-time guarantee).
        body = {
            "sender": request.sender.format(),
            "receiver": request.receiver.format(),
            "doc_type_id": request.doc_type_id,
            "process_id": request.process_id,
            "business_message_id": request.business_message_id,
            "payload_base64": base64.b64encode(request.payload).decode("ascii"),
        }
        headers = {**self._headers(), "Idempotency-Key": request.business_message_id}
        try:
            async with await self._client() as client:
                response = await client.post(
                    f"{self.gateway_url}/as4/outbound",
                    json=body,
                    headers=headers,
                )
        except UnsafeUrlError:
            self._refused()
            return TransmissionResult(
                success=False,
                status="failed",
                failure_reason=GATEWAY_URL_NOT_PUBLIC,
            )
        except httpx.RequestError as exc:
            return TransmissionResult(
                success=False,
                status="failed",
                failure_reason=f"gateway_transport_error:{exc.__class__.__name__}",
            )
        if response.status_code >= 400:
            try:
                err = response.json() or {}
            except ValueError:
                err = {}
            # PII-free: the gateway's error code / status, never the payload.
            return TransmissionResult(
                success=False,
                status="failed",
                failure_reason=f"gateway_error:{err.get('code') or response.status_code}",
            )
        try:
            data = response.json() or {}
        except ValueError:
            # A non-JSON 2xx body (CDN/WAF interception) is recoverable: treat
            # it as an empty success so status resolves to the default 'sent'.
            data = {}
        status = data.get("status", "sent")
        # A failed transmission must NOT carry a message_id: it would land in
        # the partial unique index `uq_peppol_message_id` and an explicitly-
        # supported retry (which reuses the same business_message_id, so a real
        # AP returns the same MessageId) would then collide → IntegrityError.
        message_id = data.get("message_id") if status != "failed" else None
        return TransmissionResult(
            success=status != "failed",
            message_id=message_id,
            status=status,
            failure_reason=data.get("failure_code") if status == "failed" else None,
            raw_response=data,
        )

    async def test_connection(self) -> bool:
        if not self.api_key or not self.gateway_url:
            return False
        try:
            async with await self._client() as client:
                response = await client.get(
                    f"{self.gateway_url}/health",
                    headers=self._headers(),
                )
        except UnsafeUrlError:
            self._refused()
            return False
        except httpx.RequestError:
            return False
        return response.status_code < 400

    def parse_inbound(self, headers: dict, body: bytes):
        """Parse the hosted Access Point's inbound delivery envelope.

        The route already verified the gateway HMAC over ``body`` (via
        ``peppol_receive.verify_inbound_signature`` + the
        ``FEOH_PEPPOL_INBOUND_SIGNING_SECRET``) and dedupes by the AS4 MessageId,
        so this only unpacks the (trusted) envelope into an
        :class:`InboundPeppolMessage`.

        Most hosted APs POST a JSON envelope with the SBDH metadata extracted
        plus the inbound UBL/CII base64-encoded — the inverse of the outbound
        ``send`` body. The exact field names are provider-specific; this maps
        the common shape and tolerates a couple of aliases. Returns ``None`` on
        an unparseable body or a missing MessageId so the route refuses a
        delivery it could never dedupe.
        """
        import base64 as _b64
        import json as _json

        from app.services.peppol_receive import InboundPeppolMessage

        try:
            envelope = _json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            envelope = None
        if not isinstance(envelope, dict):
            return None

        # The AS4 MessageId — providers name it variously.
        message_id = str(
            envelope.get("message_id")
            or envelope.get("messageId")
            or envelope.get("as4_message_id")
            or ""
        )
        if not message_id:
            return None

        sender = envelope.get("sender") or {}
        if isinstance(sender, str):
            # Wire form "iso6523-actorid-upis::9930:DE..." or "9930:DE...".
            try:
                pid = ParticipantId.parse(sender)
                sender_scheme, sender_value = pid.scheme, pid.value
            except ValueError:
                sender_scheme, sender_value = "", ""
        else:
            sender_scheme = str(sender.get("scheme") or envelope.get("sender_scheme") or "")
            sender_value = str(sender.get("value") or envelope.get("sender_value") or "")

        doc_type_id = str(envelope.get("doc_type_id") or envelope.get("docTypeId") or "")
        process_id = str(envelope.get("process_id") or envelope.get("processId") or "")

        payload_b64 = envelope.get("payload_base64") or envelope.get("payloadBase64")
        if payload_b64:
            try:
                payload = _b64.b64decode(payload_b64)
            except (ValueError, TypeError):
                return None
        elif envelope.get("payload"):
            payload = str(envelope["payload"]).encode("utf-8")
        else:
            return None

        return InboundPeppolMessage(
            message_id=message_id,
            sender_scheme=sender_scheme,
            sender_value=sender_value,
            doc_type_id=doc_type_id,
            process_id=process_id,
            payload=payload,
        )
