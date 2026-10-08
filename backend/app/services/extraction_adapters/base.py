"""Base extraction adapter interface and shared types."""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Supplier-statement extraction — machine-safe reason codes
#
# A statement read can fail for reasons the *user* needs to act on ("this scan
# has no text layer — configure a vision provider"), but an adapter's own error
# text is provider output and must never reach an HTTP body. These codes are the
# PII-free contract between an adapter and the service that surfaces the failure;
# `StatementExtractionResult.error` stays for logs only.
# --------------------------------------------------------------------------- #

STATEMENT_REASON_NOT_SUPPORTED = "not_supported"
STATEMENT_REASON_EMPTY_FILE = "empty_file"
STATEMENT_REASON_NO_TEXT_LAYER = "no_text_layer"
STATEMENT_REASON_NO_LINES = "no_lines_found"
STATEMENT_REASON_PROVIDER_ERROR = "provider_error"
STATEMENT_REASON_UNREADABLE = "unreadable_response"
# The org's `settings.extraction.provider` names an adapter we don't have. A
# config error, not a document error — and the one reason code whose fix is an
# admin's, not the uploader's.
STATEMENT_REASON_PROVIDER_UNKNOWN = "provider_not_registered"


def pdf_text_layer(pdf_bytes: bytes, *, min_chars: int = 50) -> str | None:
    """Return a PDF's embedded text layer, or ``None`` when there isn't one.

    ``None`` means "this is a scan" (or PyMuPDF is unavailable / the bytes
    aren't a readable PDF) — the caller must fall back to a vision model rather
    than treat an empty read as an empty document. ``min_chars`` is the
    "basically no text" floor: a scanned page often yields a few stray
    characters from a header stamp.

    Never raises.
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        text = text.strip()
        return text if len(text) > min_chars else None
    except ImportError:
        return None
    except Exception:
        return None


def coerce_confidence(raw) -> float:
    """A model's self-rated confidence, forced into the 0.0–1.0 contract.

    Shared by every adapter that parses this prompt's JSON (`claude_vision`,
    `openai_vision`, `ollama`), because a model's answer is not a validated
    input — and the raw value used to travel straight onto ``ExtractedField``:

    * ``null`` / a string / a missing number made ``sum(confidences)`` raise
      ``TypeError`` inside ``extract()``, failing an extraction whose VALUES
      were all read correctly — the invoice lands in ``failed`` with an
      ``extraction_failed`` exception and has to be re-keyed by hand.
    * a value **outside 0–1** (a model answering on a 0–100 scale) lifted the
      MEAN past ``auto_approve_threshold``. One field at ``3`` among four at
      ``0.5`` averages 1.0, fits ``Numeric(5, 4)``, persists, and
      ``decide_auto_approve`` approves the invoice touchlessly — an extraction
      the model actually rated 0.5 skips human review entirely.

    Out of contract is therefore **0.0, not a clamp to 1.0**: a number we
    cannot interpret must never be able to authorise an unattended approval,
    and 0.0 routes the invoice to a human, which is the safe outcome. ``bool``
    is rejected too (JSON ``true`` is not a confidence, and it is an ``int``).
    """
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, str):
        try:
            raw = float(raw.strip())
        except ValueError:
            return 0.0
    if not isinstance(raw, (int, float)):
        return 0.0
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):  # NaN / ±inf
        return 0.0
    if 0.0 <= value <= 1.0:
        return value
    return 0.0


@dataclass
class ExtractedField:
    """A single extracted field with confidence score."""

    value: str | None
    confidence: float = 0.0  # 0-1


@dataclass
class ExtractedLineItem:
    line_number: int = 0
    item_code: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    description: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    quantity: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    unit_price: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    tax: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    total: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    gl_account: ExtractedField = field(default_factory=lambda: ExtractedField(None))


def _token_count(raw) -> int | None:
    """A provider's token count as a non-negative int, or ``None`` if absent."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


@dataclass
class ExtractionTokenUsage:
    """What one extraction call consumed at the model provider.

    Recorded on the ``ExtractionUsage`` row so the operator can see the real
    cost of an AI-read invoice (``backend/docs/ai-extraction.md`` § Token
    tracking). Token counts only — never a dollar figure, because list prices
    drift and a stored cost would silently go stale.

    The four counts follow the Anthropic Messages API ``usage`` block, which
    keeps them disjoint: ``input_tokens`` is the UNCACHED input, and cache reads
    / writes are counted separately (each is priced differently). Thinking
    tokens are part of ``output_tokens``. ``model`` is the model id the provider
    reports it SERVED, which is what was billed.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    model: str | None = None

    @classmethod
    def from_anthropic(cls, resp_data: dict, requested_model: str | None) -> ExtractionTokenUsage:
        usage = resp_data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        return cls(
            input_tokens=_token_count(usage.get("input_tokens")),
            output_tokens=_token_count(usage.get("output_tokens")),
            cache_read_input_tokens=_token_count(usage.get("cache_read_input_tokens")),
            cache_creation_input_tokens=_token_count(usage.get("cache_creation_input_tokens")),
            model=_model_id(resp_data.get("model")) or _model_id(requested_model),
        )

    @classmethod
    def from_openai(cls, resp_data: dict, requested_model: str | None) -> ExtractionTokenUsage:
        """Map an OpenAI chat-completions ``usage`` onto the Anthropic shape.

        OpenAI's ``prompt_tokens`` INCLUDES cached tokens
        (``prompt_tokens_details.cached_tokens``); they are split out so
        ``input_tokens`` means "uncached input" for every provider. OpenAI
        reports no cache-write count.
        """
        usage = resp_data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        prompt = _token_count(usage.get("prompt_tokens"))
        details = usage.get("prompt_tokens_details") or {}
        cached = _token_count(details.get("cached_tokens")) if isinstance(details, dict) else None
        uncached = prompt
        if prompt is not None and cached is not None:
            uncached = max(prompt - cached, 0)
        return cls(
            input_tokens=uncached,
            output_tokens=_token_count(usage.get("completion_tokens")),
            cache_read_input_tokens=cached,
            cache_creation_input_tokens=None,
            model=_model_id(resp_data.get("model")) or _model_id(requested_model),
        )

    def as_columns(self) -> dict:
        """The ``ExtractionUsage`` column values this usage fills."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "model": self.model,
        }


def _model_id(raw) -> str | None:
    """A model id fit for the ``VARCHAR(100)`` column, or ``None``."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()[:100]


@dataclass
class ExtractionResult:
    """Normalized result from any extraction adapter."""

    success: bool
    overall_confidence: float = 0.0  # 0-1 average across all fields

    # Header fields
    invoice_number: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    vendor_name: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    vendor_address: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    vendor_tax_id: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    amount: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    currency: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    subtotal: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    tax_amount: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    tax_rate: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    discount_amount: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    shipping_amount: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    invoice_date: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    due_date: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    payment_terms: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    po_number: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    description: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    reference_number: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    payment_method: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    bill_to_address: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    remit_to_address: ExtractedField = field(default_factory=lambda: ExtractedField(None))

    # AI-suggested GL coding
    suggested_gl_account: ExtractedField = field(default_factory=lambda: ExtractedField(None))
    suggested_cost_center: ExtractedField = field(default_factory=lambda: ExtractedField(None))

    # Line items
    line_items: list[ExtractedLineItem] = field(default_factory=list)

    # Raw response for debugging
    raw_response: dict | None = None
    provider: str = ""
    error: str | None = None

    # Model-provider token usage for this call, when the provider reports it
    # (Claude, OpenAI). ``None`` for adapters that make no metered model call
    # (mock, einvoice) or report nothing usable (Ollama, Textract, Azure). Set
    # on FAILED results too whenever the provider answered: an unparseable or
    # refused read was still billed.
    usage: ExtractionTokenUsage | None = None

    def to_flat_dict(self) -> dict:
        """Convert to a flat dict of field_name → value (for backward compat)."""
        result = {}
        for fname in (
            "invoice_number",
            "vendor_name",
            "vendor_address",
            "vendor_tax_id",
            "amount",
            "currency",
            "subtotal",
            "tax_amount",
            "tax_rate",
            "discount_amount",
            "shipping_amount",
            "invoice_date",
            "due_date",
            "payment_terms",
            "po_number",
            "description",
            "reference_number",
            "payment_method",
            "bill_to_address",
            "remit_to_address",
            "suggested_gl_account",
            "suggested_cost_center",
        ):
            f: ExtractedField = getattr(self, fname)
            if f.value is not None:
                result[fname] = f.value
        return result


@dataclass
class StatementLineExtraction:
    """One open item read off a **supplier statement** (the supplier's view).

    Every value is the RAW string the reader saw — never a float, never a
    pre-parsed number. Normalising into ``Decimal`` / ``date`` is the job of
    ``app.services.vendor_statement_extraction``, which is also the only place
    that knows the reconciliation engine's dataclasses. Keeping adapters on
    strings is what makes it impossible for a provider response to introduce a
    float into the money path.
    """

    invoice_number: str | None = None
    invoice_date: str | None = None
    amount: str | None = None
    status: str | None = None
    confidence: float = 0.0
    raw: dict | None = None


@dataclass
class StatementExtractionResult:
    """Normalised result of reading a supplier statement of open items.

    A statement is a different document shape from an invoice — many open items
    for ONE supplier, no header totals worth trusting — so it gets its own
    result type rather than being forced through :class:`ExtractionResult`'s
    single-invoice header + line-item shape (an ``ExtractedLineItem`` has no
    invoice number or date to match on, which are exactly the two fields
    reconciliation needs).
    """

    available: bool = False
    success: bool = False
    lines: list[StatementLineExtraction] = field(default_factory=list)
    overall_confidence: float = 0.0
    # How many rows the reader recognised as an open item but REFUSED to book
    # because it couldn't resolve them unambiguously (a second money column, a
    # second reference column). Deliberately NOT a count of every line skipped:
    # blank lines, column headers, page furniture and totals go through the same
    # skip path, and counting those would report "47 rows skipped" on a clean
    # two-page statement — noise worse than silence. A count, never the rows'
    # text: the figure is what a reviewer acts on, and the text is supplier data.
    # Only the offline reader populates this; a model-backed adapter is not asked
    # to report its own skips, so it stays 0 there (honestly "not measured").
    skipped_ambiguous: int = 0
    provider: str = ""
    # PII-free code from the STATEMENT_REASON_* set — safe to map to a message.
    reason: str | None = None
    # Provider detail for LOGS ONLY — never put this in an HTTP response body.
    error: str | None = None
    raw_response: dict | None = None


class ExtractionAdapter:
    """Base class for AI extraction providers."""

    provider_name: str = "base"

    def __init__(self, config: dict):
        self.config = config

    async def extract(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
        file_url: str = "",
    ) -> ExtractionResult:
        """Extract invoice data from file bytes.

        Returns structured result with per-field confidence.
        """
        raise NotImplementedError

    async def extract_statement(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
    ) -> StatementExtractionResult:
        """Read a supplier **statement of open items** into statement lines.

        OPTIONAL capability — the default returns
        ``StatementExtractionResult(available=False,
        reason=STATEMENT_REASON_NOT_SUPPORTED)``, exactly like
        ``PaymentAdapter.get_balance`` / ``fetch_settlement``. Adapters that
        can't read a statement are unaffected, and the caller refuses the upload
        with an actionable message instead of inventing statement lines — which
        on this feature would be inventing money a clerk then chases.

        Best-effort by contract: implementations catch transport failures and
        return ``success=False`` rather than raise. The caller guards the call
        too, so a provider outage can't 500 the upload.
        """
        return StatementExtractionResult(
            available=False,
            provider=self.provider_name,
            reason=STATEMENT_REASON_NOT_SUPPORTED,
        )

    async def test_connection(self) -> bool:
        """Verify the provider connection / API key is working."""
        raise NotImplementedError
