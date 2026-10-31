"""Merge.dev unified API adapter — one integration for all ERPs."""

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.config import settings
from app.services.erp_adapters.base import (
    ACCOUNT_NOT_LINKED,
    VENDOR_NOT_LINKED,
    ErpAdapter,
    ErpInvoiceStatus,
    ErpListingIncomplete,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    ListingGap,
    PoLinePayload,
    PoListing,
    PoPayload,
    PoSkipReason,
    SkippedPo,
    VendorPayload,
    erp_http_failure,
    erp_preflight_refusal,
    sync_listing_pages,
)
from app.services.erp_adapters.dispatcher import register_adapter
from app.utils.json_money import dumps_exact_json

logger = logging.getLogger(__name__)


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


# Merge.dev's PO status enum → our internal vocabulary. Merge publishes
# DRAFT / SUBMITTED / AUTHORIZED / BILLED / DELETED (an open enum: an
# integration may pass another value through); BILLED and DELETED map as
# Xero's same statuses do. Anything not listed maps to "open" (the safest
# default — it shows up in the matching pool but doesn't mark an unfamiliar
# PO as closed/cancelled and exclude it).
_MERGE_PO_STATUS_MAP = {
    "OPEN": "open",
    "PENDING": "open",
    "DRAFT": "open",
    "SUBMITTED": "open",
    "AUTHORIZED": "open",
    "BILLED": "closed",
    "DELETED": "cancelled",
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
    # Every PO with no number used to be filed under this one row (§328).
    retired_po_numbers = ("UNKNOWN",)

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
        # Merge's Invoice references its vendor (`contact`) and each line's
        # `account` by MERGE object id — the ids our vendor and chart syncs
        # store. Without them the underlying ERP gets a bill with no vendor,
        # or a GL code where an id belongs. Refuse before any HTTP call, as
        # the direct adapters do; never fall back to the name or the code.
        if not payload.vendor_erp_id:
            return erp_preflight_refusal("Merge.dev", VENDOR_NOT_LINKED)
        if any(li.gl_account and not li.gl_account_erp_id for li in payload.line_items):
            return erp_preflight_refusal("Merge.dev", ACCOUNT_NOT_LINKED)
        body = {
            "model": {
                "type": "ACCOUNTS_PAYABLE",
                "contact": payload.vendor_erp_id,
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
                        # An uncoded line posts no account (Merge allows it);
                        # a coded one was refused above unless it has an id.
                        "account": li.gl_account_erp_id,
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
            return erp_http_failure(
                "Merge.dev",
                resp.status_code,
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
        return (await self.list_pos_report()).pos

    async def list_pos_report(self) -> PoListing:
        """Every purchase order via Merge's unified ``/purchase-orders``, and the
        ones left out for want of a total.

        A record with neither a number nor an id is dropped (no stored row can
        be found for it, so there is nothing to mark). One with a number but no
        usable ``total_amount`` is skipped, never synced at 0, and named in
        ``skipped`` as ``unpriceable``: at 0 every invoice against it read as
        over-billed, and a total an earlier sync stored for it must stop being
        matched against (decisions §323, §325). The total is never summed from
        the lines either, the rule §325 kept for every adapter: a line list can
        omit tax, charges or discounts the header total carries.

        Read whole or not at all (:meth:`_list_complete`): a non-2xx, a network
        error, an unreadable page or more than ``SYNC_LISTING_MAX_ROWS`` raises,
        and the PO sync answers the coded 502 with nothing synced. It used to
        answer "no POs" on a failure and stop silently at 1,000, which hides
        whatever the unread part held (decisions §323, §329).
        """
        items: list[PoPayload] = []
        skipped: list[SkippedPo] = []
        unnumbered = 0
        records = await self._list_complete(
            "purchase-orders", {"page_size": "100", "expand": "vendor,line_items"}
        )
        for raw in records:
            number = _merge_po_number(raw)
            if not number:
                unnumbered += 1
                continue
            payload = _merge_po_to_payload(raw)
            if payload is None:
                skipped.append(
                    SkippedPo(
                        str(number),
                        PoSkipReason.unpriceable,
                        legacy_po_numbers=_merge_legacy_po_numbers(raw, number),
                    )
                )
            else:
                items.append(payload)

        if unnumbered:
            # A count only: a PO record can carry vendor names and amounts.
            logger.warning("Merge.dev PO sync skipped %d PO(s) with no number or id", unnumbered)
        return PoListing(pos=items, skipped=skipped)

    async def _list_complete(self, path: str, params: dict[str, str]) -> list[dict]:
        """Every record of a Merge collection, following ``next`` cursors.

        Any non-2xx, network error or unreadable body raises
        :class:`ErpListingIncomplete`, as does a collection longer than
        ``SYNC_LISTING_MAX_ROWS``: the chart and vendor syncs read absence as
        evidence (base), and the PO sync reads the listing's skips, so none of
        them gets the pages read before a failure as if they were the whole
        collection.
        """
        records: list[dict] = []
        params = dict(params)
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(sync_listing_pages(100)):
                try:
                    resp = await client.get(
                        f"{_api_base()}/{path}", params=params, headers=self._headers()
                    )
                except httpx.HTTPError:
                    raise ErpListingIncomplete("Merge.dev", path, ListingGap.fetch_failed) from None
                if resp.status_code != 200:
                    raise ErpListingIncomplete("Merge.dev", path, ListingGap.fetch_failed)
                try:
                    body = resp.json() if resp.content else {}
                except ValueError:
                    raise ErpListingIncomplete("Merge.dev", path, ListingGap.fetch_failed) from None
                records.extend(r for r in body.get("results") or [] if isinstance(r, dict))
                cursor = body.get("next")
                if not cursor:
                    return records
                params["cursor"] = cursor
        raise ErpListingIncomplete("Merge.dev", path, ListingGap.truncated)

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        """The whole chart via Merge's unified ``/accounts``, or
        :class:`ErpListingIncomplete` — never the pages read before a failure.

        A Merge Account's ``parent_account`` is the parent's Merge id (a uuid),
        not a code (https://docs.merge.dev/openapi/accounting.json, ``Account``).
        It is carried as ``parent_erp_account_id`` and resolved to the parent's
        code within this pull; a parent outside the pull has no code here, so
        ``parent_code`` stays None rather than holding the uuid. A null
        ``parent_account`` may mean "top-level" or "not reported" depending on
        the ERP behind Merge, so ``reports_parent`` stays False and the sync
        never clears a parent on Merge's silence.
        """
        items: list[GLAccountPayload] = []
        for raw in await self._list_complete("accounts", {"page_size": "100"}):
            payload = _merge_account_to_payload(raw)
            if payload is not None:
                items.append(payload)
        code_by_id = {p.erp_account_id: p.code for p in items}
        for p in items:
            if p.parent_erp_account_id:
                p.parent_code = code_by_id.get(p.parent_erp_account_id)
        return items

    async def list_vendors(self) -> list[VendorPayload]:
        """Every supplier via Merge's unified ``/contacts?is_supplier=true``, or
        :class:`ErpListingIncomplete` — never the pages read before a failure.

        Merge's Accounting API has no ``/vendors`` path and no Vendor model: a
        supplier is a ``Contact`` with ``is_supplier`` true (Merge Accounting
        OpenAPI, https://docs.merge.dev/openapi/accounting.json, ``GET
        /accounting/v1/contacts``; https://docs.merge.dev/accounting/contacts/).
        A Contact's ``id`` is what a PurchaseOrder's ``vendor`` and an
        Invoice's ``contact`` carry, so it is the ``erp_vendor_id`` the PO sync
        links by and the bill push posts. ``addresses`` and ``phone_numbers``
        are expanded so they arrive as objects, not bare ids.
        """
        records = await self._list_complete(
            "contacts",
            {
                "page_size": "100",
                "is_supplier": "true",
                "expand": "addresses,phone_numbers",
            },
        )
        return [v for v in map(_merge_contact_to_payload, records) if v is not None]

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


def _merge_po_number(raw: dict) -> object:
    """The PO's number: Merge's ``purchase_order_number``, else ``number`` /
    ``transaction_number`` for records shaped the older way, else the record id.
    Falsy when the record carries none of them."""
    return (
        raw.get("purchase_order_number")
        or raw.get("number")
        or raw.get("transaction_number")
        or raw.get("id")
    )


def _merge_legacy_po_numbers(raw: dict, po_number: object) -> tuple[str, ...]:
    """The key a build before ``purchase_order_number`` stored this PO under
    (``number``, else ``transaction_number``, else the record id), so the sync
    can re-key that row rather than create a second one beside it, or mark it
    where it stands when the PO cannot be priced (§328)."""
    legacy = raw.get("number") or raw.get("transaction_number") or raw.get("id")
    return (str(legacy),) if legacy and str(legacy) != str(po_number) else ()


def _merge_po_to_payload(raw: dict) -> PoPayload | None:
    """Map a Merge.dev PO record to our normalized PoPayload.

    Merge's `vendor` is the supplier's Merge object id: an expanded object
    (when `expand=vendor` was passed) carrying it as `id`, or the bare id
    string. That id is ``vendor_erp_id`` — the Merge ``id`` ``list_vendors``
    reports as ``erp_vendor_id`` — so the sync links the PO by id, not by name,
    which a rename breaks and two suppliers can share. The name is read only
    from the expanded object.

    A line's `account` is a Merge Account id (a UUID), not an account code, so
    it is not ``gl_account``: stored as one it would be a code no chart row
    has. Nothing persists or reads a PO line's GL account, so lines carry none
    rather than costing the pull a chart read (the QuickBooks PO sync's rule).

    A record with no usable ``total_amount`` (absent, unparseable, not
    finite) maps to None too: ``list_pos_report`` names it ``unpriceable``
    rather than syncing it at 0. A stated ``0`` is a figure and is kept.

    A record with neither a number nor a Merge ``id`` maps to None and is
    skipped. The sync upserts by ``po_number``, so a shared placeholder (this
    used to be ``"UNKNOWN"``) would file every such PO into ONE row, each
    overwriting the last one's vendor, total and lines — a PO nobody issued
    for an amount nobody approved, matched against real invoices.
    """
    po_number = _merge_po_number(raw)
    if not po_number:
        return None
    total = _to_decimal(raw.get("total_amount"))
    if total is None or not total.is_finite():
        return None

    vendor = raw.get("vendor")
    vendor_name: str | None = None
    vendor_erp_id: str | None = None
    if isinstance(vendor, dict):
        vendor_name = vendor.get("name") or vendor.get("contact_name")
        vendor_erp_id = vendor.get("id") or None
    elif isinstance(vendor, str):
        vendor_erp_id = vendor or None

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
            )
        )

    # Merge.dev exposes the promised delivery date under a few names depending
    # on the upstream ERP; map the first that's present, else leave it None
    # (no fabrication — the real adapter only carries what the ERP supplied).
    expected_delivery_date = _to_date(
        raw.get("delivery_date")
        or raw.get("expected_delivery_date")
        or raw.get("requested_delivery_date")
    )

    # Merge's unified PurchaseOrder carries an ISO 4217 `currency` beside
    # `total_amount`. Passed through as given; `api/purchase_orders` normalises
    # it and stores NULL for an absent or malformed code — never a default.
    raw_currency = raw.get("currency")

    return PoPayload(
        po_number=str(po_number),
        vendor_name=vendor_name,
        vendor_erp_id=str(vendor_erp_id) if vendor_erp_id else None,
        total=total,
        status=status,
        expected_delivery_date=expected_delivery_date,
        line_items=line_items,
        currency=raw_currency if isinstance(raw_currency, str) else None,
        legacy_po_numbers=_merge_legacy_po_numbers(raw, po_number),
    )


def _merge_account_to_payload(raw: dict) -> GLAccountPayload | None:
    """Map a Merge.dev account record to our GLAccountPayload.

    ``code`` is the account number when the underlying ERP numbers its
    accounts (every one of them keeps the number unique), else the Merge
    object ``id`` — never the ``name``. A name is a display label the ERP
    lets two accounts share (QuickBooks' "Job Materials" under two parents),
    and keyed on it two accounts collapse into one row that posts to the
    wrong one (docs/decisions.md §284). A record with no ``id`` is dropped:
    a Merge bill line posts against that id, so the row could never be used,
    and its ``erp_account_id`` would otherwise have been its code.
    A record with neither a number nor a name is dropped too: nobody could
    pick it.

    An ``INACTIVE`` account, or one Merge reports deleted in the ERP
    (``remote_was_deleted``), is emitted ``active=False``: its id is still the
    ERP's, so a row linked to it is not dead, but no bill line may post to it.
    ``parent_code`` is resolved by ``list_gl_accounts`` within the pull.
    """
    merge_id = raw.get("id")
    number = raw.get("account_number") or raw.get("number")
    if not merge_id or not (number or raw.get("name")):
        return None
    code = number or merge_id
    name = raw.get("name") or code

    raw_classification = (raw.get("classification") or "").upper()
    account_type = _MERGE_ACCOUNT_TYPE_MAP.get(raw_classification)

    return GLAccountPayload(
        code=str(code),
        name=str(name),
        account_type=account_type,
        erp_account_id=str(merge_id),
        parent_erp_account_id=(
            raw.get("parent_account") if isinstance(raw.get("parent_account"), str) else None
        )
        or None,
        active=(raw.get("status") or "").upper() != "INACTIVE"
        and raw.get("remote_was_deleted") is not True,
    )


def _merge_contact_to_payload(raw: dict) -> VendorPayload | None:
    """Map a Merge.dev supplier ``Contact`` to our normalized VendorPayload,
    or None for a record with no Merge ``id`` or one Merge says is not a
    supplier.

    ``erp_vendor_id`` is the Merge ``id`` and nothing else: it is what
    ``post_invoice`` posts as the bill's ``contact`` and what a PO's ``vendor``
    names. ``remote_id`` is the underlying ERP's own id (a different kind of
    key Merge would refuse as a contact), and a name is no key at all — two
    same-named vendors would each relink one row past the sync's
    ambiguous-name guard (docs/decisions.md §284). A record with no id is
    skipped, as NetSuite's mapping does.

    Follows Merge's Accounting ``Contact`` model: ``name``, ``email_address``,
    ``tax_number``, ``phone_numbers[]`` (``number``), ``addresses[]``
    (``street_1`` / ``street_2`` / ``city`` / ``state`` / ``zip_code`` /
    ``country``; first entry used), and ``status`` ``ACTIVE`` / ``ARCHIVED``.
    A Contact carries **no payment term**, so ``payment_terms`` is always None
    — never a default — and ``sync_vendors_from_erp`` keeps whatever terms the
    local vendor already has. An ``ARCHIVED`` contact, or one Merge reports as
    deleted in the ERP (``remote_was_deleted``), is emitted ``active=False``:
    the ERP will not take a bill against it.
    """
    vendor_id = raw.get("id")
    if not vendor_id:
        return None
    # The listing asks for suppliers only; a record that says otherwise is a
    # customer and never a payee.
    if raw.get("is_supplier") is False:
        return None
    name = raw.get("name") or str(vendor_id)

    address: str | None = None
    addresses = raw.get("addresses")
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
        first = addresses[0]
        parts = [
            first.get("street_1"),
            first.get("street_2"),
            first.get("city"),
            first.get("state") if isinstance(first.get("state"), str) else None,
            first.get("zip_code"),
            first.get("country"),
        ]
        address = ", ".join(p for p in parts if p) or None

    phone: str | None = None
    phones = raw.get("phone_numbers")
    if isinstance(phones, list):
        phone = next(
            (p["number"] for p in phones if isinstance(p, dict) and p.get("number")),
            None,
        )

    status = (raw.get("status") or "").upper()
    active = status != "ARCHIVED" and raw.get("remote_was_deleted") is not True

    return VendorPayload(
        erp_vendor_id=str(vendor_id),
        name=name,
        email=raw.get("email_address") or None,
        phone=phone,
        address=address,
        tax_id=raw.get("tax_number") or None,
        payment_terms=None,
        active=active,
    )
