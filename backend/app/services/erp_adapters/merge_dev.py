"""Merge.dev unified API adapter — one integration for all ERPs."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    ErpAdapter,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    PoLinePayload,
    PoPayload,
    VendorPayload,
    erp_failure_message,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.utils.json_money import dumps_exact_json


def _to_decimal(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _to_date(v) -> date | None:
    """Parse a Merge.dev date field (ISO ``YYYY-MM-DD`` or full ISO datetime)
    into a ``date``. Returns None for anything unparseable — never fabricates a
    date and never raises, so a malformed upstream value just leaves the PO's
    expected delivery date empty rather than breaking the whole sync."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


# Merge.dev's PO status enum → our internal vocabulary. Anything not
# listed maps to "open" (the safest default — it shows up in the
# matching pool but doesn't mark an unfamiliar PO as closed/cancelled
# and exclude it).
_MERGE_PO_STATUS_MAP = {
    "OPEN": "open",
    "PENDING": "open",
    "DRAFT": "open",
    "CLOSED": "closed",
    "FULFILLED": "closed",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
    "VOIDED": "cancelled",
}

# Merge's account-classification enum → our internal account_type
# vocabulary. Anything not listed maps to None so the row still imports
# but isn't miscategorized. `account_type` is a free-text column so
# downstream this is informational, not a hard constraint.
_MERGE_ACCOUNT_TYPE_MAP = {
    "ASSET": "asset",
    "LIABILITY": "liability",
    "EQUITY": "equity",
    "REVENUE": "revenue",
    "INCOME": "revenue",
    "EXPENSE": "expense",
    "EXPENSES": "expense",
    "COST_OF_GOODS_SOLD": "expense",
}


def _api_base() -> str:
    """Merge.dev API base URL.

    Reads ``settings.erp_merge_api_base`` (default: live Merge.dev). The
    setting is OPERATOR-controlled (env/process level), not tenant-admin
    config, so it is trusted — it exists to point local dev + e2e at the
    fake ERP container (backend/docker-compose.yml `fake-erp`, host port
    12112). Read per-call, not at import, so tests can monkeypatch it.
    """
    return settings.erp_merge_api_base.rstrip("/")


@register_adapter("merge_dev")
class MergeDevAdapter(ErpAdapter):
    """Posts invoices through Merge.dev's unified accounting API.

    Required config:
        api_key: Merge.dev API key (from your Merge dashboard)
        account_token: Per-customer linked account token (created when
                       the customer connects their ERP via Merge Link)
    """

    erp_type = "merge_dev"

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "X-Account-Token": self.config["account_token"],
            "Content-Type": "application/json",
        }
        if idempotency_key:
            # Merge's unified API de-dupes a create by this header: a retried
            # push after a lost response returns the ORIGINAL record instead
            # of creating a second one (issue #143).
            headers["X-Idempotency-Key"] = idempotency_key
        return headers

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        body = {
            "model": {
                "type": "ACCOUNTS_PAYABLE",
                "number": payload.invoice_number,
                "issue_date": payload.invoice_date.isoformat() if payload.invoice_date else None,
                "due_date": payload.due_date.isoformat() if payload.due_date else None,
                "currency": payload.currency,
                # Money stays Decimal all the way to the encoder — `float()`
                # here would post a rounded amount into the customer's ledger
                # (see `utils/json_money`). Merge types these as JSON numbers,
                # and `dumps_exact_json` still emits numbers, so the wire
                # contract is unchanged.
                "total_amount": payload.amount,
                "sub_total": payload.subtotal if payload.subtotal else None,
                "total_tax_amount": payload.tax_amount if payload.tax_amount else None,
                "total_discount": payload.discount_amount if payload.discount_amount else None,
                "memo": payload.description,
                "purchase_order_number": payload.po_number,
                "line_items": [
                    {
                        "description": li.description,
                        "quantity": li.quantity if li.quantity else None,
                        "unit_price": li.unit_price if li.unit_price else None,
                        "total_line_amount": li.total if li.total else None,
                        "account": li.gl_account,
                    }
                    for li in payload.line_items
                ],
                "integration_params": {
                    "correlation_id": payload.correlation_id,
                },
            }
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_api_base()}/invoices",
                content=dumps_exact_json(body),
                headers=self._headers(idempotency_key=payload.correlation_id),
            )

        if resp.status_code in (200, 201):
            data = resp.json()
            model = data.get("model", {})
            return ErpPostResult(
                success=True,
                erp_document_id=model.get("id"),
                erp_document_number=model.get("number"),
                message="Posted via Merge.dev",
                raw_response=data,
            )
        else:
            return ErpPostResult(
                success=False,
                message=erp_failure_message("Merge.dev", resp.status_code),
                raw_response=resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else None,
            )

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_api_base()}/invoices/{erp_document_id}",
                headers=self._headers(),
            )

        if resp.status_code != 200:
            return ErpInvoiceStatus.unknown

        data = resp.json()
        status = data.get("status", "").upper()

        status_map = {
            "DRAFT": ErpInvoiceStatus.draft,
            "SUBMITTED": ErpInvoiceStatus.open,
            "OPEN": ErpInvoiceStatus.open,
            "PARTIALLY_PAID": ErpInvoiceStatus.partially_paid,
            "PAID": ErpInvoiceStatus.paid,
            "VOIDED": ErpInvoiceStatus.cancelled,
        }
        return status_map.get(status, ErpInvoiceStatus.unknown)

    async def void_invoice(self, erp_document_id: str) -> bool:
        # Merge.dev doesn't support direct voiding — depends on ERP capability
        return False

    async def list_pos(self) -> list[PoPayload]:
        """Pull purchase orders via Merge's unified `/purchase-orders`.

        Best-effort: any non-2xx returns an empty list rather than
        propagating, because the sync endpoint should degrade to
        "synced 0 POs" instead of 500ing the request. The underlying
        Merge call is paginated; we follow `next` cursors so the full
        list comes back, capped at 1000 to bound memory.
        """
        items: list[PoPayload] = []
        cursor: str | None = None
        params = {"page_size": "100", "expand": "vendor,line_items"}

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(10):  # 10 pages × 100 = 1000 PO cap
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await client.get(
                        f"{_api_base()}/purchase-orders",
                        params=params,
                        headers=self._headers(),
                    )
                except httpx.HTTPError:
                    break

                if resp.status_code != 200:
                    break

                body = resp.json() if resp.content else {}
                for raw in body.get("results", []) or []:
                    items.append(_merge_po_to_payload(raw))

                cursor = body.get("next")
                if not cursor:
                    break

        return items

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """Pull the chart of accounts via Merge's unified `/accounts`.

        Best-effort with the same degradation policy as `list_pos`:
        non-2xx → empty list, network error → empty list. The endpoint
        runs as part of an admin sync flow that already shows
        ERP-failure toasts, and silently no-op'ing here is friendlier
        than 502'ing the click.
        """
        items: list[GLAccountPayload] = []
        cursor: str | None = None
        params = {"page_size": "100"}

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(20):  # 20 × 100 = 2000-account cap
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await client.get(
                        f"{_api_base()}/accounts",
                        params=params,
                        headers=self._headers(),
                    )
                except httpx.HTTPError:
                    break

                if resp.status_code != 200:
                    break

                body = resp.json() if resp.content else {}
                for raw in body.get("results", []) or []:
                    payload = _merge_account_to_payload(raw)
                    if payload is not None:
                        items.append(payload)

                cursor = body.get("next")
                if not cursor:
                    break

        return items

    async def list_vendors(self) -> list[VendorPayload]:
        """Pull vendors via Merge's unified `/vendors`.

        Best-effort with the same degradation policy as `list_pos` /
        `list_gl_accounts`: non-2xx or network error → empty list, never
        raises — the `/api/vendors/sync-erp` endpoint should show "0 new
        vendors" rather than 500 when the ERP is unreachable. Paginated via
        Merge's cursor scheme, capped at 1000 vendors (10 pages × 100) to
        bound memory.
        """
        items: list[VendorPayload] = []
        cursor: str | None = None
        params = {"page_size": "100", "expand": "addresses"}

        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(10):  # 10 pages × 100 = 1000 vendor cap
                if cursor:
                    params["cursor"] = cursor
                try:
                    resp = await client.get(
                        f"{_api_base()}/vendors",
                        params=params,
                        headers=self._headers(),
                    )
                except httpx.HTTPError:
                    break

                if resp.status_code != 200:
                    break

                body = resp.json() if resp.content else {}
                for raw in body.get("results", []) or []:
                    items.append(_merge_vendor_to_payload(raw))

                cursor = body.get("next")
                if not cursor:
                    break

        return items

    async def test_connection(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{_api_base()}/account-details",
                    headers=self._headers(),
                )
            return resp.status_code == 200
        except Exception:
            return False


def _merge_po_to_payload(raw: dict) -> PoPayload:
    """Map a Merge.dev PO record to our normalized PoPayload.

    Merge's `vendor` field is either an expanded object (when
    `expand=vendor` was passed) or a bare ID string. We only get a
    name back in the expanded case — fall back to None otherwise so
    the API endpoint can still create the PO row, just without a
    vendor link.
    """
    vendor = raw.get("vendor")
    vendor_name: str | None = None
    if isinstance(vendor, dict):
        vendor_name = vendor.get("name") or vendor.get("contact_name")

    raw_status = (raw.get("status") or "").upper()
    status = _MERGE_PO_STATUS_MAP.get(raw_status, "open")

    line_items: list[PoLinePayload] = []
    for li in raw.get("line_items") or []:
        if not isinstance(li, dict):
            continue
        line_items.append(
            PoLinePayload(
                description=li.get("description") or li.get("memo"),
                quantity=_to_decimal(li.get("quantity")),
                unit_price=_to_decimal(li.get("unit_price")),
                total=_to_decimal(li.get("total_line_amount")),
                gl_account=(li.get("account") if isinstance(li.get("account"), str) else None),
            )
        )

    total = _to_decimal(raw.get("total_amount")) or Decimal("0")

    # Merge.dev exposes the promised delivery date under a few names depending
    # on the upstream ERP; map the first that's present, else leave it None
    # (no fabrication — the real adapter only carries what the ERP supplied).
    expected_delivery_date = _to_date(
        raw.get("delivery_date")
        or raw.get("expected_delivery_date")
        or raw.get("requested_delivery_date")
    )

    return PoPayload(
        po_number=raw.get("number") or raw.get("transaction_number") or raw.get("id") or "UNKNOWN",
        vendor_name=vendor_name,
        total=total,
        status=status,
        expected_delivery_date=expected_delivery_date,
        line_items=line_items,
    )


def _merge_account_to_payload(raw: dict) -> GLAccountPayload | None:
    """Map a Merge.dev account record to our GLAccountPayload.

    Returns None when the record has no `account_number` and no `name`
    — those are the two fields the upsert in
    `api/gl_accounts.py:sync_gl_accounts_from_erp` keys on, and a row
    missing both isn't useful. Better to drop than to import a row
    keyed on an opaque Merge id.
    """
    code = raw.get("account_number") or raw.get("number")
    name = raw.get("name")
    if not code and not name:
        return None
    if not code:
        # Merge sometimes ships the human name without a code (custom
        # accounts on small ERPs). Fall back to the upstream id so we
        # have *something* unique on the upsert key.
        code = raw.get("id") or name
    if not name:
        name = code

    raw_classification = (raw.get("classification") or "").upper()
    account_type = _MERGE_ACCOUNT_TYPE_MAP.get(raw_classification)

    return GLAccountPayload(
        code=str(code),
        name=str(name),
        account_type=account_type,
        erp_account_id=raw.get("id") or str(code),
        parent_code=(
            raw.get("parent_account") if isinstance(raw.get("parent_account"), str) else None
        ),
    )


def _merge_vendor_to_payload(raw: dict) -> VendorPayload:
    """Map a Merge.dev unified Vendor record to our normalized VendorPayload.

    Follows Merge's Accounting API Vendor model: `name`, `email_address`,
    `phone_number`, an `addresses` list (first entry used, if any), and a
    `payment_term` reference (object with a `name` like "Net 30", or a bare
    string on some ERPs). Anything absent maps to None — `sync_vendors_from_erp`
    never nulls out an existing local value for a missing field.
    """
    vendor_id = raw.get("id") or raw.get("remote_id")
    name = raw.get("name") or (str(vendor_id) if vendor_id else "")

    address: str | None = None
    addresses = raw.get("addresses")
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
        first = addresses[0]
        parts = [
            first.get("line1"),
            first.get("line2"),
            first.get("city"),
            first.get("state"),
            first.get("zip_code") or first.get("postal_code"),
            first.get("country"),
        ]
        address = ", ".join(p for p in parts if p) or None

    payment_terms: str | None = None
    pt = raw.get("payment_term")
    if isinstance(pt, dict):
        payment_terms = pt.get("name")
    elif isinstance(pt, str):
        payment_terms = pt

    return VendorPayload(
        erp_vendor_id=str(vendor_id) if vendor_id is not None else name,
        name=name,
        email=raw.get("email_address") or raw.get("email"),
        phone=raw.get("phone_number") or raw.get("phone"),
        address=address,
        tax_id=raw.get("tax_number") or raw.get("tax_id"),
        payment_terms=payment_terms,
    )
