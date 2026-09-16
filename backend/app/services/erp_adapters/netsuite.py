"""Oracle NetSuite adapter — direct REST API integration with Token-Based Auth."""

import hashlib
import hmac
import time
import uuid
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    ErpAdapter,
    ErpInvoiceStatus,
    ErpPostResult,
    InvoicePayload,
    VendorPayload,
    erp_failure_message,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.utils.json_money import dumps_exact_json


@register_adapter("netsuite")
class NetSuiteAdapter(ErpAdapter):
    """Direct integration with Oracle NetSuite REST API.

    Required config:
        account_id: NetSuite account ID (e.g. "1234567")
        consumer_key: Integration consumer key
        consumer_secret: Integration consumer secret
        token_id: Token-based auth token ID
        token_secret: Token-based auth token secret
    """

    erp_type = "netsuite"

    def _base_url(self) -> str:
        # OPERATOR-controlled override (env/process level, not tenant-admin
        # config) so local dev + e2e can point the adapter at the fake ERP
        # container (backend/docker-compose.yml `fake-erp`, host port 12112).
        # Trusted, so no SSRF guard. Empty (the default) = derive the real
        # per-account NetSuite URL from account_id. OAuth 1.0 signing below
        # always signs the URL actually used, so requests to the override
        # carry a signature computed over the override URL.
        if settings.erp_netsuite_api_base:
            return settings.erp_netsuite_api_base.rstrip("/")
        account = self.config["account_id"].replace("_", "-").lower()
        return f"https://{account}.suitetalk.api.netsuite.com/services/rest/record/v1"

    def _auth_header(self, method: str, url: str) -> str:
        """Generate OAuth 1.0 authorization header for NetSuite TBA."""
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time()))

        params = {
            "oauth_consumer_key": self.config["consumer_key"],
            "oauth_token": self.config["token_id"],
            "oauth_nonce": nonce,
            "oauth_timestamp": timestamp,
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_version": "1.0",
        }

        # Build signature base string
        param_str = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(params.items()))
        base_string = f"{method.upper()}&{quote(url, safe='')}&{quote(param_str, safe='')}"

        signing_key = (
            f"{quote(self.config['consumer_secret'])}&{quote(self.config['token_secret'])}"
        )
        signature = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha256).digest()

        import base64

        sig_b64 = base64.b64encode(signature).decode()

        parts = [
            f'OAuth realm="{self.config["account_id"]}"',
            f'oauth_consumer_key="{params["oauth_consumer_key"]}"',
            f'oauth_token="{params["oauth_token"]}"',
            f'oauth_nonce="{nonce}"',
            f'oauth_timestamp="{timestamp}"',
            'oauth_signature_method="HMAC-SHA256"',
            'oauth_version="1.0"',
            f'oauth_signature="{quote(sig_b64)}"',
        ]
        return ", ".join(parts)

    async def _find_by_external_id(self, external_id: str) -> str | None:
        """Look up an existing vendorBill by externalId.

        NetSuite enforces externalId uniqueness per record type, so this is
        the pre-create idempotency check (issue #143): a retried push after a
        client-side timeout on the FIRST attempt's response (which may have
        already succeeded server-side) finds the already-created bill here
        instead of blindly POSTing a second one.
        """
        q = f'externalId IS "{external_id}"'
        url = f"{self._base_url()}/vendorBill?q={quote(q, safe='')}"
        headers = {"Authorization": self._auth_header("GET", url)}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None
        return items[0].get("id")

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        existing_id = await self._find_by_external_id(payload.correlation_id)
        if existing_id:
            return ErpPostResult(
                success=True,
                erp_document_id=existing_id,
                erp_document_number=payload.invoice_number,
                message="Already posted to NetSuite (idempotent — found by externalId)",
            )

        url = f"{self._base_url()}/vendorBill"

        body = {
            "tranId": payload.invoice_number,
            "tranDate": payload.invoice_date.isoformat() if payload.invoice_date else None,
            "dueDate": payload.due_date.isoformat() if payload.due_date else None,
            "currency": {"refName": payload.currency},
            "memo": payload.description,
            "externalId": payload.correlation_id,
            # Money stays Decimal all the way to the encoder — `float()` here
            # would post a rounded rate into the customer's ledger (see
            # `utils/json_money`). NetSuite types `rate`/`quantity` as JSON
            # numbers, and `dumps_exact_json` still emits numbers, so the wire
            # contract is unchanged.
            "item": {
                "items": [
                    {
                        "description": li.description or "",
                        "quantity": li.quantity if li.quantity else 1,
                        "rate": li.unit_price if li.unit_price else (li.total or Decimal(0)),
                        "account": {"refName": li.gl_account} if li.gl_account else None,
                    }
                    for li in payload.line_items
                ]
                if payload.line_items
                else [
                    {
                        "description": payload.description or "",
                        "quantity": 1,
                        "rate": payload.amount,
                        "account": {"refName": payload.gl_account} if payload.gl_account else None,
                    }
                ],
            },
        }

        headers = {
            "Authorization": self._auth_header("POST", url),
            "Content-Type": "application/json",
            "Prefer": "respond-async",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, content=dumps_exact_json(body), headers=headers)

        if resp.status_code in (200, 201, 204):
            # NetSuite returns the record ID in the Location header
            location = resp.headers.get("Location", "")
            doc_id = location.rsplit("/", 1)[-1] if location else None
            return ErpPostResult(
                success=True,
                erp_document_id=doc_id,
                erp_document_number=payload.invoice_number,
                message="Posted to NetSuite",
                raw_response=resp.json() if resp.content else None,
            )
        else:
            return ErpPostResult(
                success=False,
                message=erp_failure_message("NetSuite", resp.status_code),
                raw_response=resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else None,
            )

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        url = f"{self._base_url()}/vendorBill/{erp_document_id}"
        headers = {
            "Authorization": self._auth_header("GET", url),
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown

        data = resp.json()
        ns_status = data.get("status", {}).get("refName", "").lower()

        status_map = {
            "open": ErpInvoiceStatus.open,
            "pendingapproval": ErpInvoiceStatus.draft,
            "paidinfull": ErpInvoiceStatus.paid,
            "cancelled": ErpInvoiceStatus.cancelled,
            "voided": ErpInvoiceStatus.cancelled,
        }
        return status_map.get(ns_status, ErpInvoiceStatus.unknown)

    async def void_invoice(self, erp_document_id: str) -> bool:
        # NetSuite uses a "void" transform
        return False

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors via NetSuite's `/vendor` record collection.

        Best-effort like the Merge.dev adapter's `list_pos`/`list_gl_accounts`:
        a non-200 response or a network error degrades to an empty list rather
        than raising, so an unreachable/misconfigured NetSuite account doesn't
        500 the `/api/vendors/sync-erp` endpoint. NetSuite pages this
        collection via `offset` + `hasMore`; we follow it capped at 1000
        vendors (10 pages × 100) to bound memory, matching the PO/GL sync cap.
        """
        items: list[VendorPayload] = []
        offset = 0
        limit = 100

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(10):  # 10 pages × 100 = 1000 vendor cap
                url = f"{self._base_url()}/vendor?limit={limit}&offset={offset}"
                headers = {"Authorization": self._auth_header("GET", url)}
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError:
                    break

                if resp.status_code != 200:
                    break

                body = resp.json() if resp.content else {}
                for raw in body.get("items") or []:
                    items.append(_netsuite_vendor_to_payload(raw))

                if not body.get("hasMore"):
                    break
                offset += limit

        return items

    async def test_connection(self) -> bool:
        try:
            url = f"{self._base_url()}/vendor?limit=1"
            headers = {
                "Authorization": self._auth_header("GET", url),
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
            return resp.status_code == 200
        except Exception:
            return False


def _netsuite_vendor_to_payload(raw: dict) -> VendorPayload:
    """Map a NetSuite vendor record to our normalized VendorPayload.

    `entityId` is the vendor record's name/display field (what
    `test_connection` and the fake-erp fixture both key on); real vendor
    records may also carry `companyName`, `email`, `phone`. Anything absent
    maps to None — `sync_vendors_from_erp` never nulls out an existing local
    value for a missing field.
    """
    vendor_id = raw.get("id")
    name = raw.get("entityId") or raw.get("companyName") or (str(vendor_id) if vendor_id else "")

    return VendorPayload(
        erp_vendor_id=str(vendor_id) if vendor_id is not None else name,
        name=name,
        email=raw.get("email"),
        phone=raw.get("phone"),
    )
