"""EN 16931 calculation rules (BR-CO-*) + code-list membership (BR-CL-*).

The BIS Billing 3.0 gate used to check only that the mandatory *elements* were
present, so an arithmetically incoherent document — a VAT amount that is not its
base times its rate, a grand total that is not the net plus the VAT, a line net
that is not quantity times price — passed our check and got a conformance claim
stamped on it, then was rejected by the first Access Point that recomputed it.

One satisfying and one violating document per rule, each asserting the specific
rule id comes back, because a rule that cannot name itself is not diagnosable.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from lxml import etree

from app.models.invoice import Invoice, InvoiceLineItem
from app.services.e_invoice import codelists
from app.services.e_invoice.bis3 import (
    BIS3_CUSTOMIZATION_ID,
    bis3_conformance_errors,
    is_bis3_conformant,
)
from app.services.e_invoice.en16931_rules import calculation_errors, code_list_errors
from app.services.e_invoice.generate import generate_ubl
from app.services.e_invoice.mapper import BuyerIdentity, invoice_to_einvoice_document
from app.services.e_invoice.model import (
    EInvoiceDocument,
    EInvoiceFormat,
    EInvoiceLine,
    EInvoiceParty,
    EInvoiceTax,
)
from app.services.e_invoice.ubl import parse_ubl

_NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

_SELLER_VAT = "DE123456789"  # PII — must never appear in a FieldError
_BUYER_VAT = "DE987654321"


def _doc() -> EInvoiceDocument:
    """A document that satisfies every rule this module implements.

    100.00 net, 19% VAT, one line, one breakdown group — the smallest shape in
    which every calculation rule has all of its inputs.
    """
    return EInvoiceDocument(
        source_format=EInvoiceFormat.UBL,
        invoice_number="CALC-1",
        issue_date=date(2026, 1, 15),
        due_date=date(2026, 2, 15),
        currency="EUR",
        invoice_type_code="380",
        payment_means_code="30",
        seller=EInvoiceParty(
            name="Lieferant GmbH",
            tax_id=_SELLER_VAT,
            country_code="DE",
            endpoint_id=_SELLER_VAT,
            endpoint_scheme_id="9930",
        ),
        buyer=EInvoiceParty(
            name="Kaeufer AG",
            tax_id=_BUYER_VAT,
            country_code="DE",
            endpoint_id=_BUYER_VAT,
            endpoint_scheme_id="9930",
        ),
        line_extension_amount=Decimal("100.00"),
        tax_exclusive_amount=Decimal("100.00"),
        tax_inclusive_amount=Decimal("119.00"),
        tax_total=Decimal("19.00"),
        payable_amount=Decimal("119.00"),
        taxes=[
            EInvoiceTax(
                category="S",
                rate=Decimal("19.00"),
                taxable_amount=Decimal("100.00"),
                tax_amount=Decimal("19.00"),
            )
        ],
        lines=[
            EInvoiceLine(
                line_id="1",
                description="Widget",
                quantity=Decimal("2"),
                unit_code="C62",
                unit_price=Decimal("50.00"),
                line_total=Decimal("100.00"),
                tax_amount=Decimal("19.00"),
                tax_rate=Decimal("19.00"),
                tax_category="S",
            )
        ],
    )


def _codes(errors) -> set[str]:
    return {e.code for e in errors}


def _pairs(errors) -> set[tuple[str, str]]:
    return {(e.field, e.code) for e in errors}


# ---------------------------------------------------------------------------
# The satisfying baseline
# ---------------------------------------------------------------------------


def test_baseline_document_satisfies_every_rule():
    assert calculation_errors(_doc()) == []
    assert code_list_errors(_doc()) == []
    assert bis3_conformance_errors(_doc()) == []


# ---------------------------------------------------------------------------
# Calculation rules — one violation each, naming its own rule id
# ---------------------------------------------------------------------------


def test_br_co_10_line_sum_must_equal_the_document_line_total():
    doc = _doc()
    doc.lines.append(
        EInvoiceLine(
            line_id="2",
            description="Second widget",
            quantity=Decimal("1"),
            unit_price=Decimal("40.00"),
            line_total=Decimal("40.00"),
            tax_rate=Decimal("19.00"),
            tax_category="S",
        )
    )
    # line_extension_amount still says 100.00; the lines now sum to 140.00.
    assert ("line_extension_amount", "BR-CO-10") in _pairs(calculation_errors(doc))


def test_br_co_13_total_without_vat_is_lines_less_allowances_plus_charges():
    doc = _doc()
    doc.allowance_total = Decimal("10.00")  # 100 - 10 = 90, but BT-109 says 100
    assert ("tax_exclusive_amount", "BR-CO-13") in _pairs(calculation_errors(doc))

    doc.tax_exclusive_amount = Decimal("90.00")
    doc.tax_inclusive_amount = Decimal("109.00")
    doc.payable_amount = Decimal("109.00")
    doc.taxes[0].taxable_amount = Decimal("90.00")
    doc.taxes[0].tax_amount = Decimal("19.00")  # left at 19 so BR-CO-13 is isolated
    doc.tax_total = Decimal("19.00")
    assert "BR-CO-13" not in _codes(calculation_errors(doc))


def test_br_co_14_total_vat_must_equal_the_sum_of_the_breakdown():
    doc = _doc()
    doc.tax_total = Decimal("25.00")
    assert ("tax_total", "BR-CO-14") in _pairs(calculation_errors(doc))


def test_br_co_15_total_with_vat_is_total_without_vat_plus_vat():
    doc = _doc()
    doc.tax_inclusive_amount = Decimal("125.00")
    doc.payable_amount = Decimal("125.00")
    assert ("tax_inclusive_amount", "BR-CO-15") in _pairs(calculation_errors(doc))


def test_br_co_16_amount_due_must_equal_the_total_with_vat():
    """Reduced form: BT-113 (prepaid) and BT-114 (rounding) have no model
    field, so an amount due that differs from the total with VAT cannot be
    explained by anything the model can carry."""
    doc = _doc()
    doc.payable_amount = Decimal("100.00")
    assert ("payable_amount", "BR-CO-16") in _pairs(calculation_errors(doc))


def test_br_co_17_vat_amount_must_be_its_base_times_its_rate():
    doc = _doc()
    doc.taxes[0].tax_amount = Decimal("21.00")
    doc.tax_total = Decimal("21.00")
    doc.tax_inclusive_amount = Decimal("121.00")
    doc.payable_amount = Decimal("121.00")
    codes = _pairs(calculation_errors(doc))
    assert ("taxes[0].tax_amount", "BR-CO-17") in codes


def test_br_co_17_tolerates_the_standards_own_two_cent_rounding_allowance():
    """The VAT amount is derived (base x rate), and emitters round it
    differently — EN 16931 allows two cents, so a one-cent drift is not a
    violation and must not cost the document its profile."""
    doc = _doc()
    doc.taxes[0].tax_amount = Decimal("19.01")
    doc.tax_total = Decimal("19.01")
    doc.tax_inclusive_amount = Decimal("119.01")
    doc.payable_amount = Decimal("119.01")
    assert "BR-CO-17" not in _codes(calculation_errors(doc))


def test_br_co_25_an_amount_due_needs_a_due_date_or_payment_terms():
    doc = _doc()
    doc.due_date = None
    assert ("due_date", "BR-CO-25") in _pairs(calculation_errors(doc))

    doc.payment_terms_note = "Net 30"
    assert "BR-CO-25" not in _codes(calculation_errors(doc))


def test_br_co_26_the_seller_needs_an_identifier():
    doc = _doc()
    doc.seller.tax_id = None
    assert ("seller.tax_id", "BR-CO-26") in _pairs(calculation_errors(doc))

    doc.seller.registration_id = "HRB 12345"
    assert "BR-CO-26" not in _codes(calculation_errors(doc))


def test_peppol_r120_line_net_must_equal_quantity_times_price():
    doc = _doc()
    doc.lines[0].unit_price = Decimal("40.00")  # 2 x 40 = 80, line says 100.00
    assert ("lines[0].line_total", "PEPPOL-EN16931-R120") in _pairs(calculation_errors(doc))


def test_peppol_r120_is_skipped_when_an_input_is_absent():
    """Presence is `bis3_conformance_errors`' job. Reporting the same missing
    quantity a second time under a calculation id would be noise."""
    doc = _doc()
    doc.lines[0].quantity = None
    assert "PEPPOL-EN16931-R120" not in _codes(calculation_errors(doc))


# ---------------------------------------------------------------------------
# Per-VAT-category family
# ---------------------------------------------------------------------------


def test_br_s_08_breakdown_base_must_equal_the_sum_of_its_lines():
    doc = _doc()
    doc.taxes[0].taxable_amount = Decimal("80.00")
    doc.taxes[0].tax_amount = Decimal("15.20")
    doc.tax_total = Decimal("15.20")
    doc.tax_exclusive_amount = Decimal("80.00")
    doc.tax_inclusive_amount = Decimal("95.20")
    doc.payable_amount = Decimal("95.20")
    # The lines still sum to 100.00 in category S.
    assert ("taxes[0].taxable_amount", "BR-S-08") in _pairs(calculation_errors(doc))


def test_br_s_08_is_skipped_when_the_model_cannot_attribute_allowances():
    """A document-level allowance belongs to a VAT category in EN 16931, and
    the model carries only the undifferentiated total — so the expected taxable
    amount per category is unknowable, not zero. Skipping is the honest answer;
    guessing would refuse conforming documents."""
    doc = _doc()
    doc.allowance_total = Decimal("10.00")
    doc.tax_exclusive_amount = Decimal("90.00")
    doc.taxes[0].taxable_amount = Decimal("90.00")
    doc.taxes[0].tax_amount = Decimal("17.10")
    doc.tax_total = Decimal("17.10")
    doc.tax_inclusive_amount = Decimal("107.10")
    doc.payable_amount = Decimal("107.10")
    assert "BR-S-08" not in _codes(calculation_errors(doc))


def test_br_z_01_a_line_category_obliges_a_breakdown_group():
    doc = _doc()
    doc.lines[0].tax_category = "Z"
    doc.lines[0].tax_rate = Decimal("0")
    assert ("taxes", "BR-Z-01") in _pairs(calculation_errors(doc))


def test_br_z_05_a_zero_rated_line_must_carry_a_zero_rate():
    doc = _doc()
    doc.lines[0].tax_category = "Z"  # rate is still 19.00
    assert ("lines[0].tax_rate", "BR-Z-05") in _pairs(calculation_errors(doc))


def test_br_ae_05_reverse_charge_must_carry_a_zero_rate():
    doc = _doc()
    doc.lines[0].tax_category = "AE"
    assert ("lines[0].tax_rate", "BR-AE-05") in _pairs(calculation_errors(doc))


def test_br_s_05_a_standard_rated_line_must_carry_a_rate_above_zero():
    doc = _doc()
    doc.lines[0].tax_rate = Decimal("0")
    assert ("lines[0].tax_rate", "BR-S-05") in _pairs(calculation_errors(doc))


# ---------------------------------------------------------------------------
# Code-list membership
# ---------------------------------------------------------------------------


def test_br_cl_03_currency_must_be_iso_4217():
    doc = _doc()
    doc.currency = "EURO"
    assert ("currency", "BR-CL-03") in _pairs(code_list_errors(doc))

    doc.currency = "SEK"
    assert "BR-CL-03" not in _codes(code_list_errors(doc))


def test_br_cl_01_invoice_type_code_must_be_untdid_1001():
    doc = _doc()
    doc.invoice_type_code = "999"
    assert ("invoice_type_code", "BR-CL-01") in _pairs(code_list_errors(doc))

    doc.invoice_type_code = "381"  # credit note
    assert "BR-CL-01" not in _codes(code_list_errors(doc))


def test_br_cl_14_country_must_be_iso_3166_alpha_2():
    doc = _doc()
    doc.buyer.country_code = "EL"  # a VAT prefix, not a country code
    assert ("buyer.country_code", "BR-CL-14") in _pairs(code_list_errors(doc))

    doc.buyer.country_code = "GR"
    assert "BR-CL-14" not in _codes(code_list_errors(doc))


def test_br_cl_17_and_18_vat_category_must_be_uncl5305():
    doc = _doc()
    doc.taxes[0].category = "X"
    doc.lines[0].tax_category = "X"
    pairs = _pairs(code_list_errors(doc))
    assert ("taxes[0].category", "BR-CL-17") in pairs
    assert ("lines[0].tax_category", "BR-CL-18") in pairs


def test_br_co_09_vat_identifier_needs_its_country_prefix():
    doc = _doc()
    doc.seller.tax_id = "123456789"  # a bare national id — e.g. a US EIN
    assert ("seller.tax_id", "BR-CO-09") in _pairs(code_list_errors(doc))


def test_br_co_09_accepts_the_greek_el_prefix():
    """The one prefix the rule names as an exception: Greece issues VAT numbers
    under EL, which is not an ISO 3166-1 code."""
    doc = _doc()
    doc.seller.tax_id = "EL123456789"
    doc.seller.country_code = "GR"
    assert "BR-CO-09" not in _codes(code_list_errors(doc))


def test_shape_only_lists_do_not_refuse_a_code_they_simply_do_not_hold():
    """UN/ECE Rec 20 is thousands of codes and we vendor none of them. A
    well-formed unit we don't recognise must pass — refusing it would 422 a
    conforming PEPPOL send, which is worse than the gap it closes."""
    doc = _doc()
    doc.lines[0].unit_code = "MTK"  # square metre — not in any table here
    assert "BR-CL-23" not in _codes(code_list_errors(doc))

    doc.lines[0].unit_code = "not a unit code"
    assert ("lines[0].unit_code", "BR-CL-23") in _pairs(code_list_errors(doc))


def test_payment_means_gets_the_same_shape_only_treatment():
    doc = _doc()
    doc.payment_means_code = "58"  # SEPA credit transfer
    assert "BR-CL-16" not in _codes(code_list_errors(doc))

    doc.payment_means_code = "ach"  # our internal token, not a UNCL4461 code
    assert ("payment_means_code", "BR-CL-16") in _pairs(code_list_errors(doc))


# ---------------------------------------------------------------------------
# The code lists themselves — drift guards against the rest of the codebase
# ---------------------------------------------------------------------------


def test_every_country_the_codebase_already_models_is_in_the_iso_list():
    """The country table is hand-held, and a country missing from it silently
    costs a real tenant its profile declaration. Pin it against every country
    the repo already claims to understand."""
    from app.services.e_invoice.mapper import _VAT_PREFIX_TO_COUNTRY
    from app.services.e_invoice.tax_rules import _TAX_ID_PATTERNS
    from app.services.international_tax.country_rules import COUNTRY_RULES

    known = set(COUNTRY_RULES) | set(_TAX_ID_PATTERNS) | set(_VAT_PREFIX_TO_COUNTRY.values())
    assert known <= codelists.ISO_3166_ALPHA2


def test_every_currency_the_codebase_already_models_is_in_the_iso_list():
    from app.services.payment_adapters.base import _MINOR_UNIT_EXPONENTS

    assert set(_MINOR_UNIT_EXPONENTS) <= codelists.ISO_4217_CURRENCIES


def test_every_payment_means_code_we_emit_is_shaped_like_one():
    from app.services.e_invoice.payment_means import METHOD_TO_PAYMENT_MEANS

    assert all(codelists.is_plausible_payment_means(c) for c in METHOD_TO_PAYMENT_MEANS.values())


def test_the_generators_default_unit_code_is_accepted():
    from app.services.e_invoice.generate import _DEFAULT_UNIT_CODE

    assert codelists.is_plausible_unit_code(_DEFAULT_UNIT_CODE)


def test_the_mappers_default_vat_category_is_in_the_en16931_subset():
    from app.services.e_invoice.mapper import _DEFAULT_TAX_CATEGORY

    assert codelists.is_valid_vat_category(_DEFAULT_TAX_CATEGORY)


def test_the_generators_default_invoice_type_code_is_in_untdid_1001():
    from app.services.e_invoice.generate import _DEFAULT_INVOICE_TYPE_CODE

    assert codelists.is_valid_document_type(_DEFAULT_INVOICE_TYPE_CODE)


# ---------------------------------------------------------------------------
# PII + contract
# ---------------------------------------------------------------------------


def test_no_rule_failure_ever_carries_a_value():
    doc = _doc()
    doc.currency = "EURO"
    doc.seller.tax_id = "987654321"
    doc.payable_amount = Decimal("100.00")
    doc.lines[0].unit_price = Decimal("40.00")
    for err in bis3_conformance_errors(doc):
        rendered = f"{err.field} {err.code} {err.message}"
        assert "987654321" not in rendered
        assert "EURO" not in rendered
        assert "Lieferant" not in rendered
        assert "119.00" not in rendered


def test_existing_callers_still_read_missing_for_a_mandatory_element():
    """The mandatory-element layer's contract is unchanged — `code` is still
    `"missing"`, so `str(EInvoiceValidationError)` still renders
    `field: missing` for the checks that predate the rule engine."""
    doc = _doc()
    doc.buyer.endpoint_id = None
    assert ("buyer.endpoint_id", "missing") in _pairs(bis3_conformance_errors(doc))


# ---------------------------------------------------------------------------
# The conditional profile declaration — unchanged behaviour for its callers
# ---------------------------------------------------------------------------


def _customization(xml: bytes) -> str | None:
    el = etree.fromstring(xml).find(f"{{{_NS_CBC}}}CustomizationID")
    return None if el is None else el.text


def test_a_calculation_failure_costs_the_document_its_profile_declaration():
    """The whole point: an arithmetically incoherent document used to be
    emitted WITH the BIS conformance claim, because only element presence was
    checked. It is still perfectly readable UBL — it just no longer lies."""
    doc = _doc()
    doc.tax_inclusive_amount = Decimal("125.00")
    doc.payable_amount = Decimal("125.00")
    assert not is_bis3_conformant(doc)
    assert _customization(generate_ubl(doc)) is None
    assert etree.fromstring(generate_ubl(doc)).find(f"{{{_NS_CBC}}}ID").text == "CALC-1"


def test_a_conforming_document_still_declares_the_profile():
    assert _customization(generate_ubl(_doc())) == BIS3_CUSTOMIZATION_ID


def test_declare_profile_override_still_wins_in_both_directions():
    doc = _doc()
    doc.payable_amount = Decimal("100.00")  # BR-CO-16 violation
    assert _customization(generate_ubl(doc, declare_profile=True)) == BIS3_CUSTOMIZATION_ID
    assert _customization(generate_ubl(_doc(), declare_profile=False)) is None


def test_the_round_trip_still_passes_the_whole_check():
    assert bis3_conformance_errors(parse_ubl(generate_ubl(_doc()))) == []


# ---------------------------------------------------------------------------
# Regression guard — what OUR OWN generator produces must pass
# ---------------------------------------------------------------------------


def _orm_invoice(*, currency: str = "EUR", **overrides) -> Invoice:
    kwargs = dict(
        invoice_number="INV-2026-0001",
        vendor_name="Lieferant GmbH",
        vendor_tax_id=_SELLER_VAT,
        amount=Decimal("119.00"),
        invoice_date=date(2026, 1, 15),
        due_date=date(2026, 2, 15),
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("19.00"),
        tax_rate=Decimal("19.00"),
        payment_method="ach",
    )
    kwargs.update(overrides)
    return Invoice(currency=currency, **kwargs)


_BUYER = BuyerIdentity(
    name="Kaeufer AG",
    tax_id=_BUYER_VAT,
    country_code="DE",
)


def _mapped(invoice: Invoice, line_items: list[InvoiceLineItem]) -> EInvoiceDocument:
    doc = invoice_to_einvoice_document(invoice, line_items, _BUYER)
    # `peppol_send` stamps the electronic addresses from the AS4 participant
    # ids; the mapper has no access to them. Mirror that here so the fixture
    # exercises the document the send path actually serializes.
    doc.seller.endpoint_id = _SELLER_VAT
    doc.seller.endpoint_scheme_id = "9930"
    doc.buyer.endpoint_id = _BUYER_VAT
    doc.buyer.endpoint_scheme_id = "9930"
    return doc


def test_a_document_our_own_mapper_produces_passes_the_whole_check():
    lines = [
        InvoiceLineItem(
            line_number=1,
            description="Widget",
            quantity=Decimal("2"),
            unit_price=Decimal("30.00"),
            total=Decimal("60.00"),
        ),
        InvoiceLineItem(
            line_number=2,
            description="Gadget",
            quantity=Decimal("1"),
            unit_price=Decimal("40.00"),
            total=Decimal("40.00"),
        ),
    ]
    doc = _mapped(_orm_invoice(), lines)
    assert bis3_conformance_errors(doc) == []
    assert _customization(generate_ubl(doc)) == BIS3_CUSTOMIZATION_ID


def test_a_discounted_invoice_maps_to_the_right_tax_exclusive_base():
    """Regression: the mapper put `Invoice.subtotal` in BOTH BT-106 (sum of
    line nets) and BT-109 (total without VAT). `subtotal` is only BT-106 — it
    is derived before the discount and shipping columns — so every invoice
    carrying either produced a document that failed BR-CO-13, BR-CO-15 and
    BR-CO-17 simultaneously, and the receiver's validator would have rejected
    it. 100 net - 10 discount + 5 shipping = 95 base, 19% VAT = 18.05."""
    lines = [
        InvoiceLineItem(
            line_number=1,
            description="Widget",
            quantity=Decimal("4"),
            unit_price=Decimal("25.00"),
            total=Decimal("100.00"),
        )
    ]
    invoice = _orm_invoice(
        discount_amount=Decimal("10.00"),
        shipping_amount=Decimal("5.00"),
        tax_amount=Decimal("18.05"),
        amount=Decimal("113.05"),
    )
    doc = _mapped(invoice, lines)

    assert doc.line_extension_amount == Decimal("100.00")
    assert doc.tax_exclusive_amount == Decimal("95.00")
    assert doc.taxes[0].taxable_amount == Decimal("95.00")
    assert bis3_conformance_errors(doc) == []


@pytest.mark.parametrize("currency", ["EUR", "USD", "GBP", "JPY", "SEK"])
def test_the_mapper_survives_every_currency_a_tenant_might_configure(currency):
    lines = [
        InvoiceLineItem(
            line_number=1,
            description="Widget",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            total=Decimal("100.00"),
        )
    ]
    doc = _mapped(_orm_invoice(currency=currency), lines)
    assert "BR-CL-03" not in _codes(bis3_conformance_errors(doc))
