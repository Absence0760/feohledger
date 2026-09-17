"""LLM-based invoice anomaly detection.

The rule-based fraud signals in `invoice_warnings.refresh_warnings`
catch known patterns (round amounts, future dates, bank-account
changes, statistical outliers). They miss subtler ones — a vendor
suddenly billing for a service they don't normally provide, an
invoice that drops a required reference number every other invoice
from this vendor includes, a payment-method change that doesn't trip
the bank-change rule because remit-to is unchanged but the rails
shift wire→ACH.

This service feeds the new invoice plus the vendor's last N approved
invoices to Claude with an "is this in-pattern?" prompt and emits a
structured decision. It's opt-in per org because every extraction
costs an LLM call.

Failure-soft contract: any error (no API key, network, malformed
response) returns `AnomalyResult(is_anomaly=False, reason=None)`.
The fraud-detection layer is defensive — never let a flaky LLM block
AP from working.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.schemas.money import json_money

logger = logging.getLogger(__name__)

# History size we send. Smaller = cheaper + tighter context window.
# 8 covers ~1-2 quarters of vendor activity for most SMBs.
HISTORY_SIZE = 8


@dataclass
class AnomalyResult:
    is_anomaly: bool
    reason: str | None = None
    confidence: float | None = None


@dataclass
class HistoricalInvoice:
    """Compact summary of an approved invoice — what the LLM gets to see.

    Excludes file_url, line items, etc. — those would bloat the
    prompt without adding signal for the "in-pattern?" judgement.
    """

    invoice_number: str
    invoice_date: str | None
    amount: Decimal
    currency: str
    description: str | None
    payment_method: str | None
    remit_to_address: str | None
    po_number: str | None


@dataclass
class CandidateInvoice:
    """Same shape as HistoricalInvoice but for the new arrival."""

    invoice_number: str
    invoice_date: str | None
    amount: Decimal
    currency: str
    description: str | None
    payment_method: str | None
    remit_to_address: str | None
    po_number: str | None
    vendor_name: str


_PROMPT = """You are reviewing accounts-payable invoices for fraud / anomaly signals.

Below are the last {history_count} approved invoices from vendor "{vendor}" \
(oldest → newest), followed by the candidate invoice we're evaluating.

Decide whether the candidate is *in-pattern* for this vendor or *anomalous*.
Look for things rule-based checks miss: subtle service-description shifts, \
payment-method changes, missing fields the vendor normally provides, amount \
patterns that don't fit even though they're inside the statistical band.

Approved history (JSON):
{history_json}

Candidate (JSON):
{candidate_json}

Respond with a single JSON object, no surrounding prose:

{{
  "is_anomaly": <true|false>,
  "reason": "<one short sentence; null when is_anomaly is false>",
  "confidence": <0.0–1.0>
}}
"""


def _serialise_history(history: list[HistoricalInvoice]) -> str:
    return json.dumps(
        [
            {
                "invoice_number": h.invoice_number,
                "invoice_date": h.invoice_date,
                "amount": json_money(h.amount),
                "currency": h.currency,
                "description": h.description,
                "payment_method": h.payment_method,
                "remit_to_address": h.remit_to_address,
                "po_number": h.po_number,
            }
            for h in history
        ],
        indent=2,
    )


def _serialise_candidate(candidate: CandidateInvoice) -> str:
    return json.dumps(
        {
            "invoice_number": candidate.invoice_number,
            "invoice_date": candidate.invoice_date,
            "amount": json_money(candidate.amount),
            "currency": candidate.currency,
            "description": candidate.description,
            "payment_method": candidate.payment_method,
            "remit_to_address": candidate.remit_to_address,
            "po_number": candidate.po_number,
            "vendor_name": candidate.vendor_name,
        },
        indent=2,
    )


def build_prompt(candidate: CandidateInvoice, history: list[HistoricalInvoice]) -> str:
    """Render the anomaly-detection prompt. Exposed for tests so the
    contract with the LLM stays asserted as we evolve it."""
    return _PROMPT.format(
        history_count=len(history),
        vendor=candidate.vendor_name,
        history_json=_serialise_history(history),
        candidate_json=_serialise_candidate(candidate),
    )


def parse_response(text: str) -> AnomalyResult:
    """Pull the JSON object out of the model's response. Tolerates
    code-fenced output and surrounding prose because models drift."""
    text = text.strip()
    # Strip Markdown code fences the model sometimes wraps around JSON.
    if text.startswith("```"):
        # ```json\n{...}\n```  or  ```\n{...}\n```
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: find the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return AnomalyResult(is_anomaly=False)
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return AnomalyResult(is_anomaly=False)

    return AnomalyResult(
        is_anomaly=bool(data.get("is_anomaly", False)),
        reason=data.get("reason") if data.get("is_anomaly") else None,
        confidence=float(data["confidence"]) if "confidence" in data else None,
    )


async def detect_anomaly(
    candidate: CandidateInvoice,
    history: list[HistoricalInvoice],
    *,
    api_key: str | None,
    model: str = "claude-sonnet-4-20250514",
    http_post=None,
) -> AnomalyResult:
    """Send the prompt, parse the response, fail soft.

    `http_post` is an injection point for tests — defaults to
    `httpx.AsyncClient.post`. Production code never passes it.

    Returns `is_anomaly=False` when:
      - api_key is missing
      - history is empty (no baseline → can't judge)
      - the API call fails or the response can't be parsed
    """
    if not api_key:
        logger.debug("LLM anomaly detection skipped: no API key configured")
        return AnomalyResult(is_anomaly=False)
    if not history:
        # Without history we have nothing to compare against. The
        # statistical-anomaly rule has the same gate.
        return AnomalyResult(is_anomaly=False)

    prompt = build_prompt(candidate, history)
    body = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        if http_post is not None:
            resp = await http_post(json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=body,
                    headers=headers,
                )
    except Exception:
        logger.exception("LLM anomaly detection: API call failed")
        return AnomalyResult(is_anomaly=False)

    if resp.status_code != 200:
        logger.warning(
            "LLM anomaly detection: API returned %s: %s",
            resp.status_code,
            resp.text[:200],
        )
        return AnomalyResult(is_anomaly=False)

    data = resp.json()
    blocks = data.get("content") or []
    text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
    if not text:
        return AnomalyResult(is_anomaly=False)

    return parse_response(text)


def invoice_to_candidate(invoice) -> CandidateInvoice:
    """Adapter from a SQLAlchemy `Invoice` (or test stub) to the
    LLM-ready dataclass."""
    return CandidateInvoice(
        invoice_number=invoice.invoice_number or "",
        invoice_date=invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        amount=invoice.amount if invoice.amount is not None else Decimal("0"),
        currency=invoice.currency or "USD",
        description=invoice.description,
        payment_method=invoice.payment_method,
        remit_to_address=invoice.remit_to_address,
        po_number=invoice.po_number,
        vendor_name=invoice.vendor_name or "",
    )


def invoice_to_history(invoice) -> HistoricalInvoice:
    """Adapter for historical Invoice rows."""
    return HistoricalInvoice(
        invoice_number=invoice.invoice_number or "",
        invoice_date=invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        amount=invoice.amount if invoice.amount is not None else Decimal("0"),
        currency=invoice.currency or "USD",
        description=invoice.description,
        payment_method=invoice.payment_method,
        remit_to_address=invoice.remit_to_address,
        po_number=invoice.po_number,
    )
