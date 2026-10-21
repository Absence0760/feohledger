"""Golden extraction contract tests — real-shaped Claude answers, replayed.

Every other extraction test builds its ``ExtractionResult`` by hand or runs the
mock adapter, so the only conventions the pipeline was ever tested against were
the mock's own: tax-inclusive lines, bare ``"1234.56"`` strings, USD. Issue #566
found what that hid — the self-check flagged every tax-exclusive invoice (#572)
— and #575 asked for the cure: a small set of golden invoice PDFs, each with the
answer a current Claude model gives for it, replayed through the real
``claude_vision`` parser, the real cleaners and the real self-correction pass.

What runs for real here: the adapter's request build, httpx itself (only the
transport is replaced, by ``httpx.MockTransport``), the response-block walk, the
fence strip, the JSON parse, ``_apply_extraction``'s cleaners, the line-item
cleaner run_extraction persists through, the decimal-convention vote, the
self-correction pass and the payment gate's line-total reconciliation. Nothing
calls Anthropic, and the fixtures hold no real customer data.

The expected values are written out literally below — never derived from the
fixture generator — so a fixture change that alters what a page means fails
here instead of re-deriving its own expectations. How to add a case:
``backend/docs/ai-extraction.md`` § Golden extraction cases.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.models.invoice import Invoice
from app.services.extraction import (
    _apply_extraction,
    extracted_line_values,
    extraction_amount_convention,
)
from app.services.extraction_adapters.base import pdf_text_layer
from app.services.extraction_adapters.claude_vision import ClaudeVisionAdapter
from app.services.extraction_self_correction import run_self_correction
from app.services.invoice_warnings import reconcile_line_totals

GOLDEN = Path(__file__).parent / "fixtures" / "extraction_golden"

D = Decimal
_MONEY = ("amount", "subtotal", "tax_amount", "tax_rate", "discount_amount", "shipping_amount")
_LINE_MONEY = ("quantity", "unit_price", "tax", "total")


@dataclass
class Golden:
    """What a correct pipeline must make of one golden case."""

    name: str
    header: dict
    # (line_number, item_code, description, quantity, unit_price, tax, total)
    lines: list[tuple]
    overall_confidence: float
    org_settings: dict = field(default_factory=dict)
    # The self-correction checks that must fire — exactly these, no others.
    flagged: frozenset[str] = frozenset()
    scanned: bool = False
    pages: int = 1


CORRECT_CASES = [
    Golden(
        name="us_tax_exclusive",
        header={
            "invoice_number": "NW-10482",
            "vendor_name": "Northwind Office Supply Co.",
            "currency": "USD",
            "amount": D("3554.09"),
            "subtotal": D("3204.70"),
            "tax_amount": D("264.39"),
            "tax_rate": D("8.25"),
            "shipping_amount": D("85.00"),
            "discount_amount": None,
            "invoice_date": date(2026, 9, 14),
            "due_date": date(2026, 10, 14),
            "po_number": "PO-20417",
            "payment_terms": "Net 30",
        },
        lines=[
            (1, None, "Ergonomic task chair", D("4"), D("289.00"), None, D("1156.00")),
            (2, None, "Standing desk frame", D("2"), D("649.50"), None, D("1299.00")),
            (3, None, "Monitor arm, dual", D("6"), D("124.95"), None, D("749.70")),
        ],
        overall_confidence=(0.98 + 0.97 + 0.97 + 0.97 + 0.95) / 5,
    ),
    Golden(
        name="gb_vat_inclusive",
        header={
            "invoice_number": "HF-2291",
            "vendor_name": "Harrow & Finch Facilities Ltd",
            "vendor_tax_id": "GB 999 9999 73",
            "currency": "GBP",
            "amount": D("2238.00"),
            "subtotal": D("1865.00"),
            "tax_amount": D("373.00"),
            "tax_rate": D("20"),
            "shipping_amount": None,
            "discount_amount": None,
            "invoice_date": date(2026, 9, 3),
            "due_date": date(2026, 10, 3),
            "po_number": None,
        },
        # Net unit price, the line's VAT, the GROSS total — Net/VAT/Gross columns.
        lines=[
            (1, None, "Quarterly HVAC service", D("1"), D("1450.00"), D("290.00"), D("1740.00")),
            (2, None, "Filter replacement", D("12"), D("18.75"), D("45.00"), D("270.00")),
            (3, None, "Call-out charge", D("2"), D("95.00"), D("38.00"), D("228.00")),
        ],
        overall_confidence=(0.97 + 0.96 + 0.96 + 0.95 + 0.93) / 5,
    ),
    Golden(
        name="eur_multipage",
        header={
            "invoice_number": "RL-2026-0917",
            "vendor_name": "Rheinwerk Logistik GmbH",
            "vendor_tax_id": "DE999999999",
            "currency": "EUR",
            # Returned as "EUR 8.739,84" — the ISO code transcribed with the figure.
            "amount": D("8739.84"),
            "subtotal": D("7344.40"),
            "tax_amount": D("1395.44"),
            "tax_rate": D("19"),
            "invoice_date": date(2026, 9, 30),
            "due_date": date(2026, 10, 30),
        },
        lines=[
            (1, None, "Stückgut Köln–Düsseldorf", D("1"), D("1180.00"), None, D("1180.00")),
            (2, None, "Palettentausch Euro-Palette", D("24"), D("14.50"), None, D("348.00")),
            (3, None, "Lagergeld KW 36", D("1"), D("640.00"), None, D("640.00")),
            (4, None, "Lagergeld KW 37", D("1"), D("640.00"), None, D("640.00")),
            (5, None, "Kommissionierung (Std.)", D("12.5"), D("42.00"), None, D("525.00")),
            (6, None, "Expresszuschlag", D("1"), D("95.00"), None, D("95.00")),
            (7, None, "Maut-Umlage", D("1"), D("118.40"), None, D("118.40")),
            (8, None, "Verpackungsmaterial", D("30"), D("3.85"), None, D("115.50")),
            (9, None, "Lagergeld KW 38", D("1"), D("640.00"), None, D("640.00")),
            (10, None, "Lagergeld KW 39", D("1"), D("640.00"), None, D("640.00")),
            # Page 2 — after the "Übertrag 4.941,90 €" carry-forward row, which
            # is page furniture and must not become a line.
            (11, None, "Stückgut Düsseldorf–Essen", D("1"), D("1075.00"), None, D("1075.00")),
            (12, None, "Inventur Sonderleistung (Std.)", D("6"), D("58.00"), None, D("348.00")),
            (13, None, "Gefahrgutzuschlag", D("2"), D("210.00"), None, D("420.00")),
            (14, None, "Avisierung", D("8"), D("12.00"), None, D("96.00")),
            (15, None, "Kommissionierung (Std.)", D("7.5"), D("42.00"), None, D("315.00")),
            (16, None, "Retourenabwicklung", D("15"), D("9.90"), None, D("148.50")),
        ],
        overall_confidence=(0.97 + 0.97 + 0.95 + 0.96 + 0.94) / 5,
        pages=2,
    ),
    Golden(
        name="jpy_zero_decimal",
        header={
            "invoice_number": "SP-55810",
            "vendor_name": "Sakura Precision Parts K.K.",
            "currency": "JPY",
            # "¥176,440": every token is the ambiguous one-comma-three-digit
            # shape, so nothing votes and it must still read as grouping.
            "amount": D("176440"),
            "subtotal": D("160400"),
            "tax_amount": D("16040"),
            "tax_rate": D("10"),
            "invoice_date": date(2026, 9, 25),
            # Not on the page — the model says null, and null it stays.
            "due_date": None,
            "po_number": None,
            "payment_terms": None,
        },
        lines=[
            (1, None, "Bearing unit 6204-ZZ", D("40"), D("1850"), None, D("74000")),
            (2, None, "Shaft coupling 25mm", D("12"), D("4200"), None, D("50400")),
            (3, None, "Machining fee", D("1"), D("36000"), None, D("36000")),
        ],
        # due_date is null, so only four key fields carry a confidence.
        overall_confidence=(0.97 + 0.95 + 0.96 + 0.96) / 4,
    ),
    Golden(
        name="us_scanned",
        header={
            "invoice_number": "BG-7731",
            "vendor_name": "Bluegill Catering LLC",
            "currency": "USD",
            # JSON numbers, not strings — read exactly, never through a float sum.
            "amount": D("1249.49"),
            "subtotal": D("1297.50"),
            "tax_amount": D("81.74"),
            "tax_rate": D("7"),
            "discount_amount": D("129.75"),
            "shipping_amount": None,
            "invoice_date": date(2026, 9, 18),
            "due_date": date(2026, 10, 3),
            # "N/A" and the leaked "string or null" are sentinels, not values.
            "po_number": None,
            "reference_number": None,
        },
        # A numeric SKU (JSON 4411) is still a code, i.e. text.
        lines=[
            (1, "4411", "Lunch buffet (per guest)", D("45"), D("24.50"), None, D("1102.50")),
            (2, "2030", "Coffee service", D("3"), D("65.00"), None, D("195.00")),
        ],
        overall_confidence=(0.86 + 0.88 + 0.84 + 0.85 + 0.82) / 5,
        scanned=True,
    ),
]

INCONSISTENT = Golden(
    name="inconsistent_gb",
    # Day-first dates: the org is a UK company.
    org_settings={"company": {"companies_house_number": "09999999"}},
    header={
        "invoice_number": "KP-0388",
        "currency": "GBP",
        # What the model READ (wrong), faithfully stored — the flags are the point.
        "amount": D("2843.25"),
        "subtotal": D("2310.00"),
        "tax_amount": D("173.25"),
        "invoice_date": date(2026, 10, 28),
        "due_date": date(2026, 9, 28),
    },
    lines=[
        (1, None, "Brochure print run (A4, 2,000)", D("1"), D("1200.00"), None, D("1200.00")),
        (2, None, "Design proof rounds", D("5"), D("103.00"), None, D("650.00")),
    ],
    overall_confidence=0.0,  # not asserted for the flagged case
    flagged=frozenset(
        {"total_reconciliation", "date_ordering", "line_items_sum", "line_item_math"}
    ),
)

ALL_CASES = [*CORRECT_CASES, INCONSISTENT]


# --------------------------------------------------------------------------- #
# Replay harness
# --------------------------------------------------------------------------- #


def _load(case: Golden) -> tuple[bytes, dict]:
    pdf = (GOLDEN / f"{case.name}.pdf").read_bytes()
    response = json.loads((GOLDEN / f"{case.name}.response.json").read_text(encoding="utf-8"))
    return pdf, response


def _replay(monkeypatch, response: dict) -> list[httpx.Request]:
    """Answer the adapter's POST with ``response`` at httpx's transport layer.

    The adapter's own ``httpx.AsyncClient`` is constructed exactly as in
    production; only its transport is swapped, so httpx still serialises the
    request and builds a real ``httpx.Response`` the adapter decodes itself.
    """
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response)

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


async def _run(monkeypatch, case: Golden):
    pdf, response = _load(case)
    seen = _replay(monkeypatch, response)
    adapter = ClaudeVisionAdapter({"api_key": "sk-ant-golden-test", "model": "claude-sonnet-5-5"})
    result = await adapter.extract(file_bytes=pdf, mime_type="application/pdf")

    # The golden PDF is what went out, as a document block.
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    document = body["messages"][0]["content"][0]
    assert document["type"] == "document"
    assert base64.b64decode(document["source"]["data"]) == pdf

    assert result.success, result.error
    convention = extraction_amount_convention(result)
    invoice = Invoice()
    _apply_extraction(invoice, result, convention, org_settings=case.org_settings)
    lines = [extracted_line_values(li, convention) for li in result.line_items]
    return result, invoice, lines


# --------------------------------------------------------------------------- #
# The fixtures are what they claim to be
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.name)
def test_golden_pdf_matches_its_recorded_answer(case: Golden):
    """A PDF and a response that drifted apart would make every assertion moot."""
    import pymupdf

    pdf, _ = _load(case)
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    assert doc.page_count == case.pages
    text = pdf_text_layer(pdf)
    if case.scanned:
        # A scan: no text layer, so a text-mode adapter would have nothing.
        assert text is None
        return
    assert text is not None and case.header["invoice_number"] in text
    for line in case.lines:
        assert line[2] in text
    if case.pages > 1:
        # The lines really do span the pages.
        assert case.lines[0][2] in doc[0].get_text()
        assert case.lines[-1][2] in doc[-1].get_text()
        assert case.lines[-1][2] not in doc[0].get_text()


# --------------------------------------------------------------------------- #
# A correct read of a correct invoice: exact values, no flags
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", CORRECT_CASES, ids=lambda c: c.name)
async def test_golden_header_is_extracted_exactly(monkeypatch, case: Golden):
    _, invoice, _ = await _run(monkeypatch, case)
    for name, expected in case.header.items():
        actual = getattr(invoice, name)
        assert actual == expected, f"{name}: {actual!r} != {expected!r}"
        if name in _MONEY and expected is not None:
            # Exactly a Decimal — a float that happens to compare equal is the bug.
            assert type(actual) is Decimal, f"{name} is {type(actual).__name__}"


@pytest.mark.parametrize("case", CORRECT_CASES, ids=lambda c: c.name)
async def test_golden_line_items_are_extracted_exactly(monkeypatch, case: Golden):
    _, _, lines = await _run(monkeypatch, case)
    got = [
        (
            v["line_number"],
            v["item_code"],
            v["description"],
            v["quantity"],
            v["unit_price"],
            v["tax"],
            v["total"],
        )
        for v in lines
    ]
    assert got == case.lines
    for v in lines:
        for name in _LINE_MONEY:
            if v[name] is not None:
                assert type(v[name]) is Decimal, f"{name} is {type(v[name]).__name__}"


@pytest.mark.parametrize("case", CORRECT_CASES, ids=lambda c: c.name)
async def test_golden_correct_invoice_raises_no_self_check_flag(monkeypatch, case: Golden):
    """The #572 defect class: a correct invoice must come through clean."""
    result, invoice, lines = await _run(monkeypatch, case)
    before = {
        name: getattr(result, name).confidence
        for name in ("amount", "subtotal", "tax_amount", "due_date")
    }
    line_before = [li.total.confidence for li in result.line_items]

    report = await run_self_correction(result, case.org_settings)

    assert report.violations == []
    assert report.confidence_penalties == {}
    assert {n: getattr(result, n).confidence for n in before} == before
    assert [li.total.confidence for li in result.line_items] == line_before
    assert result.overall_confidence == pytest.approx(case.overall_confidence)
    assert not invoice.warnings

    # And the payment gate agrees: the stored lines reconcile with the header.
    line_total = sum(v["total"] for v in lines)
    assert reconcile_line_totals(invoice, line_total) is None


# --------------------------------------------------------------------------- #
# A wrong read: every inconsistency is flagged, and the gate agrees
# --------------------------------------------------------------------------- #


async def test_golden_inconsistent_read_is_flagged(monkeypatch):
    case = INCONSISTENT
    result, invoice, lines = await _run(monkeypatch, case)

    # The misread values are stored as read — flagging is the response, not
    # silent correction.
    for name, expected in case.header.items():
        assert getattr(invoice, name) == expected, name
    assert [
        (
            v["line_number"],
            v["item_code"],
            v["description"],
            v["quantity"],
            v["unit_price"],
            v["tax"],
            v["total"],
        )
        for v in lines
    ] == case.lines

    amount_before = result.amount.confidence
    report = await run_self_correction(result, case.org_settings)

    assert {v["check"] for v in report.violations} == case.flagged
    assert all(v["code"].startswith("self_correction_") for v in report.violations)
    assert result.amount.confidence < amount_before
    assert "due_date" in report.confidence_penalties
    assert "line_items[1].total" in report.confidence_penalties

    # The payment gate reads the same lines the same way and refuses them too.
    mismatch = reconcile_line_totals(invoice, sum(v["total"] for v in lines))
    assert mismatch is not None
    assert mismatch["line_items_total"] == "1850.00"


# --------------------------------------------------------------------------- #
# The printed ISO code is currency evidence (decisions §308)
# --------------------------------------------------------------------------- #


def _with_currency_field(response: dict, value: str | None) -> dict:
    """``response`` with the model's ``currency`` answer replaced by ``value``.

    The eur_multipage read prints ``EUR 8.739,84`` on the total; only the
    separate currency field changes, so the replay is otherwise byte-for-byte
    the recorded one.
    """
    response = json.loads(json.dumps(response))
    block = response["content"][0]
    body = json.loads(block["text"])
    body["currency"] = {"value": value, "confidence": 0.0 if value is None else 0.97}
    block["text"] = json.dumps(body)
    return response


async def _run_response(monkeypatch, case: Golden, response: dict):
    pdf, _ = _load(case)
    _replay(monkeypatch, response)
    adapter = ClaudeVisionAdapter({"api_key": "sk-ant-golden-test", "model": "claude-sonnet-5-5"})
    result = await adapter.extract(file_bytes=pdf, mime_type="application/pdf")
    assert result.success, result.error
    invoice = Invoice(amount=D("0"), currency="USD")  # the upload placeholder
    _apply_extraction(invoice, result, extraction_amount_convention(result))
    return result, invoice


_EUR = next(c for c in CORRECT_CASES if c.name == "eur_multipage")


async def test_golden_printed_code_supplies_a_missing_currency(monkeypatch):
    _, response = _load(_EUR)
    _, invoice = await _run_response(monkeypatch, _EUR, _with_currency_field(response, None))
    assert invoice.amount == _EUR.header["amount"]
    # From "EUR 8.739,84" — not the placeholder's USD.
    assert invoice.currency == "EUR"


async def test_golden_printed_code_contradicting_the_field_leaves_money_unread(monkeypatch):
    _, response = _load(_EUR)
    result, invoice = await _run_response(monkeypatch, _EUR, _with_currency_field(response, "USD"))
    assert invoice.amount == D("0")
    assert invoice.subtotal is None and invoice.tax_amount is None
    assert invoice.currency == "USD"  # the placeholder, untouched — not "read as USD"

    report = await run_self_correction(result)
    codes = {v["code"] for v in report.violations}
    assert "self_correction_currency_conflict" in codes
