"""Claude Vision extraction adapter — uses Anthropic's Claude API for invoice OCR.

This is the platform default. Handles PDFs and images with structured extraction prompts.

Request shape (Messages API, raw ``httpx`` — house style, see
``backend/docs/ai-extraction.md`` § Claude request shape). It is written to be
accepted by every current Claude model, because a BYOK org may name any of them:

* **No ``thinking`` field.** Current models (Sonnet 5.5, Opus 5.5) run adaptive
  thinking when it is omitted; ``{"type": "disabled"}`` and ``budget_tokens``
  are a 400 on them, and ``between_tools`` is a 400 on every model but Sonnet
  5.5. Omitting it is the one spelling all of them accept.
* **No ``temperature`` / ``top_p`` / ``top_k``, no assistant prefill, no forced
  ``tool_choice``** — each is a 400 on current models. The JSON comes back as
  plain text and is parsed by block ``type`` (a thinking-on response can start
  with a ``thinking`` block), never by position.
* **``output_config.effort`` only when configured** (``FEOH_EXTRACTION_EFFORT``
  for platform mode, ``settings.extraction.effort`` for BYOK). Effort is
  rejected by older models (Haiku 4.5, Sonnet 4.5), so it is never sent unasked.
* **``max_tokens`` sized for thinking as well as the JSON** — thinking counts
  toward it even when its text is not returned.
"""

import base64
import json

import httpx

from app.config import settings
from app.services.extraction_adapters.base import (
    STATEMENT_REASON_EMPTY_FILE,
    STATEMENT_REASON_PROVIDER_ERROR,
    STATEMENT_REASON_UNREADABLE,
    ExtractedField,
    ExtractedLineItem,
    ExtractionAdapter,
    ExtractionResult,
    ExtractionTokenUsage,
    StatementExtractionResult,
    coerce_confidence,
)
from app.services.extraction_adapters.dispatcher import register_extraction_adapter
from app.services.extraction_adapters.statement_extraction import (
    STATEMENT_EXTRACTION_PROMPT,
    parse_statement_payload,
)

_EXTRACTION_PROMPT_TEMPLATE = """You are an invoice data extraction system. \
Extract all fields from this invoice image/document.

Return a JSON object with the following structure. For each field, \
provide the value and a confidence score between 0.0 and 1.0. \
Use these confidence ranges:
- 0.95-1.0: field is clearly printed and unambiguous
- 0.8-0.94: field is legible but could have a minor read error
- 0.5-0.79: field is partially obscured, blurry, or you are guessing
- 0.1-0.49: field is barely visible or mostly inferred from context
- null value with 0.0: field is not present on the document

Do NOT default to 1.0 for all fields. Be honest about uncertainty.

```json
{
  "invoice_number": {"value": "string", "confidence": 0.95},
  "vendor_name": {"value": "string", "confidence": 0.9},
  "vendor_address": {"value": "string", "confidence": 0.85},
  "vendor_tax_id": {"value": "string or null", "confidence": 0.7},
  "amount": {"value": "decimal string", "confidence": 0.95},
  "currency": {"value": "3-letter code", "confidence": 0.9},
  "subtotal": {"value": "decimal string or null", "confidence": 0.85},
  "tax_amount": {"value": "decimal string or null", "confidence": 0.8},
  "tax_rate": {"value": "decimal string or null", "confidence": 0.6},
  "discount_amount": {"value": "decimal string or null", "confidence": 0.5},
  "shipping_amount": {"value": "decimal string or null", "confidence": 0.5},
  "invoice_date": {"value": "YYYY-MM-DD", "confidence": 0.95},
  "due_date": {"value": "YYYY-MM-DD or null", "confidence": 0.85},
  "payment_terms": {"value": "string or null", "confidence": 0.7},
  "po_number": {"value": "string or null", "confidence": 0.8},
  "description": {"value": "brief description of invoice contents", "confidence": 0.75},
  "reference_number": {"value": "string or null", "confidence": 0.6},
  "payment_method": {"value": "ach|wire|check|credit_card or null", "confidence": 0.5},
  "bill_to_address": {"value": "string or null", "confidence": 0.7},
  "remit_to_address": {"value": "string or null", "confidence": 0.6},
  "suggested_gl_account": {"value": "GL code or null", "confidence": 0.5},
  "suggested_cost_center": {"value": "cost center or null", "confidence": 0.4},
  "line_items": [
    {
      "line_number": 1,
      "item_code": {"value": "string or null", "confidence": 0.7},
      "description": {"value": "string", "confidence": 0.85},
      "quantity": {"value": "decimal string", "confidence": 0.9},
      "unit_price": {"value": "decimal string", "confidence": 0.9},
      "tax": {"value": "decimal string or null", "confidence": 0.6},
      "total": {"value": "decimal string", "confidence": 0.95}
    }
  ]
}
```

For GL account suggestion, use ONLY codes from this chart of accounts:
{{GL_ACCOUNT_CATALOG}}

Return ONLY the JSON object, no other text."""

_GL_PLACEHOLDER = "{{GL_ACCOUNT_CATALOG}}"

_DEFAULT_GL_LIST = """\
- Office supplies → 6100
- Cloud/software services → 6200
- Facility/maintenance → 6300
- Marketing → 6400
- Legal/professional → 6500
- Food/catering → 6600
- Shipping/logistics → 6700
- Hardware/equipment → 1500"""

# Backward-compatible constant with the default GL list baked in.
# Other adapters (openai_vision, ollama) import this directly.
EXTRACTION_PROMPT = _EXTRACTION_PROMPT_TEMPLATE.replace(_GL_PLACEHOLDER, _DEFAULT_GL_LIST)


def _strip_json_fence(text: str) -> str:
    """Unwrap a ```json ... ``` fence the model sometimes adds around its JSON."""
    json_str = text.strip()
    if json_str.startswith("```"):
        json_str = json_str.split("```")[1]
        if json_str.startswith("json"):
            json_str = json_str[4:]
    return json_str.strip()


_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# Non-streaming ceiling. Thinking tokens count toward it, so it is well above
# the size of the JSON itself; it is a cap, not a charge — billing is on what
# the model actually produced.
_MAX_TOKENS = 16000

# A thinking-on model reading a multi-page document takes longer than the old
# no-thinking default did, and 60 s would cut real reads off mid-response.
_TIMEOUT_SECONDS = 300


def _model(config: dict) -> str:
    """The configured model, else the platform default — never a hardcoded id.

    A BYOK org that picked ``claude_vision`` without naming a model gets the
    platform's current default rather than a pinned id that Anthropic later
    retires out from under it.
    """
    return config.get("model") or settings.extraction_model


def _request_body(config: dict, content: list[dict]) -> dict:
    body: dict = {
        "model": _model(config),
        "max_tokens": _MAX_TOKENS,
        "messages": [{"role": "user", "content": content}],
    }
    effort = config.get("effort")
    if effort:
        body["output_config"] = {"effort": effort}
    return body


def _headers(config: dict) -> dict:
    return {
        "x-api-key": config.get("api_key", ""),
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _response_text(resp_data: dict) -> str:
    """Concatenate the ``text`` blocks; ``thinking`` blocks are skipped."""
    return "".join(
        block.get("text", "")
        for block in resp_data.get("content") or []
        if block.get("type") == "text"
    )


def _stop_problem(resp_data: dict) -> str | None:
    """Why a 200 response carries no usable answer, or ``None`` when it does.

    A ``refusal`` (a safety classifier declined, HTTP 200) or a ``max_tokens``
    cut-off both used to surface as "Failed to parse JSON", which sent the
    operator looking at the prompt instead of at the stop reason.
    """
    stop_reason = resp_data.get("stop_reason")
    if stop_reason == "refusal":
        details = resp_data.get("stop_details") or {}
        category = details.get("category") if isinstance(details, dict) else None
        return f"Claude declined the request (refusal, category={category})"
    if stop_reason == "max_tokens":
        return f"Claude response truncated at max_tokens={_MAX_TOKENS}"
    return None


def _document_block(file_bytes: bytes, mime_type: str) -> dict:
    """Build the Anthropic content block for a PDF page set or a single image."""
    if mime_type in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"):
        media_type = mime_type
    else:
        media_type = "application/pdf"
    return {
        "type": "document" if media_type == "application/pdf" else "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(file_bytes).decode("utf-8"),
        },
    }


def _parse_field(data: dict | None, field_name: str) -> ExtractedField:
    if not data or field_name not in data:
        return ExtractedField(None, 0.0)
    field = data[field_name]
    if isinstance(field, dict):
        return ExtractedField(field.get("value"), coerce_confidence(field.get("confidence")))
    return ExtractedField(str(field), 0.5)


@register_extraction_adapter("claude_vision")
class ClaudeVisionAdapter(ExtractionAdapter):
    """Extract invoice data using Claude's vision capabilities.

    Required config:
        api_key: Anthropic API key
        model: Model to use (default: ``FEOH_EXTRACTION_MODEL``)
        effort: optional ``output_config.effort`` (omitted when empty)
    """

    provider_name = "claude_vision"

    async def extract(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
        file_url: str = "",
    ) -> ExtractionResult:
        if not file_bytes:
            return ExtractionResult(
                success=False, error="No file bytes provided", provider=self.provider_name
            )

        # Build the extraction prompt — inject org-specific GL catalog if
        # available, otherwise fall back to the default hardcoded list.
        gl_catalog = self.config.get("gl_account_catalog")
        if gl_catalog:
            base_prompt = _EXTRACTION_PROMPT_TEMPLATE.replace(_GL_PLACEHOLDER, gl_catalog)
        else:
            base_prompt = EXTRACTION_PROMPT

        # RAG few-shot context (retrieved by services.extraction.run_extraction
        # and passed through the adapter config). Prepend as a preamble so the
        # extraction prompt that follows stays authoritative.
        few_shot = self.config.get("few_shot_prompt") or ""
        prompt_text = base_prompt
        if few_shot:
            prompt_text = f"{few_shot}\n\n---\n\n{base_prompt}"

        body = _request_body(
            self.config,
            [_document_block(file_bytes, mime_type), {"type": "text", "text": prompt_text}],
        )

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(_API_URL, json=body, headers=_headers(self.config))
        except Exception as exc:
            return ExtractionResult(
                success=False,
                error=f"API call failed: {exc}",
                provider=self.provider_name,
            )

        if resp.status_code != 200:
            return ExtractionResult(
                success=False,
                error=f"Claude API error {resp.status_code}: {resp.text}",
                provider=self.provider_name,
            )

        resp_data = resp.json()
        # Captured before any parse can fail: a refused, truncated or
        # unparseable read was still billed, and the meter must say so.
        usage = ExtractionTokenUsage.from_anthropic(resp_data, body["model"])

        problem = _stop_problem(resp_data)
        if problem is not None:
            return ExtractionResult(
                success=False, error=problem, provider=self.provider_name, usage=usage
            )

        text_content = _response_text(resp_data)
        try:
            data = json.loads(_strip_json_fence(text_content))
        except json.JSONDecodeError:
            return ExtractionResult(
                success=False,
                error="Failed to parse JSON from Claude response",
                raw_response={"text": text_content},
                provider=self.provider_name,
                usage=usage,
            )

        # Build result
        result = ExtractionResult(
            success=True,
            invoice_number=_parse_field(data, "invoice_number"),
            vendor_name=_parse_field(data, "vendor_name"),
            vendor_address=_parse_field(data, "vendor_address"),
            vendor_tax_id=_parse_field(data, "vendor_tax_id"),
            amount=_parse_field(data, "amount"),
            currency=_parse_field(data, "currency"),
            subtotal=_parse_field(data, "subtotal"),
            tax_amount=_parse_field(data, "tax_amount"),
            tax_rate=_parse_field(data, "tax_rate"),
            discount_amount=_parse_field(data, "discount_amount"),
            shipping_amount=_parse_field(data, "shipping_amount"),
            invoice_date=_parse_field(data, "invoice_date"),
            due_date=_parse_field(data, "due_date"),
            payment_terms=_parse_field(data, "payment_terms"),
            po_number=_parse_field(data, "po_number"),
            description=_parse_field(data, "description"),
            reference_number=_parse_field(data, "reference_number"),
            payment_method=_parse_field(data, "payment_method"),
            bill_to_address=_parse_field(data, "bill_to_address"),
            remit_to_address=_parse_field(data, "remit_to_address"),
            suggested_gl_account=_parse_field(data, "suggested_gl_account"),
            suggested_cost_center=_parse_field(data, "suggested_cost_center"),
            raw_response=data,
            provider=self.provider_name,
            usage=usage,
        )

        # Parse line items
        for li in data.get("line_items", []):
            result.line_items.append(
                ExtractedLineItem(
                    line_number=li.get("line_number", 0),
                    item_code=_parse_field(li, "item_code"),
                    description=_parse_field(li, "description"),
                    quantity=_parse_field(li, "quantity"),
                    unit_price=_parse_field(li, "unit_price"),
                    tax=_parse_field(li, "tax"),
                    total=_parse_field(li, "total"),
                )
            )

        # Calculate overall confidence
        fields = [
            result.invoice_number,
            result.vendor_name,
            result.amount,
            result.invoice_date,
            result.due_date,
        ]
        confidences = [f.confidence for f in fields if f.value is not None]
        result.overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        return result

    async def extract_statement(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
    ) -> StatementExtractionResult:
        """Read a supplier statement of open items via Claude's vision path.

        Same document channel as :meth:`extract` — only the prompt and the
        response shape differ, because a statement is many rows for one
        supplier rather than one invoice header. Never raises: a transport or
        provider failure comes back as ``success=False`` with a PII-free
        ``reason``; the provider's own text stays on ``error`` for the log.
        """
        if not file_bytes:
            return StatementExtractionResult(
                available=True, provider=self.provider_name, reason=STATEMENT_REASON_EMPTY_FILE
            )

        body = _request_body(
            self.config,
            [
                _document_block(file_bytes, mime_type),
                {"type": "text", "text": STATEMENT_EXTRACTION_PROMPT},
            ],
        )

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(_API_URL, json=body, headers=_headers(self.config))
        except Exception as exc:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_PROVIDER_ERROR,
                error=f"API call failed: {exc}",
            )

        if resp.status_code != 200:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_PROVIDER_ERROR,
                error=f"Claude API error {resp.status_code}",
            )

        resp_data = resp.json()
        problem = _stop_problem(resp_data)
        if problem is not None:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_UNREADABLE,
                error=problem,
            )

        text_content = _response_text(resp_data)

        try:
            data = json.loads(_strip_json_fence(text_content))
        except json.JSONDecodeError:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_UNREADABLE,
                error="Failed to parse JSON from Claude response",
            )

        return parse_statement_payload(data, self.provider_name)

    async def test_connection(self) -> bool:
        """A minimal Messages call against the CONFIGURED model.

        It used to ping a hardcoded (since deprecated) model id, so it tested a
        model the org's extractions never ran on. A tiny ``max_tokens`` is
        fine: a reply cut off at the cap is still HTTP 200, and 200 is all this
        checks — the key, the model id and the account's access to it.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    _API_URL,
                    json={
                        "model": _model(self.config),
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    headers=_headers(self.config),
                )
            return resp.status_code == 200
        except Exception:
            return False
