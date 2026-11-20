"""How the discounting surfaces decide WHICH currency their money is in.

`api/discounts._org_currency` used to read `settings.reporting_currency` alone
and fall back to a hardcoded `"USD"`, so it diverged from the canonical
`services/currency_conversion.resolve_reporting_currency` chain in two ways
that both mislabel money:

  * an org configured on `payments.home_currency` (or `invoice_defaults
    .currency`) — with no explicit `reporting_currency` — had every discount
    figure stamped USD; and
  * a non-USD deployment's platform default (`FEOH_REPORTING_CURRENCY_DEFAULT`)
    was ignored, so the last resort was wrong too.

It now delegates. `test_discount_currency_integrity.py` asserts *parity* with
the resolver; this file asserts the ABSOLUTE code each rung of the chain must
produce (parity alone would still pass if both sides were hardcoded to USD),
carries that through the real HTTP surfaces — including the two that PERSIST
the label onto a row — and pins the delegation structurally so a future
re-fork of the chain fails the suite rather than silently mislabelling money.

It also pins the money-typing invariant for the whole `/api/discounts`
surface: every amount stays `Decimal` in Python and on the DB column, and
survives the wire without float drift.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.api import discounts as discounts_api
from app.api.discounts import _org_currency
from app.config import settings
from app.models.discount import (
    OFFER_SCOPE_INVOICE,
    OFFER_SCOPE_VENDOR,
    OFFER_STATUS_CAPTURED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.vendor import Vendor
from app.schemas import discount as discount_schemas
from app.services import discount_opportunities
from app.utils.dates import utc_today

_DISCOUNTS_SOURCE = pathlib.Path(inspect.getfile(discounts_api))


def _org(settings_blob: dict | None):
    """A stand-in for the `Organization` the router receives from `get_tenant`
    — `_org_currency` reads nothing but `.settings`."""

    class _O:
        pass

    o = _O()
    o.settings = settings_blob
    return o


# ---------------------------------------------------------------------------
# The resolution chain, rung by rung, with the expected code stated outright
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings_blob", "expected"),
    [
        # 1. Explicit reporting currency — the only key the old helper read.
        ({"reporting_currency": "EUR"}, "EUR"),
        # 2. `payments.home_currency` — THE divergence. Every discount figure
        #    for this org was stamped USD.
        ({"payments": {"home_currency": "GBP"}}, "GBP"),
        # 3. `invoice_defaults.currency` — the last org-level rung.
        ({"invoice_defaults": {"currency": "CAD"}}, "CAD"),
        # Precedence is top-down, not "first key present wins by accident".
        (
            {
                "reporting_currency": "JPY",
                "payments": {"home_currency": "GBP"},
                "invoice_defaults": {"currency": "CAD"},
            },
            "JPY",
        ),
        (
            {"payments": {"home_currency": "SEK"}, "invoice_defaults": {"currency": "CAD"}},
            "SEK",
        ),
        # A blank / whitespace-only value must not shadow the next candidate —
        # a half-saved settings blob would otherwise downgrade the label.
        ({"reporting_currency": "   ", "payments": {"home_currency": "CHF"}}, "CHF"),
        ({"reporting_currency": None, "invoice_defaults": {"currency": "NOK"}}, "NOK"),
        # Case + surrounding whitespace are normalised, never treated as a
        # different currency (the dashboard filter compares upper-cased).
        ({"reporting_currency": "  eur  "}, "EUR"),
        # A wrong-shaped block degrades to the next rung instead of raising —
        # a misconfigured org must not 500 a dashboard.
        ({"payments": None, "invoice_defaults": {"currency": "AUD"}}, "AUD"),
    ],
)
def test_org_currency_resolves_each_rung_of_the_canonical_chain(settings_blob, expected):
    assert _org_currency(_org(settings_blob)) == expected


@pytest.mark.parametrize("settings_blob", [{}, None, {"payments": {}}, {"invoice_defaults": {}}])
def test_org_currency_falls_through_to_the_platform_default(monkeypatch, settings_blob):
    """The last resort is `FEOH_REPORTING_CURRENCY_DEFAULT`, not a literal
    "USD" — a non-USD deployment's fallback was wrong before."""
    monkeypatch.setattr(settings, "reporting_currency_default", "CHF")
    assert _org_currency(_org(settings_blob)) == "CHF"


def test_org_currency_never_answers_usd_for_a_non_usd_org(monkeypatch):
    """The one-line summary of the whole lead: no configuration a non-USD org
    can hold may come back USD."""
    monkeypatch.setattr(settings, "reporting_currency_default", "EUR")
    for blob in (
        {"reporting_currency": "EUR"},
        {"payments": {"home_currency": "EUR"}},
        {"invoice_defaults": {"currency": "EUR"}},
        {},
        None,
    ):
        assert _org_currency(_org(blob)) != "USD"


# ---------------------------------------------------------------------------
# Structural drift guard — one owner for the chain, no private copy
# ---------------------------------------------------------------------------


def _module_ast() -> ast.Module:
    return ast.parse(_DISCOUNTS_SOURCE.read_text(encoding="utf-8"), filename=str(_DISCOUNTS_SOURCE))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_DISCOUNTS_SOURCE.name}")


def test_org_currency_is_nothing_but_a_delegation():
    """`_org_currency`'s body must be a single `return
    resolve_reporting_currency(org.settings)`.

    The bug was a private re-implementation of the chain living here. Anything
    else in this body — a `.get("reporting_currency")`, an `or "USD"`, a
    second candidate list — is that fork coming back.
    """
    body = [n for n in _function(_module_ast(), "_org_currency").body if not _is_docstring(n)]
    assert len(body) == 1, "the helper does more than delegate"
    stmt = body[0]
    assert isinstance(stmt, ast.Return)
    assert isinstance(stmt.value, ast.Call)
    assert isinstance(stmt.value.func, ast.Name)
    assert stmt.value.func.id == "resolve_reporting_currency"
    assert len(stmt.value.args) == 1 and not stmt.value.keywords
    arg = stmt.value.args[0]
    assert isinstance(arg, ast.Attribute) and arg.attr == "settings"


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def test_the_canonical_resolver_is_called_from_exactly_one_place():
    """One owner. Seven surfaces read the org's currency; all of them go
    through `_org_currency`, so a change of chain is one edit and cannot land
    on some responses and not others."""
    tree = _module_ast()
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_reporting_currency"
    ]
    assert len(calls) == 1, (
        "resolve_reporting_currency is called from more than one place in "
        f"{_DISCOUNTS_SOURCE.name} — route it through _org_currency instead"
    )


#: The ONLY currency-code literals allowed, per module, with the reason. None:
#: `build_opportunity` used to read `offer.currency or "USD"`; it now carries
#: `iso_currency_or_none(offer.currency)`, so a missing or malformed code is
#: `None` and the optimizer reads it as unconvertible (decisions §473). Every
#: figure a caller sees is labelled from `_org_currency`, so the router itself
#: names no code at all.
_ALLOWED_CURRENCY_LITERALS = {
    "api/discounts": {},
    "services/discount_opportunities": {},
}

_CURRENCY_LITERALS = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF"}


def _source_for(label: str) -> pathlib.Path:
    if label == "api/discounts":
        return _DISCOUNTS_SOURCE
    return pathlib.Path(inspect.getfile(discount_opportunities))


@pytest.mark.parametrize("label", sorted(_ALLOWED_CURRENCY_LITERALS))
def test_no_new_hardcoded_currency_literal_reaches_the_money_path(label):
    """A hardcoded code is how the bug got in: `... or "USD"` at the one place
    that decided what every discount figure was denominated in."""
    source = _source_for(label)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    owners: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                owners.setdefault(id(child), node.name)

    found: dict[tuple[str, str], int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in _CURRENCY_LITERALS:
            key = (owners.get(id(node), "<module>"), node.value)
            found[key] = found.get(key, 0) + 1

    assert found == _ALLOWED_CURRENCY_LITERALS[label], (
        f"hardcoded currency literals in {label} changed: "
        f"{found} != {_ALLOWED_CURRENCY_LITERALS[label]}. Resolve the org's currency "
        "through _org_currency (which delegates to resolve_reporting_currency) "
        "rather than naming a code here."
    )


@pytest.mark.parametrize(
    "chain_key", ["reporting_currency", "home_currency", "invoice_defaults", "payments"]
)
def test_the_chains_settings_keys_are_not_re_read_here(chain_key):
    """`api/discounts` must not read the resolution chain's keys itself — that
    is exactly the private copy that drifted."""
    source = _DISCOUNTS_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    hits = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == chain_key
    ]
    assert hits == [], (
        f"api/discounts reads the settings key {chain_key!r} directly; the "
        "chain is owned by services/currency_conversion.resolve_reporting_currency"
    )


# ---------------------------------------------------------------------------
# End-to-end — the resolved code reaches (and is persisted by) the surfaces
# ---------------------------------------------------------------------------

TENANT = "a"


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _set_settings(realdb, org_id, blob: dict) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        org.settings = dict(blob)
        await s.commit()


async def _add_offer(
    mk,
    org_id,
    *,
    status: str,
    currency: str,
    base_amount: str = "1000.00",
    captured_amount: str | None = None,
    tiers: list[dict] | None = None,
) -> uuid.UUID:
    """One offer denominated in `currency`, with the invoice it prices.

    Written directly rather than through the API because the create endpoint
    inherits the invoice's currency — and the point here is a tenant holding
    offers in more than one.
    """
    today = utc_today()
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Globex",
            amount=Decimal(base_amount),
            currency=currency,
            due_date=today + timedelta(days=30),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.flush()
        offer = DiscountOffer(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_id=inv.id,
            scope=OFFER_SCOPE_INVOICE,
            status=status,
            currency=currency,
            base_amount=Decimal(base_amount),
            tiers=tiers or [{"days": 5, "percent": "3.00"}],
            captured_amount=Decimal(captured_amount) if captured_amount else None,
            valid_from=today,
            valid_until=today + timedelta(days=60),
        )
        s.add(offer)
        await s.commit()
        return offer.id


async def _add_vendor(mk, org_id) -> str:
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=f"Globex {uuid.uuid4().hex[:6]}",
            entity_id=await _default_entity_id(s),
        )
        s.add(v)
        await s.commit()
        return str(v.id)


_CHAIN_CASES = [
    ({"reporting_currency": "EUR"}, "EUR"),
    ({"payments": {"home_currency": "EUR"}}, "EUR"),
    ({"invoice_defaults": {"currency": "EUR"}}, "EUR"),
]


@pytest.mark.parametrize(("settings_blob", "expected"), _CHAIN_CASES)
async def test_dashboard_labels_and_filters_by_the_resolved_currency(
    realdb, settings_blob, expected
):
    """Every rung of the chain, through the real endpoint: the EUR org's
    dashboard is labelled EUR and counts the EUR rows — the USD row is the one
    excluded. Pre-fix, two of these three configurations reported USD and
    summed the USD row into the headline instead."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, settings_blob)
    await _add_offer(
        mk, org_id, status=OFFER_STATUS_CAPTURED, currency="EUR", captured_amount="500.00"
    )
    await _add_offer(
        mk, org_id, status=OFFER_STATUS_CAPTURED, currency="USD", captured_amount="100.00"
    )

    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    assert body["currency"] == expected
    assert Decimal(str(body["captured_amount"])) == Decimal("500.00")
    assert body["captured_count"] == 1
    assert body["excluded_captured_count"] == 1


@pytest.mark.parametrize(("settings_blob", "expected"), _CHAIN_CASES)
async def test_the_optimizer_response_declares_the_resolved_currency(
    realdb, settings_blob, expected
):
    """`POST /optimize`'s totals are sums, so its declared `currency` decides
    which offers are summed at all — an offer in any other one comes back
    `unconvertible`. A mislabel here silently drops (or wrongly admits) real
    money from the recommendation totals."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, settings_blob)
    await _add_offer(mk, org_id, status=OFFER_STATUS_OFFERED, currency="EUR")
    await _add_offer(mk, org_id, status=OFFER_STATUS_OFFERED, currency="USD")

    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.post("/api/discounts/optimize", json={})).json()

    assert body["currency"] == expected
    assert body["unconvertible_count"] == 1
    # 3% of the EUR 1000 offer, and nothing from the USD one.
    assert Decimal(str(body["total_savings_selected"])) == Decimal("30.00")


@pytest.mark.parametrize(("settings_blob", "expected"), _CHAIN_CASES)
async def test_a_created_vendor_scoped_offer_is_stamped_with_the_resolved_currency(
    realdb, settings_blob, expected
):
    """The mislabel that PERSISTS. A vendor-scoped offer carries no invoice to
    inherit a currency from, so `POST /offers` stamps it from the org — and
    that row is what every later figure, filter and exclusion count reads.
    Pre-fix this wrote USD onto a EUR org's offer."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, settings_blob)
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": OFFER_SCOPE_VENDOR,
                "vendor_id": vendor_id,
                "base_amount": "5000.00",
                "tiers": [{"days": 10, "percent": "2.00"}],
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["currency"] == expected

    # And on the row, not just in the response.
    async with mk() as s:
        row = (
            await s.execute(
                select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(resp.json()["id"]))
            )
        ).scalar_one()
        assert row.currency == expected


async def test_a_bulk_negotiation_is_stamped_with_the_resolved_currency(realdb):
    """`POST /bulk-negotiate` spans a vendor's open invoices and has no single
    invoice currency to inherit either, so it stamps from the org too."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, {"payments": {"home_currency": "EUR"}})
    vendor_id = await _add_vendor(mk, org_id)
    async with mk() as s:
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=await _default_entity_id(s),
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Globex",
                vendor_id=uuid.UUID(vendor_id),
                amount=Decimal("2000.00"),
                currency="EUR",
                due_date=utc_today() + timedelta(days=30),
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/bulk-negotiate",
            json={"vendor_id": vendor_id, "tiers": [{"days": 10, "percent": "2.00"}]},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["currency"] == "EUR"


async def test_an_explicit_body_currency_still_wins_over_the_org_default(realdb):
    """The org's currency is the FALLBACK for a new offer, not an override — a
    supplier proposing a GBP discount must not have it relabelled."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, {"reporting_currency": "EUR"})
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": OFFER_SCOPE_VENDOR,
                "vendor_id": vendor_id,
                "base_amount": "5000.00",
                "currency": "gbp",
                "tiers": [{"days": 10, "percent": "2.00"}],
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["currency"] == "GBP"


async def test_the_platform_default_is_the_last_resort_end_to_end(realdb, monkeypatch):
    """An org that declares nothing takes `FEOH_REPORTING_CURRENCY_DEFAULT` —
    so a CHF deployment's dashboard is CHF, not the old literal USD."""
    monkeypatch.setattr(settings, "reporting_currency_default", "CHF")
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, {})
    await _add_offer(
        mk, org_id, status=OFFER_STATUS_CAPTURED, currency="CHF", captured_amount="80.00"
    )
    await _add_offer(
        mk, org_id, status=OFFER_STATUS_CAPTURED, currency="USD", captured_amount="90.00"
    )

    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    assert body["currency"] == "CHF"
    assert Decimal(str(body["captured_amount"])) == Decimal("80.00")
    assert body["excluded_captured_count"] == 1


async def test_no_discount_surface_reports_usd_for_a_non_usd_org(realdb):
    """The lead in one test, across every response that declares a currency."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, {"payments": {"home_currency": "SEK"}})
    await _add_offer(mk, org_id, status=OFFER_STATUS_OFFERED, currency="SEK")

    async with realdb.client(key=TENANT, role="cfo") as c:
        dash = (await c.get("/api/discounts/dashboard")).json()
        opt = (await c.post("/api/discounts/optimize", json={})).json()

    assert dash["currency"] == "SEK"
    assert opt["currency"] == "SEK"
    # Nothing is excluded: the org's own offers ARE in its reporting currency.
    assert dash["excluded_captured_count"] == 0
    assert dash["excluded_missed_count"] == 0
    assert dash["unconvertible_offer_count"] == 0
    assert opt["unconvertible_count"] == 0


# ---------------------------------------------------------------------------
# Money typing — Decimal in Python, exact on the wire
# ---------------------------------------------------------------------------


def test_no_money_field_on_a_discount_schema_is_typed_float():
    """Money is `Decimal` in Python (project invariant). `app/schemas/money.py`
    serialises it to a JSON *number* deliberately, at write time only — a field
    ANNOTATED `float` would instead lose exactness before it ever reached the
    serialiser."""
    offenders: list[str] = []
    for name in dir(discount_schemas):
        obj = getattr(discount_schemas, name)
        if not (isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel):
            continue
        for field_name, field in obj.model_fields.items():
            annotation = repr(field.annotation)
            if "float" in annotation:
                offenders.append(f"{name}.{field_name}: {annotation}")
    assert offenders == [], f"float-typed field(s) on the discount schemas: {offenders}"


async def test_the_dashboards_missed_total_accumulates_exactly(realdb):
    """`missed_amount` is accumulated in PYTHON, one offer at a time — the one
    money figure on this response that is not a Postgres `Numeric` SUM.

    Thirty offers each missing exactly one cent must report 0.30. The same
    accumulation in float lands on 0.3000000000000001, so this fails loudly if
    the running total is ever retyped.
    """
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, {"reporting_currency": "USD"})
    for _ in range(30):
        await _add_offer(
            mk,
            org_id,
            status=OFFER_STATUS_DECLINED,
            currency="USD",
            base_amount="0.10",
            tiers=[{"days": 5, "percent": "10.00"}],
        )

    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    assert body["missed_count"] == 30
    assert Decimal(str(body["missed_amount"])) == Decimal("0.30")


async def test_captured_cents_survive_the_wire_unrounded(realdb):
    """A captured total carrying cents must come back as the same exact
    figure, to the cent."""
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    await _set_settings(realdb, org_id, {"reporting_currency": "USD"})
    for amount in ("1234.56", "0.07", "999999999.99"):
        await _add_offer(
            mk, org_id, status=OFFER_STATUS_CAPTURED, currency="USD", captured_amount=amount
        )

    async with realdb.client(key=TENANT, role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    # 1234.56 + 0.07 + 999999999.99 — eleven significant digits, so a float
    # hop that rounded anywhere would show up in the cents.
    assert Decimal(str(body["captured_amount"])) == Decimal("1000001234.62")
