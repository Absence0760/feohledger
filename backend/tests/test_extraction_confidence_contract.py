"""A model's self-rated confidence is not a validated input.

The prompt asks for 0.0–1.0. What arrived went straight onto ``ExtractedField``:

* ``null`` / a string / any non-number made ``sum(confidences)`` raise
  ``TypeError`` inside ``extract()`` — an extraction whose VALUES were all read
  correctly failed outright, landing the invoice in ``failed`` with an
  ``extraction_failed`` exception to re-key by hand.
* a value **outside 0–1** lifted the MEAN past ``auto_approve_threshold``. One
  field at ``3`` among four at ``0.5`` averages exactly 1.0, fits the
  ``Numeric(5, 4)`` column, persists, and auto-approves an invoice the model
  actually rated 0.5 — straight past human review.

``extraction_adapters.base.coerce_confidence`` is the contract, shared by every
adapter that parses this prompt (`claude_vision` / `openai_vision` / `ollama`
and the statement reader). ``decide_auto_approve`` guards the gate itself, so a
future adapter computing its own overall score can't trip it either.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services.extraction import decide_auto_approve
from app.services.extraction_adapters.base import coerce_confidence
from app.services.extraction_adapters.claude_vision import ClaudeVisionAdapter

# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.0, 0.0),
        (0.5, 0.5),
        (1.0, 1.0),
        (1, 1.0),
        ("0.97", 0.97),  # a stringified number is still a number
        ("  0.8 ", 0.8),
    ],
)
def test_in_contract_values_pass_through(raw, expected):
    assert coerce_confidence(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw",
    [
        None,  # JSON null — used to raise on sum()
        "high",
        "",
        {},
        [],
        True,  # JSON true is an int in Python; it is not a confidence
        False,
        95,  # a 0-100 scale — the auto-approve bypass
        3,
        1.0001,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_out_of_contract_values_fail_closed_to_zero(raw):
    """0.0, never a clamp to 1.0 — an uninterpretable number must not be able to
    authorise an unattended approval."""
    assert coerce_confidence(raw) == 0.0


# --------------------------------------------------------------------------- #
# End to end through the adapter every model-backed provider shares
# --------------------------------------------------------------------------- #


def _extract(model_json: dict, monkeypatch) -> object:
    payload = {"content": [{"type": "text", "text": json.dumps(model_json)}]}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return asyncio.run(
        ClaudeVisionAdapter({"api_key": "k"}).extract(file_bytes=b"%PDF-1.4", file_key="x.pdf")
    )


def _model_json(amount_confidence):
    return {
        "invoice_number": {"value": "INV-1", "confidence": 0.5},
        "vendor_name": {"value": "Acme", "confidence": 0.5},
        "amount": {"value": "9500.00", "confidence": amount_confidence},
        "invoice_date": {"value": "2026-05-01", "confidence": 0.5},
        "due_date": {"value": "2026-05-31", "confidence": 0.5},
    }


def test_a_null_confidence_no_longer_fails_the_whole_extraction(monkeypatch):
    result = _extract(_model_json(None), monkeypatch)
    assert result.success is True
    assert result.error is None
    # The values were read fine; only that one field's confidence is unknown.
    assert result.amount.value == "9500.00"
    assert result.amount.confidence == 0.0
    assert 0.0 <= result.overall_confidence <= 1.0


def test_a_string_confidence_no_longer_fails_the_whole_extraction(monkeypatch):
    result = _extract(_model_json("0.97"), monkeypatch)
    assert result.success is True
    assert result.amount.confidence == pytest.approx(0.97)


def test_one_mis_scaled_field_cannot_average_its_way_into_auto_approval(monkeypatch):
    """The regression: 0.5,0.5,3,0.5,0.5 used to mean 1.0 → touchless approval."""
    result = _extract(_model_json(3), monkeypatch)
    assert result.overall_confidence <= 1.0
    assert (
        decide_auto_approve(
            {"auto_approve_enabled": True, "auto_approve_threshold": 0.95},
            {},
            overall_confidence=result.overall_confidence,
            amount="9500.00",
        )
        is False
    )


def test_an_honest_high_confidence_extraction_still_auto_approves(monkeypatch):
    result = _extract(
        {
            "invoice_number": {"value": "INV-1", "confidence": 0.99},
            "vendor_name": {"value": "Acme", "confidence": 0.98},
            "amount": {"value": "100.00", "confidence": 0.99},
            "invoice_date": {"value": "2026-05-01", "confidence": 0.97},
            "due_date": {"value": "2026-05-31", "confidence": 0.96},
        },
        monkeypatch,
    )
    assert (
        decide_auto_approve(
            {"auto_approve_enabled": True, "auto_approve_threshold": 0.95},
            {},
            overall_confidence=result.overall_confidence,
            amount="100.00",
        )
        is True
    )


# --------------------------------------------------------------------------- #
# The gate's own guard — independent of any adapter
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bogus", [95.0, 3.0, 1.5, -1.0])
def test_the_gate_refuses_an_out_of_range_overall_confidence(bogus):
    assert (
        decide_auto_approve(
            {"auto_approve_enabled": True, "auto_approve_threshold": 0.95},
            {},
            overall_confidence=bogus,
            amount="100.00",
        )
        is False
    )


@pytest.mark.parametrize("amount", ["0", "0.00", None, "-25.00"])
def test_no_amount_never_auto_approves_on_either_trigger(amount):
    """An extraction that read no amount (the upload's 0 placeholder survives —
    an unparseable token, or a currency the document contradicts) used to sail
    under any `auto_approve_below` floor and past every cap (decisions §302)."""
    assert (
        decide_auto_approve(
            {"auto_approve_enabled": True, "auto_approve_threshold": 0.5},
            {"auto_approve_below": "500"},
            overall_confidence=0.99,
            amount=amount,
        )
        is False
    )
