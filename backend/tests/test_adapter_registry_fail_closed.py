"""A NAMED provider with no registered adapter is refused — never the fixture.

`decisions.md` §29 removed the `_REGISTRY.get(x) or _REGISTRY["mock"]` fallback
from the payment / ERP / FX dispatchers, and §36 from sanctions. The reasoning
is the same everywhere and does not depend on the domain: a `mock` adapter is
**not an inert stub**. It is the thing that makes `pnpm dev` work with no cloud
account, so it answers *yes* to everything — and substituting it for a name we
do not recognise converts a configuration error into a confident wrong answer
that nothing anywhere reports.

Four registries still fell open. Two were closed in round 15 (cards, positive
pay); this file adds the other two and pins all four together, plus the drift
guard that stops a fifth appearing.

* **`card_adapters`** — `create_card` returns `success=True` with the fixture
  PAN `4242…`, so a `VirtualCard` row landed, the payment was marked
  `completed`, and the vendor was emailed a reveal link to that fixture PAN.
* **`tax_filing_adapters`** — `submit_batch` returns `accepted` with a
  `MOCK-<year>-<hex>` confirmation, so a `Tax1099Filing` row plus a
  `tax_1099.filed` audit row told the org its 1099s were e-filed when nothing
  reached the IRS — and the idempotency slot was burned, making the corrected
  retry a no-op.
* **`tin_validation_adapters`** — `validate` checks digit grouping and the IRS
  structural rules and nothing else, yet `Vendor.tin_verified_at` was stamped
  from it, driving B-notice / 24% backup-withholding decisions off a regex.
* **`positive_pay_adapters`** — renders the `csv` layout, stored under the
  REQUESTED format on the row and the audit trail with the
  `(run, bank_format)` slot burned. The bank cannot parse it, so the
  cheque-fraud control is simply not in force and nothing says so.
* **`peppol_adapters`** — `send` returns `success=True` with a synthetic
  message id and no network involved, which `peppol_send` writes onto a
  `PeppolTransmission` as `status="sent"` + a `message_id` and records as
  `invoice.peppol_sent`. A legally-significant e-invoice was reported as
  transmitted to a supplier that never received it, and the row occupied the
  live-transmission slot so the honest resend came back `already_sent`.
* **`tax_rate_adapters`** — answers every country from the in-repo
  country-rules fixture, so `/api/international-tax/{vat,gst,rate}` computed a
  jurisdiction figure off a hardcoded rate while the response's `provider`
  field named the provider that was asked for.
* **`punchout_adapters`** — `build_setup_request` returns a synthetic
  in-process start URL (persisted as a `PunchoutSession` and navigated to), and
  `parse_order_message` reads a permissive dev envelope on the PUBLIC
  cart-return route, whose fixture cart converts into a real
  `PurchaseRequisition`.
* **`qms_adapters`** — returns three deterministic fixtures that `qms_sync`
  resolves against the tenant's REAL purchase orders and persists as
  `completed` `QualityInspection` rows. Those are the 4-way match's quality
  leg, so a fabricated `pass` clears the quality gate for whatever invoice
  references that PO — a purchase order cleared for payment by an inspection
  that never happened — and a fabricated `fail` flips real invoices to
  `mismatch`. A typo'd `FEOH_QMS_PROVIDER` opts **every** org in at once.

**The caller decides what the refusal means** (§29's per-caller table). This
file pins the callers for the two registries it converts, plus the positive-pay
route (which had the refusal but no route-level coverage of it):

  | call site | on refusal |
  |---|---|
  | `POST /api/tax/1099/file` | 409 — no filing row, no confirmation, slot free |
  | `POST /api/tax/vendors/{id}/tin-verify` | 409 — `tin_verified_at` untouched |
  | `POST /api/positive-pay/ach-authorization` | 422 — no file, no row |
  | `POST /api/invoices/{id}/peppol-send` | 422 — resolved above the slot |
  |   | claim, so no `PeppolTransmission` row at all |
  | `POST /api/peppol/inbound/{org}`  | bodyless **503** — our failure, not |
  |   | a decision (§37): ask the AP to redeliver rather than ack a drop |
  | `POST /api/international-tax/{rate,vat,gst}` | 409 — pure compute, |
  |   | nothing to unwind |
  | `POST /api/catalogs/{id}/punchout/start` | 422 — no `PunchoutSession` row |
  | `POST /api/catalogs/punchout/return/{slug}` | silent 204 — the supplier |
  |   | posts once from a browser, so there is no retry to ask for |
  | `qms_sync` background sweep | counted per-tenant failure (not a skip) |
  |   | and `last_synced_at` NOT advanced |
  | `POST /api/inspections/sync` | 409 — an operator asked directly, so say |
  |   | why; nothing persisted |

The card call sites (all six of them) are pinned in
`tests/test_card_provider_resolution.py`; they are not duplicated here.

An **absent or empty** provider still resolves the local-first default in every
one of the four — guard rail 7. That is a normal state, not a misconfiguration.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import pathlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.organization import Organization
from app.models.peppol_transmission import PeppolTransmission
from app.models.positive_pay import PositivePayFile
from app.models.procurement import PunchoutSession
from app.models.quality_inspection import QualityInspection
from app.models.tax_filing import Tax1099Filing
from app.models.vendor import Vendor
from app.services import qms_sync
from app.services.card_adapters import UnknownCardProviderError, get_card_adapter
from app.services.card_adapters.mock_adapter import MockCardAdapter
from app.services.peppol_adapters import UnknownPeppolProviderError, get_peppol_adapter
from app.services.peppol_adapters.mock_adapter import MockPeppolAdapter
from app.services.positive_pay_adapters import (
    UnknownPositivePayFormatError,
    get_positive_pay_formatter,
)
from app.services.positive_pay_adapters.csv_formatter import CsvPositivePayFormatter
from app.services.punchout_adapters import (
    UnknownPunchoutProviderError,
    get_punchout_adapter,
)
from app.services.punchout_adapters.mock_adapter import MockPunchoutAdapter
from app.services.qms_adapters import UnknownQmsProviderError, get_qms_adapter
from app.services.qms_adapters.mock_adapter import MockQMSAdapter
from app.services.tax_filing_adapters import (
    UnknownTaxFilingProviderError,
    get_tax_filing_adapter,
)
from app.services.tax_filing_adapters.mock_adapter import MockTaxFilingAdapter
from app.services.tax_rate_adapters import (
    UnknownTaxRateProviderError,
    get_tax_rate_adapter,
)
from app.services.tax_rate_adapters.mock_adapter import MockTaxRateAdapter
from app.services.tin_validation_adapters import (
    UnknownTinValidationProviderError,
    get_tin_validation_adapter,
)
from app.services.tin_validation_adapters.mock_adapter import MockTINValidationAdapter

TENANT = "a"
SERVICES_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"


# --------------------------------------------------------------------------- #
# (a) + (b) — the four registries, resolver-level
# --------------------------------------------------------------------------- #

#: ``(label, resolve, empty_config, expected_default_cls, named_unknown_config,
#:   error_cls)``. ``empty_config`` is what an org that has configured nothing
#: sends; it must still resolve the local-first default.
REGISTRY_CASES = [
    pytest.param(
        get_card_adapter,
        {"region": "US"},
        # `mock`, not Lithic: an org that has configured nothing must not reach
        # a real issuer. The `REGION_DEFAULTS` preference applies only once a
        # credential for the preferred issuer exists — see
        # `tests/test_card_provider_local_first.py`.
        MockCardAdapter,
        {"provider": "marqeta", "region": "US"},
        UnknownCardProviderError,
        id="card_adapters",
    ),
    pytest.param(
        get_tax_filing_adapter,
        None,
        MockTaxFilingAdapter,
        {"provider": "taxbandits"},
        UnknownTaxFilingProviderError,
        id="tax_filing_adapters",
    ),
    pytest.param(
        get_tin_validation_adapter,
        None,
        MockTINValidationAdapter,
        {"provider": "irs_direct"},
        UnknownTinValidationProviderError,
        id="tin_validation_adapters",
    ),
    pytest.param(
        get_positive_pay_formatter,
        None,
        CsvPositivePayFormatter,
        "wells_fargo_xyz",
        UnknownPositivePayFormatError,
        id="positive_pay_adapters",
    ),
    pytest.param(
        get_peppol_adapter,
        None,
        MockPeppolAdapter,
        {"provider": "storecove_xyz"},
        UnknownPeppolProviderError,
        id="peppol_adapters",
    ),
    pytest.param(
        get_tax_rate_adapter,
        None,
        MockTaxRateAdapter,
        {"rate_provider": "vertex_xyz"},
        UnknownTaxRateProviderError,
        id="tax_rate_adapters",
    ),
    pytest.param(
        get_punchout_adapter,
        None,
        MockPunchoutAdapter,
        {"provider": "ariba_xyz"},
        UnknownPunchoutProviderError,
        id="punchout_adapters",
    ),
    pytest.param(
        get_qms_adapter,
        None,
        MockQMSAdapter,
        {"provider": "labware_xyz"},
        UnknownQmsProviderError,
        id="qms_adapters",
    ),
]


@pytest.mark.parametrize(
    "resolve,empty_config,default_cls,unknown_config,error_cls", REGISTRY_CASES
)
def test_absent_config_still_resolves_the_local_first_default(
    resolve, empty_config, default_cls, unknown_config, error_cls
):
    """Guard rail 7: a fresh clone with no provider configured must work."""
    assert isinstance(resolve(empty_config), default_cls)


@pytest.mark.parametrize(
    "resolve,empty_config,default_cls,unknown_config,error_cls", REGISTRY_CASES
)
def test_named_unknown_provider_raises_instead_of_the_fixture(
    resolve, empty_config, default_cls, unknown_config, error_cls
):
    with pytest.raises(error_cls):
        resolve(unknown_config)


@pytest.mark.parametrize(
    "resolve,empty_config,default_cls,unknown_config,error_cls", REGISTRY_CASES
)
def test_the_bad_name_is_bounded_in_the_error(
    resolve, empty_config, default_cls, unknown_config, error_cls
):
    """The name is admin/caller-supplied config, not PII — but it reaches a log
    line and an HTTP body, so an absurd value must not bloat either."""
    absurd = "z" * 500
    if isinstance(unknown_config, dict):
        # Each family names its own key (`provider` / `rate_provider`); replace
        # whichever one this case carries rather than adding a second.
        oversized = {k: absurd for k in unknown_config}
    else:
        oversized = absurd
    with pytest.raises(error_cls) as exc:
        resolve(oversized)
    assert len(str(exc.value)) < 400


def test_the_registries_populate_themselves_before_refusing():
    """The refusal is only trustworthy if every built-in adapter has had a
    chance to register — a real provider a caller never imported must resolve,
    not raise. Both resolvers import their built-ins on the way in."""
    assert get_tax_filing_adapter({"provider": "tax1099"}).provider_name == "tax1099"
    assert get_tin_validation_adapter({"provider": "tax1099"}).provider_name == "tax1099"


# --------------------------------------------------------------------------- #
# The drift guard — a new registry cannot quietly fail open
# --------------------------------------------------------------------------- #

#: Dispatchers that refuse a NAMED unregistered provider. Adding a registry
#: means adding it here (and making it refuse) or to the list below with a
#: reason.
FAIL_CLOSED_DISPATCHERS = {
    "audit_shipping",
    "card_adapters",
    "enrichment_adapters",
    "erp_adapters",
    "extraction_adapters",
    "financing_adapters",
    "fx_adapters",
    "payment_adapters",
    "peppol_adapters",
    "positive_pay_adapters",
    "punchout_adapters",
    "qms_adapters",
    "sanctions_adapters",
    "tax_filing_adapters",
    "tax_rate_adapters",
    "tin_validation_adapters",
}

#: Dispatchers that still resolve an unknown NAMED provider to their default.
#: Listed with what the fallback actually does, so the next reviewer can judge
#: it rather than inherit an assertion. Every reason here was re-verified
#: against the current resolver and its consumer when the eighth registry
#: (`qms_adapters`) was converted — none is inherited. What they share, and what
#: separates them from the eight that were converted, is that the fallback
#: cannot produce a confident wrong answer about money, a document, or a
#: control: it degrades to a no-op, a log line, or a lower-quality suggestion.
#: A new entry belongs here only if that is true of it too.
FAIL_OPEN_DISPATCHERS = {
    "assistant": (
        "claude-without-a-key → mock is the documented local-first downgrade; a "
        "typo'd name degrades to a deterministic fixture chat answer. Read-only, "
        "no money, nothing persisted claiming otherwise."
    ),
    "billing_adapters": (
        "falls back to mock, but the only path where that is dangerous — the "
        "PUBLIC billing webhook, whose mock parse_webhook verifies no signature "
        "— is refused at boot when the provider is mock or unregistered."
    ),
    "chat_notification_adapters": (
        "an approval notification degrades to the no-network mock. Best-effort "
        "by design; the transition never depends on it."
    ),
    "email_adapters": "falls back to `console`, which logs instead of sending.",
    "embedding_adapters": "falls back to mock vectors — RAG / duplicate-similarity quality only.",
}


def _dispatcher_stems() -> set[str]:
    return {p.parent.name for p in SERVICES_DIR.glob("*/dispatcher.py")}


def test_every_adapter_registry_is_classified():
    """A new adapter family must declare whether it fails closed. Without this
    the §29 rule is a habit rather than a property — the four this file fixes
    were each written *after* §29 landed."""
    on_disk = _dispatcher_stems()
    classified = FAIL_CLOSED_DISPATCHERS | set(FAIL_OPEN_DISPATCHERS)
    unclassified = on_disk - classified
    assert not unclassified, (
        "new adapter registry with no fail-closed decision recorded: "
        f"{sorted(unclassified)} — make its resolver raise on a NAMED unknown "
        "provider and add it to FAIL_CLOSED_DISPATCHERS (decisions.md §29), or "
        "add it to FAIL_OPEN_DISPATCHERS with what its fallback actually does."
    )
    assert not classified - on_disk, (
        f"classified registry no longer on disk: {sorted(classified - on_disk)}"
    )


def _fallback_offenders(path: pathlib.Path) -> list[str]:
    """Every `<registry>.get(x) or <anything>` / `<registry>.get(x, <default>)`
    in `path`. Pure AST — no import, no execution."""
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []

    def _is_registry_get(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and "REGISTRY" in node.func.value.id.upper()
        )

    for node in ast.walk(tree):
        # `_REGISTRY.get(provider) or _REGISTRY["mock"]`
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(_is_registry_get(v) for v in node.values[:-1]):
                offenders.append(f"{path.parent.name}:{node.lineno} (`.get(...) or <default>`)")
        # `_REGISTRY.get(provider, MockAdapter)`
        elif _is_registry_get(node) and len(node.args) > 1:
            offenders.append(f"{path.parent.name}:{node.lineno} (`.get(..., <default>)`)")
    return offenders


def test_no_fail_closed_dispatcher_reintroduces_the_fallback():
    """The behavioural tests above cover four resolvers; this covers all twelve,
    including the ones §29 and §36 fixed, so the shape cannot creep back in
    under a name this file does not import."""
    offenders: list[str] = []
    for stem in sorted(FAIL_CLOSED_DISPATCHERS):
        offenders.extend(_fallback_offenders(SERVICES_DIR / stem / "dispatcher.py"))

    assert not offenders, (
        "a fail-closed adapter registry resolves an unknown provider to a "
        "default again — the mock is not an inert stub (decisions.md §29): " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# (c) — the callers
# --------------------------------------------------------------------------- #


async def _set_provider(realdb, path: list[str], provider: str | None):
    """Point one `Organization.settings` provider at `provider` (None clears)."""
    org_id = realdb.info(TENANT).org_id
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        cursor = settings
        for key in path[:-1]:
            cursor[key] = dict(cursor.get(key) or {})
            cursor = cursor[key]
        if provider is None:
            cursor.pop(path[-1], None)
        else:
            cursor[path[-1]] = {"provider": provider}
        org.settings = settings
        await s.commit()


async def test_filing_route_409s_and_persists_no_filing(realdb):
    """The org named a partner we have no adapter for. Nothing may be recorded:
    a `Tax1099Filing` row would tell the operator their 1099s were transmitted,
    and it would hold the idempotency slot their corrected retry needs."""
    await _set_provider(realdb, ["tax", "filing"], "taxbandits")
    org_id = realdb.info(TENANT).org_id

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/tax/1099/file", json={"year": 2026, "idempotency_key": "unregistered-provider"}
        )

    assert resp.status_code == 409, resp.text
    assert "taxbandits" in resp.text
    assert "mock" in resp.text  # names the registered alternatives

    async with realdb.sessionmaker(TENANT)() as s:
        rows = (
            (await s.execute(select(Tax1099Filing).where(Tax1099Filing.organization_id == org_id)))
            .scalars()
            .all()
        )
    assert rows == [], "a filing row was persisted for a partner that was never reached"


async def test_filing_route_still_files_with_no_provider_configured(realdb):
    """The refusal must not cost the local-first default."""
    await _set_provider(realdb, ["tax", "filing"], None)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/tax/1099/file", json={"year": 2026, "idempotency_key": "default-provider"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider"] == "mock"


async def test_tin_route_409s_and_leaves_the_verification_stamp_alone(realdb):
    """`tin_verified_at` is what the 1099 dashboard reads as "TIN verified" and
    what B-notice / backup-withholding decisions key off. An unresolvable
    provider must leave it — and the stored TIN — exactly as they were."""
    org_id = realdb.info(TENANT).org_id
    stamped = datetime(2026, 1, 2, tzinfo=UTC)
    async with realdb.sessionmaker(TENANT)() as s:
        v = Vendor(
            organization_id=org_id,
            name="Unresolvable Provider Co",
            tax_id="12-3456789",
            is_1099_eligible=True,
            tin_verified_at=stamped,
        )
        s.add(v)
        await s.commit()
        await s.refresh(v)
        vendor_id = v.id

    await _set_provider(realdb, ["tax", "tin_validation"], "irs_direct")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/tax/vendors/{vendor_id}/tin-verify", json={"tax_id": "98-7654321"}
        )

    assert resp.status_code == 409, resp.text
    assert "irs_direct" in resp.text
    # The refusal body carries a config name, never a TIN.
    assert "987654321" not in resp.text
    assert "98-7654321" not in resp.text

    async with realdb.sessionmaker(TENANT)() as s:
        row = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert row.tin_verified_at is not None, "an unresolvable provider cleared the stamp"
        assert row.tin_verified_at.replace(tzinfo=UTC) == stamped
        assert row.tax_id == "12-3456789", "the submitted TIN was written despite the refusal"


async def test_positive_pay_route_422s_and_writes_no_file(realdb):
    """A layout we cannot render must not become a CSV filed under the bank's
    name — the operator would believe the cheque-fraud control is in force."""
    await _set_provider(realdb, ["tax", "filing"], None)  # unrelated; keeps settings shaped
    org_id = realdb.info(TENANT).org_id

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/positive-pay/ach-authorization", json={"bank_format": "wells_fargo_xyz"}
        )

    assert resp.status_code == 422, resp.text
    assert "wells_fargo_xyz" in resp.text
    assert "csv" in resp.text  # names the registered alternatives

    async with realdb.sessionmaker(TENANT)() as s:
        rows = (
            (
                await s.execute(
                    select(PositivePayFile).where(PositivePayFile.organization_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "a positive-pay row was persisted for a layout we never rendered"


async def test_positive_pay_route_still_renders_the_default_layout(realdb):
    """The refusal must not cost the local-first default."""
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post("/api/positive-pay/ach-authorization", json={})
    assert resp.status_code == 201, resp.text
    assert resp.json()["bank_format"] == "csv"


# --------------------------------------------------------------------------- #
# (c) — the callers, round 2: PEPPOL, tax rates, punch-out
# --------------------------------------------------------------------------- #


async def _seed_bis3_invoice(realdb) -> uuid.UUID:
    """A BIS Billing 3.0-conformant approved invoice + the tenant company
    profile the buyer party needs. The send path validates conformance BEFORE
    it resolves the adapter, so a thinner fixture would be refused for the
    wrong reason and never reach the case under test. The due date is part of
    that: BR-CO-25 requires a due date or payment terms once there is an amount
    due."""
    org_id = realdb.info(TENANT).org_id
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        cfg["company"] = {"name": "Buyer Co", "country_code": "DE"}
        org.settings = cfg
        await s.commit()

    inv_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme GmbH",
                vendor_tax_id="DE123456789",
                amount=Decimal("119.00"),
                currency="EUR",
                invoice_date=date(2026, 1, 1),
                due_date=date(2026, 1, 31),
                subtotal=Decimal("100.00"),
                tax_amount=Decimal("19.00"),
                tax_rate=Decimal("19.00"),
                status=InvoiceStatus.approved,
            )
        )
        s.add(
            InvoiceLineItem(
                invoice_id=inv_id,
                line_number=1,
                description="Widget",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                total=Decimal("100.00"),
                tax=Decimal("19.00"),
            )
        )
        await s.commit()
    return inv_id


_PEPPOL_SEND_BODY = {
    "receiver_scheme": "9930",
    "receiver_value": "SUPPLIER123",
    "sender_scheme": "9930",
    "sender_value": "DE000000000",
}


async def test_peppol_send_422s_and_persists_no_transmission(realdb):
    """A transmission row is the record that a legally-significant document
    reached the network. An Access Point we have no adapter for must leave
    none — and must leave the live-transmission slot free, or the honest resend
    comes back `already_sent`."""
    inv_id = await _seed_bis3_invoice(realdb)
    await _set_provider(realdb, ["peppol"], "storecove_xyz")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/peppol-send", json=_PEPPOL_SEND_BODY)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "peppol_provider_not_configured"
    # The PII-free code only — never the admin's raw settings value.
    assert "storecove_xyz" not in resp.text

    async with realdb.sessionmaker(TENANT)() as s:
        rows = (await s.execute(select(PeppolTransmission))).scalars().all()
    assert rows == [], "a transmission row was persisted for a network never reached"


async def test_peppol_inbound_asks_for_redelivery_rather_than_acking(realdb, monkeypatch):
    """§37: an unresolvable provider is OUR failure, not a decision about this
    document. Acking 204 drops a supplier's invoice permanently behind a log
    line; a bodyless 503 leaves it as unprocessed work the AP will retry."""
    from unittest.mock import AsyncMock

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "peppol_inbound_enabled", True)
    monkeypatch.setattr(app_settings, "peppol_inbound_signing_secret", "dev-inbound-secret")
    monkeypatch.setattr("app.services.extraction_dispatch.dispatch_extraction", AsyncMock())
    await _set_provider(realdb, ["peppol"], "storecove_xyz")

    body = json.dumps({"message_id": f"as4-{uuid.uuid4().hex}"}).encode()
    signature = hmac.new(b"dev-inbound-secret", body, hashlib.sha256).hexdigest()

    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.post(
            f"/api/peppol/inbound/{realdb.info(TENANT).org_id}",
            content=body,
            headers={"X-Peppol-Signature": signature},
        )

    assert resp.status_code == 503, resp.text
    assert resp.content == b"", "the redelivery ask must carry no detail and no tenant"

    async with realdb.sessionmaker(TENANT)() as s:
        assert (await s.execute(select(Invoice))).scalars().all() == []
        assert (await s.execute(select(PeppolTransmission))).scalars().all() == []


async def test_vat_route_409s_rather_than_quoting_a_fixture_rate(realdb):
    """`/international-tax/vat` is pure compute and persists nothing, so the
    refusal costs no unwinding — what it buys is that the returned rate is
    never sourced from a provider nobody chose."""
    await _set_provider(realdb, ["tax"], None)
    org_id = realdb.info(TENANT).org_id
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        cfg["tax"] = {**(cfg.get("tax") or {}), "rate_provider": "vertex_xyz"}
        org.settings = cfg
        await s.commit()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/international-tax/vat",
            json={"net_amount": "100.00", "supplier_country": "DE"},
        )

    assert resp.status_code == 409, resp.text
    assert "vertex_xyz" in resp.text
    assert "mock" in resp.text  # names the registered alternatives


async def test_vat_route_still_computes_with_no_provider_configured(realdb):
    """The refusal must not cost the local-first default."""
    await _set_provider(realdb, ["tax"], None)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/international-tax/vat",
            json={"net_amount": "100.00", "supplier_country": "DE"},
        )
    assert resp.status_code == 200, resp.text


async def test_punchout_start_422s_and_persists_no_session(realdb):
    """A `PunchoutSession` row records that a supplier round-trip was started
    and carries the URL the buyer is sent to. A provider we have no adapter for
    must leave neither."""
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        created = await c.post(
            "/api/catalogs",
            json={
                "name": f"PunchVendor-{uuid.uuid4().hex[:8]}",
                "catalog_type": "punchout",
                "punchout_url": "https://supplier.example/punchout",
            },
        )
    assert created.status_code == 201, created.text
    catalog_id = created.json()["id"]

    await _set_provider(realdb, ["punchout"], "ariba_xyz")

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/catalogs/{catalog_id}/punchout/start")

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "punchout_provider_not_configured"
    # The PII-free code only — distinct from `punchout_not_configured`, which
    # means the cXML adapter resolved but has no shared secret.
    assert "ariba_xyz" not in resp.text

    async with realdb.sessionmaker(TENANT)() as s:
        rows = (await s.execute(select(PunchoutSession))).scalars().all()
    assert rows == [], "a punch-out session was persisted for a supplier never contacted"


# --------------------------------------------------------------------------- #
# (c) — the callers, round 3: QMS sync
# --------------------------------------------------------------------------- #


def _fake_control_session(rows: list[tuple]):
    """Stand-in for `control_session_factory()` yielding a fixed org listing."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(all=lambda: rows))
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    return MagicMock(return_value=cm)


@pytest.mark.asyncio
async def test_qms_sweep_counts_a_failure_and_holds_the_cursor():
    """A counted failure, NOT a skip — and `last_synced_at` must not move.

    A skip is indistinguishable from "this tenant had nothing to sync", which
    is the state this control has silently been in. A counted failure reaches
    the consecutive-failure streak and shows `degraded` on
    `GET /api/health/sweeps` (decisions §24), which is the only signal anyone
    gets that the quality leg of the 4-way match stopped being fed.

    Advancing the cursor would be worse than the fallback it replaces: it
    closes a window that was never pulled, so every inspection written during
    the outage is skipped forever once the config is corrected.
    """
    org_id = uuid.uuid4()
    stored: list = []

    async def _record_cursor(oid, *, at):
        stored.append((oid, at))

    with (
        patch.object(
            qms_sync,
            "control_session_factory",
            _fake_control_session(
                [(org_id, "feoh_nonexistent", {"qms": {"provider": "labware_xyz"}})]
            ),
        ),
        patch.object(qms_sync, "_store_cursor", AsyncMock(side_effect=_record_cursor)),
        patch.object(qms_sync.settings, "qms_provider", "mock"),
    ):
        result = await qms_sync.run_qms_sync_once()

    assert result.tenants_scanned == 1, "an opted-in tenant must still be counted as scanned"
    assert result.failures == 1, "an unresolvable provider is a counted failure, not a skip"
    assert result.created == 0 and result.updated == 0
    assert stored == [], "the high-water mark was advanced for a window never pulled"


@pytest.mark.asyncio
async def test_qms_sweep_still_syncs_a_registered_provider():
    """The refusal must not cost a working tenant its sweep — or its cursor."""
    org_id = uuid.uuid4()
    stored: list = []

    async def _record_cursor(oid, *, at):
        stored.append((oid, at))

    summary = {"fetched": 1, "created": 1, "updated": 0, "unchanged": 0, "skipped": 0}
    with (
        patch.object(
            qms_sync,
            "control_session_factory",
            _fake_control_session([(org_id, "feoh_x", {"qms": {"provider": "generic"}})]),
        ),
        patch.object(qms_sync, "_sweep_tenant", AsyncMock(return_value=summary)),
        patch.object(qms_sync, "_store_cursor", AsyncMock(side_effect=_record_cursor)),
        patch.object(qms_sync.settings, "qms_provider", "mock"),
    ):
        result = await qms_sync.run_qms_sync_once()

    assert result.failures == 0
    assert result.created == 1
    assert [oid for oid, _ in stored] == [org_id], "a successful sweep must advance the cursor"


async def test_manual_sync_409s_and_persists_no_inspection(realdb):
    """An operator asked for this pull directly. A clean all-zero summary would
    hide the reason it found nothing — and the fixtures the old fallback
    returned would have landed as `completed` inspections against this tenant's
    real purchase orders."""
    await _set_provider(realdb, ["qms"], "labware_xyz")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post("/api/inspections/sync")

    assert resp.status_code == 409, resp.text
    assert "labware_xyz" in resp.text
    assert "mock" in resp.text  # names the registered alternatives

    async with realdb.sessionmaker(TENANT)() as s:
        rows = (await s.execute(select(QualityInspection))).scalars().all()
    assert rows == [], "a fabricated inspection was persisted for a QMS never reached"


async def test_manual_sync_still_works_for_a_registered_provider(realdb):
    """The refusal must not cost the opted-in path."""
    await _set_provider(realdb, ["qms"], "mock")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post("/api/inspections/sync")

    assert resp.status_code == 200, resp.text
    assert resp.json()["fetched"] >= 1
