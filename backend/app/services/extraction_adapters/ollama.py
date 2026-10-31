"""Ollama local extraction adapter — runs AI models locally via Ollama.

Supports LLaVA, Llama 3.2 Vision, and other Ollama vision models.
No API key needed — runs on localhost.

Setup:
    brew install ollama
    ollama pull llama3.2-vision:11b
"""

import base64
import json

import httpx

from app.services.extraction_adapters.base import (
    STATEMENT_REASON_EMPTY_FILE,
    STATEMENT_REASON_NO_TEXT_LAYER,
    STATEMENT_REASON_PROVIDER_ERROR,
    STATEMENT_REASON_UNREADABLE,
    ExtractedLineItem,
    ExtractionAdapter,
    ExtractionResult,
    StatementExtractionResult,
    pdf_text_layer,
)
from app.services.extraction_adapters.claude_vision import EXTRACTION_PROMPT, _parse_field
from app.services.extraction_adapters.dispatcher import register_extraction_adapter
from app.services.extraction_adapters.statement_extraction import (
    STATEMENT_EXTRACTION_PROMPT,
    parse_statement_payload,
)
from app.utils.url_safety import UnsafeUrlError, org_host_client

#: Shown when a per-org ``base_url`` is not publicly routable in a deployed
#: environment. Names the field, never the URL or what it resolved to.
UNSAFE_BASE_URL_ERROR = (
    "The Ollama base_url configured for this organization is not a publicly routable "
    "http(s) URL, so it was not called."
)


def _parse_ollama_json(content: str) -> dict | None:
    """Robust JSON extractor for Ollama responses.

    Even with `format: "json"` set on the request, the smaller vision
    models (Llama 3.2 Vision 11B in particular) sometimes wrap output in
    ```json fences, prefix it with prose ("Here is the extraction: ..."),
    or trail with commentary. We try, in order:

    1. Direct parse — the happy path.
    2. Strip ```json``` fences.
    3. Find the first balanced { ... } block via brace counting.

    Returns the parsed dict, or None if no JSON object can be recovered.
    """
    if not content:
        return None
    text = content.strip()

    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fenced block
    if "```" in text:
        candidate = text.split("```", 2)[1] if text.count("```") >= 2 else text.split("```", 1)[1]
        candidate = candidate.lstrip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].lstrip()
        candidate = candidate.split("```", 1)[0].strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 3. Brace-counted scan — find the first `{` and read forward until the
    #    matching closing `}`. Tolerates models that prepend "Here is the
    #    invoice data:" or trailing commentary.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


@register_extraction_adapter("ollama")
class OllamaAdapter(ExtractionAdapter):
    """Extract invoice data using a local Ollama vision model.

    Config:
        base_url: Ollama API URL (default: http://localhost:11434)
        model: Model name (default: llama3.2-vision:11b)
    """

    provider_name = "ollama"

    @staticmethod
    def _extract_pdf_text(pdf_bytes: bytes) -> str | None:
        """Extract text from a PDF. Returns None if no text layer (scanned doc).

        Kept as a staticmethod because `openai_vision` calls it by this name;
        the implementation lives in `base.pdf_text_layer` so the statement
        reader and the invoice reader can't drift on what "has a text layer"
        means.
        """
        return pdf_text_layer(pdf_bytes)

    @staticmethod
    def _pdf_to_images(pdf_bytes: bytes, max_pages: int = 20) -> list[bytes]:
        """Convert all pages of a PDF to PNG images. Fallback for scanned PDFs.

        Returns a list of PNG byte buffers (one per page), capped at max_pages
        to avoid blowing up token budgets on unusually long documents. Pages
        are passed through :func:`image_preprocess.auto_rotate_pages` so
        90/180/270-off-upright scans are sent to the vision adapter the right
        way up. The rotation pass is gated on ``settings.extraction_auto_rotate``
        and degrades to a no-op when Tesseract is not available on the host.
        """
        try:
            import pymupdf  # PyMuPDF

            doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            images = []
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                pix = page.get_pixmap(dpi=200)
                images.append(pix.tobytes("png"))
        except Exception:
            return []

        from app.config import settings

        if settings.extraction_auto_rotate and images:
            from app.services.image_preprocess import auto_rotate_pages

            images = auto_rotate_pages(images)
        return images

    def _org_base_url(self) -> str:
        """The per-org ``base_url`` (BYOK config), or "" when none is set."""
        return str(self.config.get("base_url") or "").strip()

    def _base_url(self) -> str:
        # Per-org config wins; otherwise fall back to the global FEOH_OLLAMA_BASE_URL
        # (lets the Compose container on :11435 be selected without per-org config).
        from app.config import settings

        return self._org_base_url() or settings.ollama_base_url

    async def _client(self, timeout: float) -> httpx.AsyncClient:
        """The HTTP client for every Ollama call.

        A per-org ``base_url`` (BYOK extraction config, writable by any tenant
        admin) is SSRF-guarded: checked up front and pinned at connect
        (``url_safety.org_host_client``). Loopback is allowed outside a
        deployed environment, so the local Compose model server on :11435 and a
        native ``ollama serve`` on :11434 keep working. The adapter posts the
        invoice image and echoes the response body into its error, so an
        internal target would be a read-back channel. The operator's
        ``FEOH_OLLAMA_BASE_URL`` is trusted and keeps a plain client
        (decisions §334). Raises :class:`UnsafeUrlError`.
        """
        from app.config import settings

        org_url = self._org_base_url()
        if not org_url:
            return httpx.AsyncClient(timeout=timeout)
        return await org_host_client(
            org_url, allow_private=not settings.is_deployed, timeout=timeout
        )

    def _model(self) -> str:
        return self.config.get("model", "llama3.2-vision:11b")

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

        is_pdf = mime_type == "application/pdf" or file_key.lower().endswith(".pdf")
        use_vision = True
        pdf_text = None

        page_images: list[bytes] = []
        if is_pdf:
            # Try to extract text first — much faster and more accurate
            pdf_text = self._extract_pdf_text(file_bytes)
            if pdf_text:
                use_vision = False
            else:
                # Scanned PDF — convert ALL pages to images for vision model
                page_images = self._pdf_to_images(file_bytes)
                if not page_images:
                    return ExtractionResult(
                        success=False,
                        error="Cannot read PDF. Install PyMuPDF: pip install PyMuPDF",
                        provider=self.provider_name,
                    )

        # Sampling settings. We deliberately do NOT pin a fixed seed: with
        # weak local vision models (Llama 3.2 Vision 11B), a fixed seed +
        # temperature=0 locks every retry onto the exact same failure mode
        # — if that one path misses a field, you can never recover. A
        # fresh seed each call gives the model a chance to land on a
        # different path on retry, which is the user-visible behaviour
        # we actually want for "click extract again, hope it works."
        #
        # `num_predict` is just a buffer for the long JSON schema response
        # and doesn't affect quality. Keep it.
        ollama_options = {
            "num_predict": 4096,
        }

        if use_vision:
            # Vision mode — send all page images to model (multi-page support)
            if page_images:
                images_b64 = [base64.b64encode(img).decode("utf-8") for img in page_images]
            else:
                # Non-PDF image file — single image
                images_b64 = [base64.b64encode(file_bytes).decode("utf-8")]
            body = {
                "model": self._model(),
                "messages": [
                    {
                        "role": "user",
                        "content": EXTRACTION_PROMPT,
                        "images": images_b64,
                    }
                ],
                "stream": False,
                "format": "json",
                "options": ollama_options,
            }
        else:
            # Text mode — send extracted text, works with any model (no vision needed)
            body = {
                "model": self._model(),
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"{EXTRACTION_PROMPT}\n\nHere is the invoice text:\n\n{pdf_text}"
                        ),
                    }
                ],
                "stream": False,
                "format": "json",
                "options": ollama_options,
            }

        # Ollama serialises inference per-model by default (one in-flight
        # request at a time). When users upload N invoices in quick
        # succession, requests 2..N queue inside Ollama. With a tight
        # client timeout the queued requests give up before they even
        # start running. 600s lets the queue drain even with ~10 invoices
        # batched on an 11B vision model (~30–60s each).
        try:
            async with await self._client(600) as client:
                resp = await client.post(
                    f"{self._base_url()}/api/chat",
                    json=body,
                )
        except UnsafeUrlError:
            return ExtractionResult(
                success=False,
                error=UNSAFE_BASE_URL_ERROR,
                provider=self.provider_name,
            )
        except httpx.ConnectError:
            return ExtractionResult(
                success=False,
                error="Cannot connect to Ollama. Is it running? Start with: ollama serve",
                provider=self.provider_name,
            )
        except httpx.ReadTimeout:
            return ExtractionResult(
                success=False,
                error=(
                    "Ollama timed out (>10 min). The model is probably overloaded — "
                    "either too many invoices queued at once, or the model is too large "
                    "for the GPU. Try uploading fewer at a time, or set "
                    "OLLAMA_NUM_PARALLEL=2 in Ollama's environment to allow parallel inference."
                ),
                provider=self.provider_name,
            )
        except Exception as exc:
            return ExtractionResult(
                success=False,
                error=f"Ollama API error: {exc}",
                provider=self.provider_name,
            )

        if resp.status_code != 200:
            return ExtractionResult(
                success=False,
                error=f"Ollama error {resp.status_code}: {resp.text}",
                provider=self.provider_name,
            )

        resp_data = resp.json()
        text_content = resp_data.get("message", {}).get("content", "")

        data = _parse_ollama_json(text_content)
        if data is None:
            return ExtractionResult(
                success=False,
                error="Failed to parse JSON from Ollama response",
                raw_response={"text": text_content},
                provider=self.provider_name,
            )

        # Build result (same parsing as Claude adapter)
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
        )

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

        fields = [result.invoice_number, result.vendor_name, result.amount]
        confidences = [f.confidence for f in fields if f.value is not None]
        result.overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        return result

    async def extract_statement(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
    ) -> StatementExtractionResult:
        """Read a supplier statement of open items with the LOCAL model.

        This is the no-cloud answer for a *scanned* statement, where the
        deterministic text-layer reader has nothing to work with: `pnpm
        ollama:up`, point the org's extraction config at `ollama`, and the whole
        PDF-intake path runs on the laptop with no credential anywhere.

        Same two modes as :meth:`extract` — text when the PDF has a text layer
        (cheaper and far more accurate on a table), page images when it doesn't.
        Never raises.
        """
        if not file_bytes:
            return StatementExtractionResult(
                available=True, provider=self.provider_name, reason=STATEMENT_REASON_EMPTY_FILE
            )

        is_pdf = mime_type == "application/pdf" or file_key.lower().endswith(".pdf")
        message: dict = {"role": "user", "content": STATEMENT_EXTRACTION_PROMPT}
        if is_pdf:
            pdf_text = self._extract_pdf_text(file_bytes)
            if pdf_text:
                message["content"] = (
                    f"{STATEMENT_EXTRACTION_PROMPT}\n\nHere is the statement text:\n\n{pdf_text}"
                )
            else:
                page_images = self._pdf_to_images(file_bytes)
                if not page_images:
                    return StatementExtractionResult(
                        available=True,
                        provider=self.provider_name,
                        reason=STATEMENT_REASON_NO_TEXT_LAYER,
                        error="Cannot read PDF. Install PyMuPDF: pip install PyMuPDF",
                    )
                message["images"] = [base64.b64encode(img).decode("utf-8") for img in page_images]
        else:
            message["images"] = [base64.b64encode(file_bytes).decode("utf-8")]

        try:
            async with await self._client(600) as client:
                resp = await client.post(
                    f"{self._base_url()}/api/chat",
                    json={
                        "model": self._model(),
                        "messages": [message],
                        "stream": False,
                        "format": "json",
                        "options": {"num_predict": 8192},
                    },
                )
        except UnsafeUrlError:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_PROVIDER_ERROR,
                error=UNSAFE_BASE_URL_ERROR,
            )
        except Exception as exc:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_PROVIDER_ERROR,
                error=f"Ollama API error: {exc}",
            )

        if resp.status_code != 200:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_PROVIDER_ERROR,
                error=f"Ollama error {resp.status_code}",
            )

        data = _parse_ollama_json(resp.json().get("message", {}).get("content", ""))
        if data is None:
            return StatementExtractionResult(
                available=True,
                provider=self.provider_name,
                reason=STATEMENT_REASON_UNREADABLE,
                error="Failed to parse JSON from Ollama response",
            )

        return parse_statement_payload(data, self.provider_name)

    async def test_connection(self) -> bool:
        try:
            async with await self._client(5) as client:
                resp = await client.get(f"{self._base_url()}/api/tags")
            if resp.status_code != 200:
                return False
            # Check if the configured model is available
            models = [m["name"] for m in resp.json().get("models", [])]
            target = self._model()
            return any(target in m for m in models)
        except Exception:
            return False
