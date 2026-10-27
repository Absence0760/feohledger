"""Unit coverage for the ``extract_statement`` extraction-adapter capability.

Covers the optional-capability default (an adapter that hasn't implemented it
reports "not supported" instead of pretending), the shared model-payload
parser, the deterministic offline reader the ``mock`` adapter uses in place of a
model, and both model-backed adapters' happy + failure paths with the network
stubbed.

See ``backend/docs/vendor-statement-reconciliation.md`` § PDF intake.
"""

from __future__ import annotations

import json

import pytest

from app.services.extraction_adapters.base import (
    STATEMENT_REASON_EMPTY_FILE,
    STATEMENT_REASON_NO_LINES,
    STATEMENT_REASON_NO_TEXT_LAYER,
    STATEMENT_REASON_NOT_SUPPORTED,
    STATEMENT_REASON_PROVIDER_ERROR,
    STATEMENT_REASON_UNREADABLE,
    ExtractionAdapter,
    pdf_text_layer,
)
from app.services.extraction_adapters.claude_vision import ClaudeVisionAdapter
from app.services.extraction_adapters.mock_adapter import MockExtractionAdapter
from app.services.extraction_adapters.ollama import OllamaAdapter
from app.services.extraction_adapters.statement_extraction import (
    parse_statement_payload,
    scan_statement_text,
)

_STATEMENT_TEXT = """\
Globex Industrial
Statement of Account
Statement date: 2026-02-28

Invoice     Date          Amount
INV-1001    2026-01-15    1,200.00
INV-1002    01/20/2026    $850.50
INV-1003    2026-02-01    (250.00)

Total                     1,800.50
Page 1 of 1
"""


def _pdf_with_text(text: str) -> bytes:
    """Render a real PDF carrying a text layer, so the reader is exercised
    through PyMuPDF rather than a stub."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 60), text, fontsize=9, fontname="cour")
    return doc.tobytes()


# --------------------------------------------------------------------------- #
# Optional-capability default
# --------------------------------------------------------------------------- #


async def test_base_adapter_reports_statement_extraction_unavailable():
    """The default must say "I can't", never return an empty success — an empty
    success would create a reconciliation run claiming the supplier listed
    nothing."""
    result = await ExtractionAdapter({}).extract_statement(b"%PDF-1.4 whatever")
    assert result.available is False
    assert result.success is False
    assert result.reason == STATEMENT_REASON_NOT_SUPPORTED
    assert result.lines == []


async def test_unimplemented_registered_adapter_inherits_the_default():
    """aws_textract / openai_vision haven't implemented the capability; they
    must inherit the honest default rather than silently mis-reading."""
    from app.services.extraction_adapters.openai_vision import OpenAIVisionAdapter

    result = await OpenAIVisionAdapter({}).extract_statement(b"%PDF-1.4")
    assert result.available is False
    assert result.reason == STATEMENT_REASON_NOT_SUPPORTED


# --------------------------------------------------------------------------- #
# Shared model-payload parser
# --------------------------------------------------------------------------- #


def test_parse_statement_payload_reads_wrapped_and_bare_fields():
    result = parse_statement_payload(
        {
            "lines": [
                {
                    "invoice_number": {"value": "INV-1", "confidence": 0.9},
                    "invoice_date": {"value": "2026-01-15", "confidence": 0.8},
                    "amount": {"value": "1200.00", "confidence": 0.95},
                    "status": {"value": "open", "confidence": 0.4},
                },
                # Bare scalars — some models drop the {value, confidence} wrapper.
                {"invoice_number": "INV-2", "amount": "-250.00"},
            ]
        },
        "claude_vision",
    )
    assert result.available is True
    assert result.success is True
    assert [ln.invoice_number for ln in result.lines] == ["INV-1", "INV-2"]
    assert result.lines[0].amount == "1200.00"
    assert result.lines[0].invoice_date == "2026-01-15"
    # Credits survive as a signed string — the service parses it into Decimal.
    assert result.lines[1].amount == "-250.00"
    assert 0 < result.overall_confidence <= 1


def test_parse_statement_payload_drops_rows_with_nothing_to_match_on():
    result = parse_statement_payload(
        {
            "lines": [
                {"invoice_number": {"value": None}, "amount": {"value": None}},
                {"invoice_number": {"value": "INV-9", "confidence": 0.9}},
                "not-a-dict",
            ]
        },
        "mock",
    )
    assert [ln.invoice_number for ln in result.lines] == ["INV-9"]


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"lines": []}, STATEMENT_REASON_NO_LINES),
        ({"lines": [{"invoice_number": {"value": None}}]}, STATEMENT_REASON_NO_LINES),
        ({"nope": 1}, STATEMENT_REASON_UNREADABLE),
        ([], STATEMENT_REASON_UNREADABLE),
        ("garbage", STATEMENT_REASON_UNREADABLE),
    ],
)
def test_parse_statement_payload_failure_reasons(payload, expected):
    result = parse_statement_payload(payload, "mock")
    assert result.success is False
    assert result.reason == expected
    # `available` stays True — the provider CAN do this, it just didn't work.
    assert result.available is True


def test_parse_statement_payload_never_emits_a_float():
    """Money leaves an adapter as a string; a float here would be a rounding
    bug the reconciliation engine could never undo."""
    result = parse_statement_payload(
        {"lines": [{"invoice_number": "INV-1", "amount": {"value": 1200.10}}]}, "mock"
    )
    assert result.lines[0].amount == "1200.1"
    assert isinstance(result.lines[0].amount, str)


# --------------------------------------------------------------------------- #
# Deterministic offline reader
# --------------------------------------------------------------------------- #


def test_scan_statement_text_reads_rows_and_skips_furniture():
    scan = scan_statement_text(_STATEMENT_TEXT)
    lines = scan.lines
    assert [ln.invoice_number for ln in lines] == ["INV-1001", "INV-1002", "INV-1003"]
    assert [ln.amount for ln in lines] == ["1,200.00", "$850.50", "(250.00)"]
    assert [ln.invoice_date for ln in lines] == ["2026-01-15", "01/20/2026", "2026-02-01"]
    # Header row, the total, the page footer and the address block are all gone.
    # A heuristic read must not claim a model's clearly-printed confidence.
    assert all(ln.confidence < 0.8 for ln in lines)


@pytest.mark.parametrize(
    "row",
    [
        "Invoice     Date          Amount",
        "Total                     1,800.50",
        "Balance forward           500.00",
        "Page 1 of 2",
        "Globex Industrial",
        "Statement date: 2026-02-28",
        "",
        # The aging statement's footer — a money reference followed by FOUR
        # figures. No real open item is printed that way, so it is furniture.
        "Total  1,200.00  850.50  410.00  2,460.50",
    ],
)
def test_scan_statement_text_rejects_non_item_rows(row):
    scan = scan_statement_text(row)
    assert scan.lines == []
    # ...and NOT counted as an ambiguous skip. These lines never looked like an
    # open item, so reporting them would be the noise the split exists to avoid.
    assert scan.ambiguous_skips == 0


def test_scan_statement_text_handles_a_leading_row_counter():
    lines = scan_statement_text("1  INV-1002  01/20/2026  850.50").lines
    assert len(lines) == 1
    assert lines[0].invoice_number == "INV-1002"
    assert lines[0].amount == "850.50"


@pytest.mark.parametrize(
    ("row", "why"),
    [
        ("PO 4502  INV-1  2026-01-15  500.00", "a PO/reference column left of the invoice number"),
        ("0-30  INV-1  2026-01-15  500.00", "an aging-bucket label left of it"),
        ("4502  INV-1  2026-01-15  500.00", "a bare reference number left of it"),
    ],
)
def test_scan_statement_text_skips_a_second_identifier_column(row, why):
    """Two identifier-shaped tokens left of the amount means the match key is a
    guess, so the row is skipped.

    Taking the FIRST one booked the PO/aging label as the invoice number. The
    AMOUNT was read correctly in every one of these, which is why the two
    earlier money-column fixes didn't catch it — the failure is a misrouted
    reconciliation, not a wrong figure, and it is only softened (not removed) by
    the engine's amount+date fallback.
    """
    scan = scan_statement_text(row)
    assert scan.lines == [], why
    # This row DID look like an open item — it is the class a clerk must see.
    assert scan.ambiguous_skips == 1, why


@pytest.mark.parametrize(
    ("row", "number", "amount"),
    [
        # The guard must not swallow the layouts that legitimately carry a
        # digits-only token beside the amount.
        ("100234  2026-01-15  500.00", "100234", "500.00"),  # all-digit invoice no.
        ("INV-1  2026-01-15  1200", "INV-1", "1200"),  # no-cents amount
        ("100234  2026-01-15  1200", "100234", "1200"),  # both at once
        ("Net 30  INV-7  2026-01-15  1,200.00", "INV-7", "1,200.00"),  # terms column
        ("1  INV-1002  01/20/2026  850.50", "INV-1002", "850.50"),  # row counter
    ],
)
def test_scan_statement_text_still_reads_one_identifier_rows(row, number, amount):
    """The other direction: an identifier-shaped guard that demanded a letter,
    or that counted the amount itself, would break each of these."""
    scan = scan_statement_text(row)
    lines = scan.lines
    assert len(lines) == 1, row
    assert scan.ambiguous_skips == 0, row
    assert lines[0].invoice_number == number
    assert lines[0].amount == amount


def test_scan_statement_text_handles_a_row_with_no_date_column():
    lines = scan_statement_text("INV-7001   4200.00").lines
    assert len(lines) == 1
    assert lines[0].invoice_number == "INV-7001"
    assert lines[0].invoice_date is None


@pytest.mark.parametrize(
    "row",
    [
        # A payment-terms column between the date and the balance.
        "INV-1001    2026-01-15    Net 30    1,200.00",
        # An aging-days column in the same place.
        "INV-1001    2026-01-15    45    1,200.00",
    ],
)
def test_scan_statement_text_does_not_read_a_bare_integer_column_as_the_balance(row):
    """`Net 30` / `45 days` are amount-SHAPED but are not money. Reading one as
    the open balance is silently wrong money — the one outcome this reader must
    never produce."""
    lines = scan_statement_text(row).lines
    assert len(lines) == 1
    assert lines[0].amount == "1,200.00"


def test_scan_statement_text_accepts_a_lone_whole_number_balance():
    """A statement that prints no cents still reconciles when the row is
    unambiguous."""
    lines = scan_statement_text("INV-7001   2026-01-15   4200").lines
    assert [ln.amount for ln in lines] == ["4200"]


@pytest.mark.parametrize(
    "row,amount",
    [
        ("INV-1001   2026-01-15   850,00", "850,00"),
        ("INV-1001   2026-01-15   1.234,56", "1.234,56"),
        ("INV-1001   2026-01-15   €1.234,56", "€1.234,56"),
        ("INV-1001   2026-01-15   1.200", "1.200"),
    ],
)
def test_scan_statement_text_reads_a_european_amount_column(row, amount):
    """The reader used to reject `1.234,56` and `1.200` outright — its amount
    pattern only allowed a comma as the grouping separator — so a European
    statement's rows were skipped wholesale. The token is passed through
    VERBATIM; turning it into a Decimal is `parse_amount`'s job, against the
    whole document's convention."""
    lines = scan_statement_text(row).lines
    assert [ln.amount for ln in lines] == [amount]


@pytest.mark.parametrize(
    "row,expected_date,expected_amount",
    [
        ("INV-1001   15.01.2026   1.234,56", "15.01.2026", "1.234,56"),
        ("INV-1001   01.15.2026   850,00", "01.15.2026", "850,00"),
        ("INV-1001   15.01.2026   1.200", "15.01.2026", "1.200"),
    ],
)
def test_scan_statement_text_reads_a_dotted_european_date_row(row, expected_date, expected_amount):
    """A dotted date must be recognised as a DATE, not as a second identifier.

    Unrecognised, `15.01.2026` is identifier-shaped, so the exactly-one-
    identifier rule refused the whole row — every dotted-date European
    statement came back empty. It must also not be read as MONEY: the amount
    pattern pins a thousands group to exactly three digits, which is what keeps
    a three-component date out of the money bucket.
    """
    lines = scan_statement_text(row).lines
    assert len(lines) == 1
    assert lines[0].invoice_number == "INV-1001"
    assert lines[0].invoice_date == expected_date
    assert lines[0].amount == expected_amount


@pytest.mark.parametrize(
    "row",
    [
        # invoice-amount + balance-due: nothing on the row says which is open.
        "INV-1001   2026-01-15   1,200.00   950.00",
        # balance + a trailing aging bucket.
        "INV-1001   2026-01-15   1,200.00   0.00",
        # No cents anywhere and two unlabelled integer columns.
        "INV-7001   2026-01-15   45   4200",
        # MIXED: one decimal token and one bare integer to its RIGHT — an
        # invoice-amount + balance-due row whose balance happens to be a round
        # figure. Counting only within the money bucket would call this
        # unambiguous and return 1200.00, the invoice amount, not the balance.
        "INV-8   2026-03-01   1200.00   800",
        # Same shape with a "days past due" trailing column.
        "INV-9   2026-03-01   1,200.00   45",
    ],
)
def test_scan_statement_text_skips_a_row_with_a_second_numeric_column(row):
    """A second numeric column to the right of the balance is a guess, and a
    guessed open balance is wrong money presented as fact. Skipping leaves our
    invoice visible as `missing_on_their_side` — a difference the clerk
    chases."""
    scan = scan_statement_text(row)
    assert scan.lines == []
    assert scan.ambiguous_skips == 1


# --------------------------------------------------------------------------- #
# Skip classification — "looked like an open item but was ambiguous" vs
# "was never a row". Only the first class is reported; see `StatementScan`.
# --------------------------------------------------------------------------- #

# A realistic aging-bucket statement: every data row prints Current / 1-30 /
# 31-60 / Total, so nothing on the row says which figure is the open balance.
# This is the layout the offline reader cannot resolve honestly.
_AGING_STATEMENT_TEXT = """\
Initech Supplies Ltd
Statement of Open Items
Statement date: 2026-03-31

Invoice     Date          Current     1-30        31-60       Total
INV-2001    2026-03-05    1,200.00    0.00        0.00        1,200.00
INV-2002    2026-02-18    0.00        850.50      0.00        850.50
INV-2003    2026-01-22    0.00        0.00        410.00      410.00

Total                     1,200.00    850.50      410.00      2,460.50
Page 1 of 1
"""

# Two money columns per row (invoice amount + balance due) — the other common
# layout the reader refuses, and one accepted row alongside them so the count
# is provably per-row rather than per-document.
_MIXED_STATEMENT_TEXT = """\
Invoice     Date          Amount      Balance
INV-3001    2026-03-05    1,200.00    900.00
INV-3002    2026-03-06    850.50      850.50
INV-3003    2026-03-07    410.00
"""


def test_a_clean_statement_reports_no_ambiguous_skips():
    """The bar the count has to clear: silence on a document it read fully.

    A counter that fired on blank lines, the vendor block, the column header,
    the total and the page footer would report six skips here — noise that
    trains a reviewer to ignore the number entirely.
    """
    scan = scan_statement_text(_STATEMENT_TEXT)
    assert len(scan.lines) == 3
    assert scan.ambiguous_skips == 0


def test_an_aging_bucket_statement_reports_every_refused_row():
    """The case the count exists for: the run comes back empty and says why.

    Three open items are on this document and none is bookable. Without the
    count the clerk sees a run built entirely from our own ledger and no signal
    that the supplier's side was ever read.
    """
    scan = scan_statement_text(_AGING_STATEMENT_TEXT)
    assert scan.lines == []
    assert scan.ambiguous_skips == 3


def test_a_partially_readable_statement_counts_only_the_refused_rows():
    scan = scan_statement_text(_MIXED_STATEMENT_TEXT)
    assert [ln.invoice_number for ln in scan.lines] == ["INV-3003"]
    assert scan.ambiguous_skips == 2


def test_a_money_reference_is_never_booked_as_an_invoice_number():
    """A row whose reference is itself a figure is never accepted.

    `Current: 1,200.00  Past due: 850.00` used to be: the first money token
    became the "invoice number" and the second the balance, booking a fabricated
    open item no ledger row can ever match. That is invented money — the outcome
    this reader exists not to produce — and it is worse than a skip.
    """
    for row in (
        "Current: 1,200.00  Past due: 850.00",
        "Subtotal 1,200.00 Total 2,050.50",
        "Total  1,200.00  850.50  410.00  2,460.50",
        "Total                     1,800.50",
    ):
        assert scan_statement_text(row).lines == [], row


@pytest.mark.parametrize(
    ("row", "expected", "why"),
    [
        # Exactly one figure follows the money reference — the shape a real open
        # item has, so refusing it has to be announced.
        ("Current: 1,200.00  Past due: 850.00", 1, "summary block"),
        ("Subtotal 1,200.00 Total 2,050.50", 1, "two labelled figures"),
        # Nothing follows: a plain total line, never an open item.
        ("Total                     1,800.50", 0, "statement total"),
        ("Balance forward           500.00", 0, "balance forward"),
        # Four figures follow: an aging footer. Counting it would inflate every
        # aging statement's figure by one — a footer masquerading as a lost row.
        ("Total  1,200.00  850.50  410.00  2,460.50", 0, "aging footer"),
    ],
)
def test_a_money_reference_is_reported_only_when_the_row_looked_like_an_item(row, expected, why):
    """The verdict is deferred to where the row's shape is known.

    Refusing a money-referenced row is right either way, but refusing it
    *silently* is only right when nothing on the row claimed to be an open item.
    """
    assert scan_statement_text(row).ambiguous_skips == expected, why


@pytest.mark.parametrize(
    "reference",
    [
        "INV-1001",  # the common prefixed form
        "100234",  # all-digit — no cents, no separator, so not money
        "1200",
        "0012345678",  # zero-padded
        "INV/2026/001",  # slash-separated
        "2026-001",  # year-sequence, hyphenated
        "2026.001",  # year-sequence, THREE decimals — money allows at most two
        "FR-2026-01",
        "SI-2026.01",  # prefixed, so the decimal can't make it money-shaped
        "#4502",
        "A100.50",  # a letter anywhere disqualifies it as money
    ],
)
def test_a_real_invoice_reference_survives_the_money_test(reference):
    """The counterpart, over the reference formats suppliers actually use.

    A dropped supplier row becomes a false `missing_on_their_side` difference,
    so the money test has to stay narrow. It does: it needs cents, a thousands
    separator, or a currency symbol on a bare numeric token.
    """
    row = f"{reference}  2026-01-15  500.00"
    scan = scan_statement_text(row)
    assert [ln.invoice_number for ln in scan.lines] == [reference], row
    assert scan.ambiguous_skips == 0, row


@pytest.mark.parametrize(
    "reference",
    [
        "2026.01",  # year.sequence
        "5001.01",  # revision / split-invoice suffix
        "24.05",
        "1,234",  # thousands separator
        # European money, and the reader now reads it as such. This used to sit
        # in the list above — but only because the reader could not recognise a
        # European amount at all, so `1.234,56` fell through as "not money". Now
        # that it can, this reference is indistinguishable from €1,234.56 in a
        # summary block's first column, which is exactly what this rule refuses.
        "1.234,56",
    ],
)
def test_a_numeric_reference_shaped_like_money_is_refused_but_reported(reference):
    """The shapes the rule genuinely costs us — refused, and never silently.

    A bare, prefix-less, purely numeric reference carrying cents or a thousands
    separator is indistinguishable from the first column of a summary block, and
    this reader resolves every ambiguity by skipping. But a real supplier does
    use `5001.01`, so dropping it quietly would cost a clerk a genuine open item
    with nothing to chase — the whole failure mode `ambiguous_skips` exists to
    close. It is therefore counted: the run says N rows were skipped and points
    at the CSV / vision alternative that can read them.
    See `docs/vendor-statement-reconciliation.md` § The cost, named.
    """
    scan = scan_statement_text(f"{reference}  2026-01-15  500.00")
    assert scan.lines == []
    assert scan.ambiguous_skips == 1


def test_furniture_around_ambiguous_rows_stays_uncounted():
    """The two classes travel through the same loop and must not blur.

    Same ambiguous rows as above, wrapped in the header/total/footer furniture a
    real page carries — the count must not move.
    """
    bare = scan_statement_text(
        "INV-4001   2026-03-05   1,200.00   900.00\nINV-4002   2026-03-06   850.50   800.00"
    )
    padded = scan_statement_text(
        "Acme Supply Co\n"
        "Statement of Open Items\n"
        "\n"
        "Invoice     Date         Amount      Balance\n"
        "INV-4001   2026-03-05   1,200.00   900.00\n"
        "INV-4002   2026-03-06   850.50   800.00\n"
        "\n"
        "Total                                1,700.00\n"
        "Page 1 of 2\n"
    )
    assert bare.ambiguous_skips == 2
    assert padded.ambiguous_skips == bare.ambiguous_skips


def test_scan_statement_text_keeps_a_bare_integer_column_left_of_the_balance():
    """The counterpart of the rule above: `Net 30` / `45 days` print BEFORE the
    balance and must NOT cause a skip — position is what separates a terms
    column from a second money column, since their shapes are identical."""
    for row in ("INV-1  2026-01-15  Net 30  1,200.00", "INV-2  2026-01-15  45  1,200.00"):
        scan = scan_statement_text(row)
        assert [ln.amount for ln in scan.lines] == ["1,200.00"], row
        assert scan.ambiguous_skips == 0, row


# --------------------------------------------------------------------------- #
# mock adapter — the offline, credential-free path
# --------------------------------------------------------------------------- #


async def test_mock_reads_a_real_pdf_text_layer():
    pdf = _pdf_with_text(_STATEMENT_TEXT)
    result = await MockExtractionAdapter({}).extract_statement(pdf, "s.pdf", "application/pdf")
    assert result.available is True
    assert result.success is True
    assert [ln.invoice_number for ln in result.lines] == ["INV-1001", "INV-1002", "INV-1003"]
    assert result.provider == "mock"


async def test_mock_reads_plain_text_payloads_too():
    result = await MockExtractionAdapter({}).extract_statement(
        _STATEMENT_TEXT.encode(), "s.txt", "text/plain"
    )
    assert result.success is True
    assert len(result.lines) == 3


async def test_mock_gives_up_loudly_on_a_scan_instead_of_inventing_lines():
    """A scanned statement has no text layer. The mock must NOT fall back to a
    fixture — a fabricated open item is money a clerk would chase."""
    scanned = _pdf_with_text("")  # a PDF with no meaningful text
    result = await MockExtractionAdapter({}).extract_statement(scanned, "scan.pdf")
    assert result.available is True
    assert result.success is False
    assert result.reason == STATEMENT_REASON_NO_TEXT_LAYER
    assert result.lines == []


async def test_mock_reports_a_readable_document_that_has_no_open_items():
    text = b"Dear customer,\n\nThank you for your business.\n\nRegards,\nGlobex"
    result = await MockExtractionAdapter({}).extract_statement(text, "letter.txt", "text/plain")
    assert result.success is False
    assert result.reason == STATEMENT_REASON_NO_LINES


async def test_mock_reports_an_empty_upload():
    result = await MockExtractionAdapter({}).extract_statement(b"")
    assert result.reason == STATEMENT_REASON_EMPTY_FILE


def test_pdf_text_layer_returns_none_on_non_pdf_bytes():
    assert pdf_text_layer(b"this is not a pdf") is None


# --------------------------------------------------------------------------- #
# Model-backed adapters (network stubbed)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient used as an async context manager."""

    def __init__(self, response=None, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._raises is not None:
            raise self._raises
        return self._response


def _patch_httpx(monkeypatch, module, client: _FakeClient):
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: client)
    return client


_MODEL_JSON = {
    "lines": [
        {
            "invoice_number": {"value": "INV-1001", "confidence": 0.96},
            "invoice_date": {"value": "2026-01-15", "confidence": 0.9},
            "amount": {"value": "1200.00", "confidence": 0.97},
            "status": {"value": "open", "confidence": 0.5},
        }
    ]
}


async def test_claude_vision_extract_statement_happy_path(monkeypatch):
    from app.services.extraction_adapters import claude_vision as mod

    client = _patch_httpx(
        monkeypatch,
        mod,
        _FakeClient(
            _FakeResponse(
                200,
                {"content": [{"type": "text", "text": f"```json\n{json.dumps(_MODEL_JSON)}\n```"}]},
            )
        ),
    )
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract_statement(
        b"%PDF-1.4 fake", "s.pdf", "application/pdf"
    )
    assert result.success is True
    assert result.lines[0].invoice_number == "INV-1001"
    assert result.lines[0].amount == "1200.00"
    # The statement prompt went out — not the invoice one.
    sent = client.calls[0]["json"]["messages"][0]["content"][1]["text"]
    assert "STATEMENT OF OPEN ITEMS" in sent


async def test_claude_vision_statement_provider_error_stays_out_of_the_reason(monkeypatch):
    from app.services.extraction_adapters import claude_vision as mod

    _patch_httpx(monkeypatch, mod, _FakeClient(_FakeResponse(500, {"error": "boom"})))
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract_statement(b"%PDF-1.4")
    assert result.success is False
    assert result.reason == STATEMENT_REASON_PROVIDER_ERROR
    # The provider's own body must never ride along to the caller's message.
    assert "boom" not in (result.error or "")


async def test_claude_vision_statement_transport_failure_is_not_raised(monkeypatch):
    from app.services.extraction_adapters import claude_vision as mod

    _patch_httpx(monkeypatch, mod, _FakeClient(raises=RuntimeError("connection reset")))
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract_statement(b"%PDF-1.4")
    assert result.reason == STATEMENT_REASON_PROVIDER_ERROR


async def test_claude_vision_statement_unparseable_response(monkeypatch):
    from app.services.extraction_adapters import claude_vision as mod

    _patch_httpx(
        monkeypatch,
        mod,
        _FakeClient(_FakeResponse(200, {"content": [{"type": "text", "text": "sorry, no."}]})),
    )
    result = await ClaudeVisionAdapter({"api_key": "k"}).extract_statement(b"%PDF-1.4")
    assert result.reason == STATEMENT_REASON_UNREADABLE


async def test_ollama_extract_statement_uses_the_text_layer_when_present(monkeypatch):
    from app.services.extraction_adapters import ollama as mod

    monkeypatch.setattr(OllamaAdapter, "_extract_pdf_text", staticmethod(lambda _: _STATEMENT_TEXT))
    client = _patch_httpx(
        monkeypatch,
        mod,
        _FakeClient(_FakeResponse(200, {"message": {"content": json.dumps(_MODEL_JSON)}})),
    )
    result = await OllamaAdapter({}).extract_statement(b"%PDF-1.4", "s.pdf", "application/pdf")
    assert result.success is True
    assert result.lines[0].invoice_number == "INV-1001"
    message = client.calls[0]["json"]["messages"][0]
    assert "images" not in message, "a text-layer PDF must not be sent as page images"
    assert "STATEMENT OF OPEN ITEMS" in message["content"]


async def test_ollama_extract_statement_falls_back_to_page_images(monkeypatch):
    from app.services.extraction_adapters import ollama as mod

    monkeypatch.setattr(OllamaAdapter, "_extract_pdf_text", staticmethod(lambda _: None))
    monkeypatch.setattr(OllamaAdapter, "_pdf_to_images", staticmethod(lambda b, **kw: [b"png"]))
    client = _patch_httpx(
        monkeypatch,
        mod,
        _FakeClient(_FakeResponse(200, {"message": {"content": json.dumps(_MODEL_JSON)}})),
    )
    result = await OllamaAdapter({}).extract_statement(b"%PDF-1.4", "s.pdf", "application/pdf")
    assert result.success is True
    assert client.calls[0]["json"]["messages"][0]["images"]


async def test_ollama_extract_statement_unreadable_pdf(monkeypatch):
    monkeypatch.setattr(OllamaAdapter, "_extract_pdf_text", staticmethod(lambda _: None))
    monkeypatch.setattr(OllamaAdapter, "_pdf_to_images", staticmethod(lambda b, **kw: []))
    result = await OllamaAdapter({}).extract_statement(b"%PDF-1.4", "s.pdf", "application/pdf")
    assert result.success is False
    assert result.reason == STATEMENT_REASON_NO_TEXT_LAYER
