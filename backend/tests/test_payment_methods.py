"""Payment-rail classification + its drift guard.

``services/payment_methods`` decides two things about every ``Payment.method``
rail:

  * which the payer reports on a 1099 and which the card settlement entity
    reports on a 1099-K — getting that wrong files a wrong number with the IRS
    in either direction; and
  * which rails are **international** — which decides whether an FX rate is
    locked onto the row, whether a caller's corridor override is trusted, and
    whether the vendor must be KYC-verified.

Both registries have to stay exhaustive, so the drift tests below fail the
moment a rail exists anywhere in the codebase without BOTH treatments.
"""

from __future__ import annotations

import pytest

from app.schemas.payment import PaymentMethod
from app.services.payment_corridor import CORRIDOR_OVERRIDE_FEES
from app.services.payment_methods import (
    CARD_PAYMENT_METHODS,
    DOMESTIC_PAYMENT_METHODS,
    INTERNATIONAL_PAYMENT_METHODS,
    IRS_1099_REPORTABLE_METHODS,
    KNOWN_PAYMENT_METHODS,
    is_1099_reportable_method,
    is_card_payment_method,
    is_international_payment_method,
    normalize_payment_method,
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_card_and_reportable_sets_are_disjoint():
    assert CARD_PAYMENT_METHODS & IRS_1099_REPORTABLE_METHODS == frozenset()
    assert KNOWN_PAYMENT_METHODS == CARD_PAYMENT_METHODS | IRS_1099_REPORTABLE_METHODS


def test_virtual_card_is_the_card_rail():
    """The only rail settled over a payment card — the issuer/network files
    the 1099-K, so it must never reach our 1099 box amount."""
    assert is_card_payment_method("virtual_card") is True
    assert is_1099_reportable_method("virtual_card") is False


@pytest.mark.parametrize(
    "method",
    [
        "ach",
        "check",
        "wire",
        "rtp",
        "sepa",
        "international_ach",
        "international_wire",
        "bacs",
        "faster_payments",
        "chaps",
    ],
)
def test_bank_rails_are_1099_reportable(method):
    """ACH / cheque / wire / RTP / SEPA / IAT + the UK domestic rails
    (BACS / Faster Payments / CHAPS) are all direct payments by the payer out
    of a bank account — squarely 1099-NEC/MISC reportable. Excluding any of
    them would UNDER-report, which is as wrong as over-reporting."""
    assert is_card_payment_method(method) is False
    assert is_1099_reportable_method(method) is True


@pytest.mark.parametrize("value", [None, "", "   ", "some_future_rail"])
def test_unknown_or_null_rail_stays_reportable(value):
    """NULL is what the manual / legacy payment paths write. Defaulting an
    unclassified rail OUT of the report would silently drop filed money."""
    assert is_card_payment_method(value) is False
    assert is_1099_reportable_method(value) is True


@pytest.mark.parametrize("value", ["VIRTUAL_CARD", " Virtual_Card ", "virtual_card "])
def test_classification_is_case_and_whitespace_insensitive(value):
    assert normalize_payment_method(value) == "virtual_card"
    assert is_card_payment_method(value) is True


# ---------------------------------------------------------------------------
# Geography classification
# ---------------------------------------------------------------------------


def test_international_and_domestic_sets_are_disjoint_and_cover_every_known_rail():
    """A rail is international or domestic — never both, never neither. The
    'neither' half is what matters: `compliance._kyc_required_for` treats an
    unclassified rail as low-risk, so an unclassified international rail would
    fail OPEN on the KYC gate."""
    assert INTERNATIONAL_PAYMENT_METHODS & DOMESTIC_PAYMENT_METHODS == frozenset()
    assert INTERNATIONAL_PAYMENT_METHODS | DOMESTIC_PAYMENT_METHODS == KNOWN_PAYMENT_METHODS


@pytest.mark.parametrize("method", ["sepa", "international_ach", "international_wire"])
def test_international_rails(method):
    assert is_international_payment_method(method) is True


@pytest.mark.parametrize(
    "method",
    ["ach", "check", "wire", "rtp", "virtual_card", "bacs", "faster_payments", "chaps"],
)
def test_domestic_rails(method):
    assert is_international_payment_method(method) is False


@pytest.mark.parametrize("value", [None, "", "   ", "some_future_rail"])
def test_unknown_or_null_rail_is_not_international(value):
    """`pick_corridor` must not honour an unrecognised `requested_method` as a
    deliberate international override — it falls through to auto-selection."""
    assert is_international_payment_method(value) is False


@pytest.mark.parametrize("value", ["SEPA", " Sepa ", "International_Wire"])
def test_geography_is_case_and_whitespace_insensitive(value):
    assert is_international_payment_method(value) is True


# ---------------------------------------------------------------------------
# Drift guard — every rail the codebase can produce must be classified
# ---------------------------------------------------------------------------


def _unclassified(methods) -> set[str]:
    return {m for m in methods if normalize_payment_method(m) not in KNOWN_PAYMENT_METHODS}


def test_every_schema_enum_rail_is_classified():
    missing = _unclassified(m.value for m in PaymentMethod)
    assert not missing, (
        f"PaymentMethod rails with no 1099 treatment: {sorted(missing)}. "
        "Add each to CARD_PAYMENT_METHODS or IRS_1099_REPORTABLE_METHODS in "
        "app/services/payment_methods.py."
    )


def test_every_adapter_supported_rail_is_classified():
    # Importing the package self-registers every adapter.
    import app.services.payment_adapters  # noqa: F401, PLC0415
    from app.services.payment_adapters.dispatcher import _ADAPTER_REGISTRY  # noqa: PLC0415

    offered: set[str] = set()
    for adapter_cls in _ADAPTER_REGISTRY.values():
        offered.update(getattr(adapter_cls, "supported_methods", ()) or ())
    assert offered, "no payment adapters registered — the guard would pass vacuously"

    missing = _unclassified(offered)
    assert not missing, (
        f"payment-adapter rails with no 1099 treatment: {sorted(missing)}. "
        "Add each to CARD_PAYMENT_METHODS or IRS_1099_REPORTABLE_METHODS in "
        "app/services/payment_methods.py."
    )


def test_every_corridor_rail_is_classified():
    missing = _unclassified(CORRIDOR_OVERRIDE_FEES)
    assert not missing, (
        f"corridor rails with no 1099 treatment: {sorted(missing)}. "
        "Add each to CARD_PAYMENT_METHODS or IRS_1099_REPORTABLE_METHODS in "
        "app/services/payment_methods.py."
    )


def test_registry_carries_no_rail_the_codebase_cannot_produce():
    """The reverse direction — a stale entry here would be dead weight that
    quietly claims a treatment for a rail nobody writes any more."""
    import app.services.payment_adapters  # noqa: F401, PLC0415
    from app.services.payment_adapters.dispatcher import _ADAPTER_REGISTRY  # noqa: PLC0415

    producible = {m.value for m in PaymentMethod} | set(CORRIDOR_OVERRIDE_FEES)
    for adapter_cls in _ADAPTER_REGISTRY.values():
        producible.update(getattr(adapter_cls, "supported_methods", ()) or ())

    stale = KNOWN_PAYMENT_METHODS - {normalize_payment_method(m) for m in producible}
    assert not stale, f"classified rails nothing can produce: {sorted(stale)}"


def test_no_module_hand_rolls_the_international_rail_set():
    """`payment_corridor`, `compliance` and `api/payments` each used to carry
    their own copy of the international rail list, so adding a fourth rail meant
    remembering three places — and the `compliance` copy drives the KYC gate.

    This scans `app/` for any collection literal that re-enumerates the set.
    If it fires, import `INTERNATIONAL_PAYMENT_METHODS` (or
    `is_international_payment_method`) from `services/payment_methods` instead
    of restating it.
    """
    import ast  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    owner = app_dir / "services" / "payment_methods.py"

    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        if path == owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Set | ast.List | ast.Tuple):
                elements = node.elts
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "frozenset"
                and node.args
                and isinstance(node.args[0], ast.Set | ast.List | ast.Tuple)
            ):
                elements = node.args[0].elts
            else:
                continue
            literals = {
                normalized
                for e in elements
                if isinstance(e, ast.Constant)
                and isinstance(e.value, str)
                and (normalized := normalize_payment_method(e.value))
            }
            # Only a collection made up *entirely* of international rails is a
            # copy of the set. A wider rail list (an adapter's
            # `supported_methods`, a fee table) legitimately names them
            # alongside the domestic rails and is not a duplicate registry.
            if len(literals) >= 2 and literals <= INTERNATIONAL_PAYMENT_METHODS:
                offenders.append(f"{path.relative_to(app_dir.parent)}:{node.lineno}")

    assert not offenders, (
        "the international rail set is re-enumerated at: "
        f"{offenders}. Import INTERNATIONAL_PAYMENT_METHODS / "
        "is_international_payment_method from app/services/payment_methods.py."
    )


def test_card_is_a_record_only_rail_off_the_1099():
    """`card` — a company card used outside FeohLedger (decisions §295): off
    the 1099 like `virtual_card`, recordable, and refused on every payment
    FeohLedger would dispatch (a standalone payment and a run item)."""
    import pytest
    from pydantic import ValidationError

    from app.api.payments import CreatePaymentRunItem
    from app.schemas.payment import PaymentCreate, RecordPaymentOutsideRequest
    from app.services.payment_methods import CARD_PAYMENT_METHODS, IRS_1099_REPORTABLE_METHODS

    assert "card" in CARD_PAYMENT_METHODS and "card" not in IRS_1099_REPORTABLE_METHODS
    with pytest.raises(ValidationError):
        PaymentCreate(invoice_id="x", method="card")
    with pytest.raises(ValidationError):
        CreatePaymentRunItem(invoice_id="x", method="card")
    recorded = RecordPaymentOutsideRequest(
        invoice_id="00000000-0000-0000-0000-000000000001",
        method="card",
        reference="AMEX-1",
        paid_on="2026-10-01",
    )
    assert recorded.method == "card"
