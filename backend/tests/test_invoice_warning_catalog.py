"""The invoice-warning catalogue, and the guards that keep it honest.

`decisions.md` §157 replaced the hand-composed `message` on every
`invoice.warnings` entry with a `{code, params}` pair the client can localize.
Three failures would quietly undo that, and there is a test for each:

* a composition site that goes back to a dict literal — then the warning it
  emits has no code and reaches the browser as server English again;
* a declared code with no call site — the objection
  `e_invoice/rule_catalog.py` raises against registries ("an author can add a
  table entry and forget to use it");
* a stale generated frontend catalogue — the same `--check` drift guard the
  e-invoice rule messages already have, asserted here as well as in CI so a
  local run catches it.
"""

from __future__ import annotations

import ast
import importlib
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import invoice_warning_catalog as cat
from app.services.contract_compliance import COMPLIANCE_EXCEPTION_TYPE
from app.services.invoice_warning_catalog import WARNING_SPECS, codes, render, warning

#: Every module that appends to an `invoice.warnings` list. A new one joins the
#: list here, which is the point: the scan below is only as complete as this.
PRODUCER_MODULES = (
    "app/services/invoice_warnings.py",
    "app/services/contract_compliance.py",
    "app/services/duplicate_detection.py",
    "app/services/recurring_invoices.py",
    "app/services/extraction.py",
    "app/services/extraction_self_correction.py",
)

_BACKEND = Path(__file__).resolve().parent.parent

#: One sample value per kind, good enough to render every template.
_SAMPLES: dict[str, object] = {
    "text": "SAMPLE",
    "currency": "EUR",
    "money": Decimal("1234.50"),
    "number": "12.5",
    "percent": "+3.5",
    "count": 2,
    "date": date(2026, 5, 4),
}


def _sample_params(spec: cat.WarningSpec) -> dict:
    return {name: _SAMPLES[kind] for name, kind in spec.params.items()}


# --------------------------------------------------------------------------- #
# The catalogue itself
# --------------------------------------------------------------------------- #


def test_codes_are_unique_and_snake_case():
    assert len(set(codes())) == len(codes())
    for code in codes():
        assert re.fullmatch(r"[a-z][a-z0-9_]*", code), code


@pytest.mark.parametrize("spec", WARNING_SPECS, ids=lambda s: s.code)
def test_every_code_renders_a_complete_sentence(spec):
    """No placeholder survives, and the payload carries code + params."""
    out = warning(spec.code, "warning", **_sample_params(spec))
    assert out["type"] == spec.type
    assert out["code"] == spec.code
    assert set(out["params"]) == set(spec.params)
    assert out["message"]
    # A leftover `{` means a placeholder the params didn't cover, or an ICU
    # block that failed to resolve — either way a reviewer would see markup.
    assert "{" not in out["message"] and "}" not in out["message"], out["message"]
    assert ", plural," not in out["message"]


def test_every_code_has_a_call_site():
    """A declared code nobody emits is a catalogue entry pretending to work."""
    sources = "\n".join((_BACKEND / rel).read_text(encoding="utf-8") for rel in PRODUCER_MODULES)
    for code in codes():
        assert f'"{code}"' in sources or f"'{code}'" in sources, f"{code} has no call site"


def test_no_producer_hand_rolls_a_warning_dict():
    """Every warning is built by `warning(...)`, never a dict literal.

    This is the guard that makes the catalogue complete rather than
    aspirational: a hand-rolled `{"type": …, "message": …}` carries no code, so
    the client falls back to server English and nothing else notices.
    """
    offenders: list[str] = []
    for rel in PRODUCER_MODULES:
        tree = ast.parse((_BACKEND / rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            literal_keys = {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if {"type", "message"} <= literal_keys:
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "warning dicts built by hand instead of `invoice_warning_catalog.warning(...)`: "
        + ", ".join(offenders)
    )


def test_contract_codes_carry_the_registered_exception_type():
    """The `_ensure_exception` type and the finding type are one vocabulary."""
    contract_codes = [s for s in WARNING_SPECS if s.code.startswith("contract_")]
    assert contract_codes
    for spec in contract_codes:
        assert spec.type == COMPLIANCE_EXCEPTION_TYPE


def test_unknown_code_and_bad_params_raise():
    with pytest.raises(KeyError):
        warning("no_such_code", "warning")
    with pytest.raises(ValueError):
        warning("round_amount", "info", amount=Decimal("1"))  # missing `currency`
    with pytest.raises(ValueError):
        warning("past_due", "warning", extra="nope")


# --------------------------------------------------------------------------- #
# Param coercion
# --------------------------------------------------------------------------- #


def test_money_keeps_exact_digits_and_never_narrows_below_two_places():
    out = warning(
        "line_total_mismatch",
        "error",
        lineItemsTotal=Decimal("100.5"),
        headerAmount=Decimal("120.1234"),
        currency="usd",
    )
    # Widened to 2 places, never rounded down from 4.
    assert out["params"]["lineItemsTotal"] == "100.50"
    assert out["params"]["headerAmount"] == "120.1234"
    # Currency normalised to an ISO code the client can format with.
    assert out["params"]["currency"] == "USD"


def test_a_missing_currency_falls_back_rather_than_emitting_none():
    out = warning("round_amount", "info", amount=Decimal("5000.00"), currency=None)
    assert out["params"]["currency"] == cat.DEFAULT_CURRENCY
    assert out["message"] == "Round amount: 5000.00 USD"


def test_dates_are_iso_so_the_client_can_reformat_them():
    out = warning(
        "contract_expired",
        "warning",
        invoiceDate=date(2026, 3, 2),
        contractNumber="C-1",
        endDate=date(2026, 1, 31),
    )
    assert out["params"]["invoiceDate"] == "2026-03-02"
    assert out["message"] == ("Invoice dated 2026-03-02 is after contract C-1 expired (2026-01-31)")


# --------------------------------------------------------------------------- #
# The English plural resolver
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (1, "Rush payment: due within 1 day of the invoice date"),
        (0, "Rush payment: due within 0 days of the invoice date"),
        (3, "Rush payment: due within 3 days of the invoice date"),
    ],
)
def test_english_plural_selection(days, expected):
    assert warning("rush_payment", "warning", days=days)["message"] == expected


def test_an_exact_zero_arm_can_render_nothing():
    """The cross-entity tail is a `=0 {}` arm, not a fifth code."""
    base = dict(similarity="98", invoiceNumber="INV-1", vendorName="Acme")
    assert warning("duplicate_similar", "warning", **base, crossEntityCount=0)["message"] == (
        "Potential duplicate: 98% match to INV-1 from Acme"
    )
    assert warning("duplicate_similar", "warning", **base, crossEntityCount=1)["message"] == (
        "Potential duplicate: 98% match to INV-1 from Acme "
        "(plus 1 near-identical invoice under another entity)"
    )


def test_render_substitutes_literally_not_by_regex():
    """A param value carrying `{`/`$` must not be re-interpreted."""
    assert render("a {x} b", {"x": "{y}$1"}) == "a {y}$1 b"


# --------------------------------------------------------------------------- #
# The generated frontend catalogue
# --------------------------------------------------------------------------- #


def test_the_committed_frontend_catalogue_is_in_sync():
    gen = importlib.import_module("scripts.gen_invoice_warning_messages")
    target = _BACKEND.parent / gen.OUTPUT_PATH
    assert target.read_text(encoding="utf-8") == gen.render(), (
        "frontend/src/lib/api/invoiceWarningMessages.generated.ts is stale — "
        "run `pnpm gen:warning-messages`"
    )


def test_the_check_goes_red_when_the_catalogue_gains_a_code(monkeypatch):
    """The drift guard actually fires, rather than passing vacuously."""
    gen = importlib.import_module("scripts.gen_invoice_warning_messages")
    widened = (
        *WARNING_SPECS,
        cat.WarningSpec("brand_new_rule", "fraud_flag", "A brand new finding", {}),
    )
    monkeypatch.setattr(gen, "WARNING_SPECS", widened)
    assert gen.main(["--check"]) == 1


def test_every_message_key_is_derived_from_its_code():
    gen = importlib.import_module("scripts.gen_invoice_warning_messages")
    assert gen.message_key("round_amount") == "invoices.warning.roundAmount"
    assert (
        gen.message_key("contract_spend_limit_exceeded_not_to_exceed")
        == "invoices.warning.contractSpendLimitExceededNotToExceed"
    )
    # Distinct codes cannot collapse onto one key — that would silently make
    # two different findings read as the same sentence.
    keys = [gen.message_key(c) for c in codes()]
    assert len(set(keys)) == len(keys)


# --------------------------------------------------------------------------- #
# The generated mobile catalogue
#
# Round 26 hand-transcribed all 48 codes into Dart and backstopped them with a
# flutter test that parsed the web's TypeScript. The generator now writes the
# Dart half from the same catalogue in the same run, so the guards move here.
# --------------------------------------------------------------------------- #


def _gen():
    return importlib.import_module("scripts.gen_invoice_warning_messages")


def test_the_committed_mobile_catalogue_is_in_sync():
    gen = _gen()
    target = _BACKEND.parent / gen.MOBILE_OUTPUT_PATH
    assert target.read_text(encoding="utf-8") == gen.render_dart(), (
        "mobile/lib/l10n/invoice_warning_messages.generated.dart is stale — "
        "run `pnpm gen:warning-messages`"
    )


def test_the_check_names_the_mobile_file_when_only_the_arb_moved(monkeypatch, capsys):
    """Reordering a placeholder in the English ARB changes the Dart method's
    signature without touching the catalogue — and the check still goes red,
    naming the mobile file and not the web one."""
    gen = _gen()
    arb = gen._load_arb()
    meta = arb["@invoiceWarningLineTotalMismatch"]["placeholders"]
    arb["@invoiceWarningLineTotalMismatch"]["placeholders"] = dict(reversed(list(meta.items())))
    monkeypatch.setattr(gen, "_load_arb", lambda: arb)

    assert gen.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert str(gen.MOBILE_OUTPUT_PATH) in err
    assert f"{gen.OUTPUT_PATH} is STALE" not in err


def test_every_arb_method_is_derived_from_its_code():
    gen = _gen()
    assert gen.arb_method("round_amount") == "invoiceWarningRoundAmount"
    assert (
        gen.arb_method("contract_spend_limit_exceeded_not_to_exceed")
        == "invoiceWarningContractSpendLimitExceededNotToExceed"
    )
    methods = [gen.arb_method(c) for c in codes()]
    assert len(set(methods)) == len(methods)
    # Every one of them is a message the English catalogue actually states.
    arb = gen._load_arb()
    assert [m for m in methods if m not in arb] == []


def _arm(dart: str, code: str) -> str:
    """The generated `case` body for one code."""
    return dart.split(f"    case '{code}':\n")[1].split("    case '")[0]


def test_the_argument_order_is_read_from_the_arb_not_the_catalogue():
    """Two `String` arguments in the wrong order compile and render a figure in
    the other's slot. The ARB is what `flutter gen-l10n` builds the signature
    from, so its order is the one the call must follow."""
    gen = _gen()
    arb = gen._load_arb()
    method = "invoiceWarningLineTotalMismatch"
    assert list(arb[f"@{method}"]["placeholders"]) == ["lineItemsTotal", "headerAmount"]
    assert f"return l.{method}(lineItemsTotal, headerAmount);" in _arm(
        gen.render_dart(arb), "line_total_mismatch"
    )

    swapped = {
        **arb,
        f"@{method}": {"placeholders": {"headerAmount": {}, "lineItemsTotal": {}}},
    }
    assert f"return l.{method}(headerAmount, lineItemsTotal);" in _arm(
        gen.render_dart(swapped), "line_total_mismatch"
    )


def test_each_argument_is_formatted_by_its_kind_and_its_arb_type():
    gen = _gen()
    dart = gen.render_dart()
    round_amount = _arm(dart, "round_amount")
    # Money is formatted in the invoice's own currency; the currency itself is
    # not a placeholder, it is how the money beside it renders.
    assert "final amount = _money(p['amount'], currency);" in round_amount
    assert "p['currency']" not in round_amount
    # A plural selector is the int the ARB declares; a `count` in a String slot
    # (a line number) is the server's own digits.
    assert "final crossEntityCount = _count(p['crossEntityCount']);" in _arm(
        dart, "duplicate_similar"
    )
    assert "final lineNumber = _text(p['lineNumber'], currency);" in _arm(
        dart, "self_correction_line_item_math"
    )
    # A parameterless message is a getter, not a call.
    assert "return l.invoiceWarningMissingVendorName;" in _arm(dart, "missing_vendor_name")


def test_an_arb_that_disagrees_with_the_catalogue_refuses_to_generate(monkeypatch, capsys):
    gen = _gen()
    arb = gen._load_arb()
    arb["@invoiceWarningPoNotFound"] = {"placeholders": {"poNumber": {}, "extra": {}}}
    with pytest.raises(gen.ArbMismatch, match="invoiceWarningPoNotFound"):
        gen.render_dart(arb)

    # ...and the CLI reports it rather than writing a half-right file.
    monkeypatch.setattr(gen, "_load_arb", lambda: arb)
    assert gen.main([]) == 1
    assert "invoiceWarningPoNotFound" in capsys.readouterr().err


def test_an_int_placeholder_must_be_a_count():
    gen = _gen()
    arb = gen._load_arb()
    arb["@invoiceWarningPoNotFound"] = {"placeholders": {"poNumber": {"type": "int"}}}
    with pytest.raises(gen.ArbMismatch, match="only a `count`"):
        gen.render_dart(arb)


def test_a_code_the_arb_does_not_state_yet_still_gets_an_arm(monkeypatch):
    """The arm is emitted in catalogue order and calls a method `app_en.arb`
    lacks, so `flutter analyze` goes red until the English message exists —
    the mobile counterpart of `satisfies Record<string, MessageKey>`."""
    gen = _gen()
    widened = (
        *WARNING_SPECS,
        cat.WarningSpec(
            "brand_new_rule",
            "fraud_flag",
            "{days, plural, one {# day} other {# days}} late on {poNumber}",
            {"days": "count", "poNumber": "text", "currency": "currency"},
        ),
    )
    monkeypatch.setattr(gen, "WARNING_SPECS", widened)
    arm = _arm(gen.render_dart(), "brand_new_rule")
    assert "final days = _count(p['days']);" in arm
    assert "final poNumber = _text(p['poNumber'], currency);" in arm
    assert "return l.invoiceWarningBrandNewRule(days, poNumber);" in arm
