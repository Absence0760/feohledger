"""Regenerate the golden extraction fixtures: one invoice PDF + one recorded
Claude Messages API response per case.

    cd backend && .venv/bin/python tests/fixtures/extraction_golden/generate.py

Every vendor, address, tax id and figure here is invented. Nothing was read off
a real invoice and nothing here was sent to the Anthropic API: each
``<case>.response.json`` is hand-recorded in the exact shape
``POST /v1/messages`` returns, carrying the answer a current Claude model gives
for that page, quirks included (thousands separators, currency symbols and
codes, European decimal commas, a zero-decimal currency, ``null`` for a field
the page does not have, a ``thinking`` block before the text, a fenced JSON
body, JSON numbers where the prompt asked for strings, a sentinel string).

The PDF and the response come from the same case definition so they cannot
disagree about what the page says. The EXPECTED values do not come from here:
``tests/test_extraction_golden.py`` states them literally, so a change to this
file that alters what a page means fails that test instead of silently
re-deriving its expectations.

The ``inconsistent_gb`` response is deliberately wrong (a misread total, a
dropped line, a misread unit price, swapped dates) — the case that must be
flagged. Every other response is a correct read of a correct invoice, which
must produce no warning at all.

See ``backend/docs/ai-extraction.md`` § Golden extraction cases.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pymupdf
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent
MODEL = "claude-sonnet-5-5"
CENT = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def us(value: Decimal) -> str:
    return f"{value:,.2f}"


def eu(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def yen(value: Decimal) -> str:
    return f"{value:,.0f}"


def qty_eu(value: Decimal) -> str:
    return str(value).replace(".", ",")


def f(value, confidence: float) -> dict:
    return {"value": value, "confidence": confidence}


# --------------------------------------------------------------------------- #
# PDF rendering
# --------------------------------------------------------------------------- #


def _render(path: Path, pages: list[list[tuple[float, float, str, str]]]) -> None:
    """Draw ``pages`` — each a list of (x, y, font, text) — into ``path``.

    ``invariant=1`` drops ReportLab's timestamp and random document id, so a
    regeneration with no content change is a byte-identical file.
    """
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle("")
    c.setAuthor("")
    for items in pages:
        for x, y, font, text in items:
            size = 16 if font == "title" else 9
            c.setFont("Helvetica-Bold" if font in ("title", "bold") else "Helvetica", size)
            c.drawString(x, y, text)
        c.showPage()
    c.save()


def _rasterise(path: Path) -> None:
    """Replace ``path`` with an image-only copy — a scan has no text layer."""
    src = pymupdf.open(path)
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=110, colorspace=pymupdf.csGRAY)
        jpeg = pix.tobytes("jpg", jpg_quality=60)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=jpeg)
    out.set_metadata({})
    src.close()
    out.save(path, garbage=4, deflate=True, no_new_id=True)
    out.close()


def _page(header: list[str], party: list[str], cols, rows, totals, *, y0=800):
    """Lay out one invoice page as (x, y, font, text) tuples."""
    items: list[tuple[float, float, str, str]] = []
    y = y0
    items.append((40, y, "title", header[0]))
    y -= 22
    for line in header[1:]:
        items.append((40, y, "plain", line))
        y -= 12
    y -= 8
    for line in party:
        items.append((40, y, "plain", line))
        y -= 12
    y -= 10
    for x, name in cols:
        items.append((x, y, "bold", name))
    y -= 14
    for row in rows:
        for (x, _), cell in zip(cols, row, strict=True):
            items.append((x, y, "plain", cell))
        y -= 13
    y -= 8
    for label, value in totals:
        items.append((330, y, "bold" if label.startswith(("Total", "Gesamt")) else "plain", label))
        items.append((470, y, "plain", value))
        y -= 13
    return items


def _response(case: str, payload: dict, *, text_prefix="", text_suffix="", thinking=None):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking, "signature": "Er0CCkgIBhAC"})
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    content.append({"type": "text", "text": f"{text_prefix}{body}{text_suffix}"})
    return {
        "id": f"msg_golden_{case}",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 3120,
            "output_tokens": 1480,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def _line(n, desc, qty, unit, total, *, conf=0.93, tax=None, code=None):
    return {
        "line_number": n,
        "item_code": f(code, 0.8 if code is not None else 0.0),
        "description": f(desc, conf),
        "quantity": f(qty, conf),
        "unit_price": f(unit, conf),
        "tax": f(tax, 0.85 if tax is not None else 0.0),
        "total": f(total, conf),
    }


def _header(**fields) -> dict:
    """Every key the prompt asks for, ``null`` unless the page shows it."""
    keys = (
        "invoice_number vendor_name vendor_address vendor_tax_id amount currency subtotal "
        "tax_amount tax_rate discount_amount shipping_amount invoice_date due_date "
        "payment_terms po_number description reference_number payment_method "
        "bill_to_address remit_to_address suggested_gl_account suggested_cost_center"
    ).split()
    out = {k: f(None, 0.0) for k in keys}
    out.update(fields)
    return out


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #

US_COLS = [(40, "Description"), (300, "Qty"), (360, "Unit price"), (470, "Amount")]


def us_tax_exclusive():
    """USD, lines net of tax, header subtotal + sales tax + shipping."""
    lines = [
        ("Ergonomic task chair", Decimal(4), Decimal("289.00")),
        ("Standing desk frame", Decimal(2), Decimal("649.50")),
        ("Monitor arm, dual", Decimal(6), Decimal("124.95")),
    ]
    totals = [_q(q * p) for _, q, p in lines]
    subtotal = sum(totals)
    tax = _q(subtotal * Decimal("0.0825"))
    shipping = Decimal("85.00")
    amount = subtotal + tax + shipping
    page = _page(
        [
            "INVOICE",
            "Invoice no. NW-10482",
            "Date: 2026-09-14   Due: 2026-10-14   Terms: Net 30",
            "PO: PO-20417",
        ],
        [
            "Northwind Office Supply Co., 400 Example Ave, Springfield, IL 62701",
            "Bill to: Example Buyer Inc., 1 Test Plaza, Chicago, IL 60601",
        ],
        US_COLS,
        [(d, str(q), f"${us(p)}", f"${us(t)}") for (d, q, p), t in zip(lines, totals, strict=True)],
        [
            ("Subtotal", f"${us(subtotal)}"),
            ("Sales tax 8.25%", f"${us(tax)}"),
            ("Shipping", f"${us(shipping)}"),
            ("Total due", f"${us(amount)}"),
        ],
    )
    payload = _header(
        invoice_number=f("NW-10482", 0.98),
        vendor_name=f("Northwind Office Supply Co.", 0.97),
        vendor_address=f("400 Example Ave, Springfield, IL 62701", 0.9),
        amount=f(f"${us(amount)}", 0.97),
        currency=f("USD", 0.95),
        subtotal=f(f"${us(subtotal)}", 0.95),
        tax_amount=f(f"${us(tax)}", 0.94),
        tax_rate=f("8.25%", 0.92),
        shipping_amount=f(f"${us(shipping)}", 0.93),
        invoice_date=f("2026-09-14", 0.97),
        due_date=f("2026-10-14", 0.95),
        payment_terms=f("Net 30", 0.92),
        po_number=f("PO-20417", 0.94),
        description=f("Office furniture and monitor arms", 0.8),
        bill_to_address=f("Example Buyer Inc., 1 Test Plaza, Chicago, IL 60601", 0.88),
        suggested_gl_account=f("1500", 0.72),
        line_items=[
            _line(i, d, str(q), f"${us(p)}", f"${us(t)}")
            for i, ((d, q, p), t) in enumerate(zip(lines, totals, strict=True), start=1)
        ],
    )
    return [page], _response("us_tax_exclusive", payload)


def gb_vat_inclusive():
    """GBP, Net / VAT / Gross columns: the model reports net unit price, the
    line's VAT and the GROSS line total — tax-inclusive lines."""
    lines = [
        ("Quarterly HVAC service", Decimal(1), Decimal("1450.00")),
        ("Filter replacement", Decimal(12), Decimal("18.75")),
        ("Call-out charge", Decimal(2), Decimal("95.00")),
    ]
    nets = [_q(q * p) for _, q, p in lines]
    vats = [_q(n * Decimal("0.20")) for n in nets]
    gross = [n + v for n, v in zip(nets, vats, strict=True)]
    subtotal, vat, amount = sum(nets), sum(vats), sum(gross)
    cols = [
        (40, "Description"),
        (220, "Qty"),
        (260, "Unit"),
        (330, "Net"),
        (400, "VAT"),
        (470, "Gross"),
    ]
    page = _page(
        [
            "TAX INVOICE",
            "Invoice HF-2291",
            "Invoice date: 2026-09-03   Due date: 2026-10-03",
            "VAT reg. GB 999 9999 73",
        ],
        [
            "Harrow & Finch Facilities Ltd, 12 Sample Row, Leeds LS1 4AB",
            "To: Example Buyer UK Ltd, 3 Test Lane, London EC1A 1AA",
        ],
        cols,
        [
            (d, str(q), f"£{us(p)}", f"£{us(n)}", f"£{us(v)}", f"£{us(g)}")
            for (d, q, p), n, v, g in zip(lines, nets, vats, gross, strict=True)
        ],
        [
            ("Net total", f"£{us(subtotal)}"),
            ("VAT @ 20%", f"£{us(vat)}"),
            ("Total GBP", f"£{us(amount)}"),
        ],
    )
    payload = _header(
        invoice_number=f("HF-2291", 0.97),
        vendor_name=f("Harrow & Finch Facilities Ltd", 0.96),
        vendor_address=f("12 Sample Row, Leeds LS1 4AB", 0.9),
        vendor_tax_id=f("GB 999 9999 73", 0.9),
        amount=f(f"£{us(amount)}", 0.96),
        currency=f("GBP", 0.96),
        subtotal=f(f"£{us(subtotal)}", 0.94),
        tax_amount=f(f"£{us(vat)}", 0.94),
        tax_rate=f("20%", 0.93),
        invoice_date=f("2026-09-03", 0.95),
        due_date=f("2026-10-03", 0.93),
        description=f("HVAC maintenance and parts", 0.8),
        line_items=[
            _line(i, d, str(q), f"£{us(p)}", f"£{us(g)}", tax=f"£{us(v)}")
            for i, ((d, q, p), v, g) in enumerate(zip(lines, vats, gross, strict=True), start=1)
        ],
    )
    return [page], _response("gb_vat_inclusive", payload)


EU_LINES = [
    ("Stückgut Köln–Düsseldorf", "1", "1180.00"),
    ("Palettentausch Euro-Palette", "24", "14.50"),
    ("Lagergeld KW 36", "1", "640.00"),
    ("Lagergeld KW 37", "1", "640.00"),
    ("Kommissionierung (Std.)", "12.5", "42.00"),
    ("Expresszuschlag", "1", "95.00"),
    ("Maut-Umlage", "1", "118.40"),
    ("Verpackungsmaterial", "30", "3.85"),
    ("Lagergeld KW 38", "1", "640.00"),
    ("Lagergeld KW 39", "1", "640.00"),
    ("Stückgut Düsseldorf–Essen", "1", "1075.00"),
    ("Inventur Sonderleistung (Std.)", "6", "58.00"),
    ("Gefahrgutzuschlag", "2", "210.00"),
    ("Avisierung", "8", "12.00"),
    ("Kommissionierung (Std.)", "7.5", "42.00"),
    ("Retourenabwicklung", "15", "9.90"),
]


def eur_multipage():
    """EUR, decimal commas, two pages: lines 1–10 on page 1 with a carried-
    forward total, lines 11–16 and the totals on page 2. 19 % MwSt on top."""
    lines = [(d, Decimal(q), Decimal(p)) for d, q, p in EU_LINES]
    totals = [_q(q * p) for _, q, p in lines]
    subtotal = sum(totals)
    tax = _q(subtotal * Decimal("0.19"))
    amount = subtotal + tax
    carried = sum(totals[:10])
    cols = [(40, "Pos."), (70, "Leistung"), (300, "Menge"), (360, "Einzelpreis"), (470, "Betrag")]
    rows = [
        (str(i), d, qty_eu(q), f"{eu(p)} €", f"{eu(t)} €")
        for i, ((d, q, p), t) in enumerate(zip(lines, totals, strict=True), start=1)
    ]
    head = [
        "RECHNUNG",
        "Rechnungsnummer RL-2026-0917",
        "Rechnungsdatum: 2026-09-30   Fällig: 2026-10-30   Seite 1/2",
        "USt-IdNr. DE999999999",
    ]
    party = [
        "Rheinwerk Logistik GmbH, Beispielstraße 5, 50667 Köln",
        "An: Example Buyer GmbH, Teststraße 1, 40213 Düsseldorf",
    ]
    page1 = _page(head, party, cols, rows[:10], [("Übertrag", f"{eu(carried)} €")])
    page2 = _page(
        ["RECHNUNG", "Rechnungsnummer RL-2026-0917   Seite 2/2"],
        [],
        cols,
        [("", "Übertrag", "", "", f"{eu(carried)} €"), *rows[10:]],
        [
            ("Nettobetrag", f"{eu(subtotal)} €"),
            ("MwSt. 19 %", f"{eu(tax)} €"),
            ("Gesamtbetrag EUR", f"{eu(amount)} €"),
        ],
    )
    payload = _header(
        invoice_number=f("RL-2026-0917", 0.97),
        vendor_name=f("Rheinwerk Logistik GmbH", 0.97),
        vendor_address=f("Beispielstraße 5, 50667 Köln", 0.9),
        vendor_tax_id=f("DE999999999", 0.93),
        # Transcribed as printed, the ISO code included.
        amount=f(f"EUR {eu(amount)}", 0.95),
        currency=f("EUR", 0.97),
        subtotal=f(f"{eu(subtotal)} €", 0.94),
        tax_amount=f(f"{eu(tax)} €", 0.93),
        tax_rate=f("19 %", 0.93),
        invoice_date=f("2026-09-30", 0.96),
        due_date=f("2026-10-30", 0.94),
        description=f("Lager- und Transportleistungen September 2026", 0.78),
        line_items=[
            _line(i, d, qty_eu(q), eu(p), eu(t), conf=0.9)
            for i, ((d, q, p), t) in enumerate(zip(lines, totals, strict=True), start=1)
        ],
    )
    return [page1, page2], _response("eur_multipage", payload)


def jpy_zero_decimal():
    """JPY — no minor unit — 10 % consumption tax; no due date or PO on the page."""
    lines = [
        ("Bearing unit 6204-ZZ", Decimal(40), Decimal(1850)),
        ("Shaft coupling 25mm", Decimal(12), Decimal(4200)),
        ("Machining fee", Decimal(1), Decimal(36000)),
    ]
    totals = [q * p for _, q, p in lines]
    subtotal = sum(totals)
    tax = subtotal * Decimal("0.10")
    amount = subtotal + tax
    page = _page(
        ["INVOICE", "No. SP-55810", "Issue date: 2026-09-25", "Registration No. T9999999999999"],
        [
            "Sakura Precision Parts K.K., 1-2-3 Example-cho, Nagoya",
            "To: Example Buyer Japan G.K., Tokyo",
        ],
        US_COLS,
        [
            (d, str(q), f"¥{yen(p)}", f"¥{yen(t)}")
            for (d, q, p), t in zip(lines, totals, strict=True)
        ],
        [
            ("Subtotal", f"¥{yen(subtotal)}"),
            ("Consumption tax 10%", f"¥{yen(tax)}"),
            ("Total", f"¥{yen(amount)}"),
        ],
    )
    payload = _header(
        invoice_number=f("SP-55810", 0.97),
        vendor_name=f("Sakura Precision Parts K.K.", 0.95),
        vendor_address=f("1-2-3 Example-cho, Nagoya", 0.85),
        vendor_tax_id=f("T9999999999999", 0.9),
        amount=f(f"¥{yen(amount)}", 0.96),
        currency=f("JPY", 0.95),
        subtotal=f(f"¥{yen(subtotal)}", 0.94),
        tax_amount=f(f"¥{yen(tax)}", 0.93),
        tax_rate=f("10%", 0.92),
        invoice_date=f("2026-09-25", 0.96),
        description=f("Bearings, couplings and machining", 0.8),
        line_items=[
            _line(i, d, str(q), f"¥{yen(p)}", f"¥{yen(t)}")
            for i, ((d, q, p), t) in enumerate(zip(lines, totals, strict=True), start=1)
        ],
    )
    return [page], _response("jpy_zero_decimal", payload)


def us_scanned():
    """USD, a SCAN (image-only PDF). Lower confidences; the answer comes after a
    thinking block, inside a ```json fence, with money as JSON numbers, a
    numeric SKU, an "N/A" sentinel and a prompt-schema leak."""
    lines = [
        ("Lunch buffet (per guest)", Decimal(45), Decimal("24.50"), 4411),
        ("Coffee service", Decimal(3), Decimal("65.00"), 2030),
    ]
    totals = [_q(q * p) for _, q, p, _ in lines]
    subtotal = sum(totals)
    discount = _q(subtotal * Decimal("0.10"))
    tax = _q((subtotal - discount) * Decimal("0.07"))
    amount = subtotal - discount + tax
    page = _page(
        [
            "INVOICE",
            "Invoice # BG-7731",
            "Date 2026-09-18   Due 2026-10-03",
            "Bluegill Catering LLC",
        ],
        ["77 Sample Road, Madison, WI 53703", "Bill to: Example Buyer Inc."],
        US_COLS,
        [
            (f"{code} {d}", str(q), us(p), us(t))
            for (d, q, p, code), t in zip(lines, totals, strict=True)
        ],
        [
            ("Subtotal", us(subtotal)),
            ("Loyalty discount 10%", f"-{us(discount)}"),
            ("Sales tax 7%", us(tax)),
            ("Total", us(amount)),
        ],
    )
    payload = _header(
        invoice_number=f("BG-7731", 0.86),
        vendor_name=f("Bluegill Catering LLC", 0.88),
        vendor_address=f("77 Sample Road, Madison, WI 53703", 0.75),
        amount=f(float(amount), 0.84),
        currency=f("USD", 0.7),
        subtotal=f(float(subtotal), 0.82),
        tax_amount=f(float(tax), 0.8),
        tax_rate=f(7, 0.78),
        discount_amount=f(float(discount), 0.78),
        invoice_date=f("2026-09-18", 0.85),
        due_date=f("2026-10-03", 0.82),
        po_number=f("N/A", 0.3),
        reference_number=f("string or null", 0.2),
        description=f("Event catering", 0.7),
        line_items=[
            {**_line(i, d, int(q), float(p), float(t), conf=0.8, code=code)}
            for i, ((d, q, p, code), t) in enumerate(zip(lines, totals, strict=True), start=1)
        ],
    )
    return [page], _response(
        "us_scanned",
        payload,
        text_prefix="```json\n",
        text_suffix="\n```",
        thinking="The scan is legible; totals reconcile after the 10% discount.",
    )


def inconsistent_gb():
    """GBP, a correct invoice read WRONG: the total transposed (2,843.25 for
    2,483.25), line 3 dropped, line 2's unit price misread (103.00 for 130.00)
    and the two dates swapped. Dates come back day-first as printed."""
    printed = [
        ("Brochure print run (A4, 2,000)", Decimal(1), Decimal("1200.00")),
        ("Design proof rounds", Decimal(5), Decimal("130.00")),
        ("Courier delivery", Decimal(4), Decimal("115.00")),
    ]
    totals = [_q(q * p) for _, q, p in printed]
    subtotal = sum(totals)
    tax = _q(subtotal * Decimal("0.075"))
    amount = subtotal + tax
    page = _page(
        [
            "INVOICE",
            "Invoice KP-0388",
            "Date: 28/09/2026   Due: 28/10/2026",
            "Company no. 09999999",
        ],
        ["Kestrel Print Studio Ltd, 9 Sample Mews, Bristol BS1 5AA", "To: Example Buyer UK Ltd"],
        US_COLS,
        [
            (d, str(q), f"£{us(p)}", f"£{us(t)}")
            for (d, q, p), t in zip(printed, totals, strict=True)
        ],
        [
            ("Subtotal", f"£{us(subtotal)}"),
            ("VAT 7.5%", f"£{us(tax)}"),
            ("Total", f"£{us(amount)}"),
        ],
    )
    payload = _header(
        invoice_number=f("KP-0388", 0.95),
        vendor_name=f("Kestrel Print Studio Ltd", 0.95),
        amount=f("£2,843.25", 0.9),
        currency=f("GBP", 0.95),
        subtotal=f(f"£{us(subtotal)}", 0.92),
        tax_amount=f(f"£{us(tax)}", 0.9),
        tax_rate=f("7.5%", 0.9),
        invoice_date=f("28/10/2026", 0.88),
        due_date=f("28/09/2026", 0.86),
        line_items=[
            _line(1, printed[0][0], "1", "£1,200.00", "£1,200.00"),
            _line(2, printed[1][0], "5", "£103.00", "£650.00"),
        ],
    )
    return [page], _response("inconsistent_gb", payload)


CASES = {
    "us_tax_exclusive": (us_tax_exclusive, False),
    "gb_vat_inclusive": (gb_vat_inclusive, False),
    "eur_multipage": (eur_multipage, False),
    "jpy_zero_decimal": (jpy_zero_decimal, False),
    "us_scanned": (us_scanned, True),
    "inconsistent_gb": (inconsistent_gb, False),
}


def main() -> None:
    for name, (build, scanned) in CASES.items():
        pages, response = build()
        pdf = OUT / f"{name}.pdf"
        _render(pdf, pages)
        if scanned:
            _rasterise(pdf)
        (OUT / f"{name}.response.json").write_text(
            json.dumps(response, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {pdf.name} ({pdf.stat().st_size} bytes) + {name}.response.json")


if __name__ == "__main__":
    main()
