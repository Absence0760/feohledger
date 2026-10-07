"""OpenAI GPT-4V extraction adapter — BYOK option for customers with OpenAI keys."""

import base64
import json

import httpx

from app.services.extraction_adapters.base import (
    ExtractedLineItem,
    ExtractionAdapter,
    ExtractionResult,
    ExtractionTokenUsage,
)
from app.services.extraction_adapters.claude_vision import EXTRACTION_PROMPT, _parse_field
from app.services.extraction_adapters.dispatcher import register_extraction_adapter


@register_extraction_adapter("openai_vision")
class OpenAIVisionAdapter(ExtractionAdapter):
    """Extract invoice data using OpenAI GPT-4V.

    Required config:
        api_key: OpenAI API key
        model: Model to use (default: gpt-4o)
    """

    provider_name = "openai_vision"

    async def extract(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
        file_url: str = "",
    ) -> ExtractionResult:
        api_key = self.config.get("api_key", "")
        model = self.config.get("model", "gpt-4o")

        if not file_bytes:
            return ExtractionResult(
                success=False, error="No file bytes provided", provider=self.provider_name
            )

        is_pdf = mime_type == "application/pdf" or file_key.lower().endswith(".pdf")
        pdf_text = None

        page_images: list[bytes] = []
        if is_pdf:
            from app.services.extraction_adapters.ollama import OllamaAdapter

            pdf_text = OllamaAdapter._extract_pdf_text(file_bytes)
            if not pdf_text:
                # Scanned PDF — convert ALL pages to images
                page_images = OllamaAdapter._pdf_to_images(file_bytes)
                if page_images:
                    mime_type = "image/png"

        # If we have text, use text-only mode (cheaper, faster, more accurate)
        if pdf_text:
            body = {
                "model": model,
                "max_tokens": 4096,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"{EXTRACTION_PROMPT}\n\nHere is the invoice text:\n\n{pdf_text}"
                        ),
                    }
                ],
            }

            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        json=body,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                    )
            except Exception as exc:
                return ExtractionResult(
                    success=False, error=f"API call failed: {exc}", provider=self.provider_name
                )

            if resp.status_code != 200:
                return ExtractionResult(
                    success=False,
                    error=f"OpenAI error {resp.status_code}: {resp.text}",
                    provider=self.provider_name,
                )

            resp_data = resp.json()
            usage = ExtractionTokenUsage.from_openai(resp_data, model)
            text_content = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")

            json_str = text_content.strip()
            if json_str.startswith("```"):
                json_str = json_str.split("```")[1]
                if json_str.startswith("json"):
                    json_str = json_str[4:]
            json_str = json_str.strip()

            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                return ExtractionResult(
                    success=False,
                    error="Failed to parse JSON from OpenAI response",
                    provider=self.provider_name,
                    usage=usage,
                )

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
                invoice_date=_parse_field(data, "invoice_date"),
                due_date=_parse_field(data, "due_date"),
                payment_terms=_parse_field(data, "payment_terms"),
                po_number=_parse_field(data, "po_number"),
                description=_parse_field(data, "description"),
                suggested_gl_account=_parse_field(data, "suggested_gl_account"),
                suggested_cost_center=_parse_field(data, "suggested_cost_center"),
                raw_response=data,
                provider=self.provider_name,
                usage=usage,
            )

            for li in data.get("line_items", []):
                result.line_items.append(
                    ExtractedLineItem(
                        line_number=li.get("line_number", 0),
                        description=_parse_field(li, "description"),
                        quantity=_parse_field(li, "quantity"),
                        unit_price=_parse_field(li, "unit_price"),
                        total=_parse_field(li, "total"),
                    )
                )

            fields = [result.invoice_number, result.vendor_name, result.amount]
            confidences = [f.confidence for f in fields if f.value is not None]
            result.overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0
            return result

        # Build image content blocks — multi-page support
        if page_images:
            image_content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64.b64encode(img).decode('utf-8')}"
                    },
                }
                for img in page_images
            ]
        else:
            file_b64 = base64.b64encode(file_bytes).decode("utf-8")
            if mime_type in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"):
                image_url = f"data:{mime_type};base64,{file_b64}"
            else:
                image_url = f"data:image/png;base64,{file_b64}"
            image_content = [{"type": "image_url", "image_url": {"url": image_url}}]

        body = {
            "model": model,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        *image_content,
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception as exc:
            return ExtractionResult(
                success=False, error=f"API call failed: {exc}", provider=self.provider_name
            )

        if resp.status_code != 200:
            return ExtractionResult(
                success=False,
                error=f"OpenAI error {resp.status_code}: {resp.text}",
                provider=self.provider_name,
            )

        resp_data = resp.json()
        usage = ExtractionTokenUsage.from_openai(resp_data, model)
        text_content = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")

        json_str = text_content.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
        json_str = json_str.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return ExtractionResult(
                success=False,
                error="Failed to parse JSON from OpenAI response",
                provider=self.provider_name,
                usage=usage,
            )

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
            invoice_date=_parse_field(data, "invoice_date"),
            due_date=_parse_field(data, "due_date"),
            payment_terms=_parse_field(data, "payment_terms"),
            po_number=_parse_field(data, "po_number"),
            description=_parse_field(data, "description"),
            suggested_gl_account=_parse_field(data, "suggested_gl_account"),
            suggested_cost_center=_parse_field(data, "suggested_cost_center"),
            raw_response=data,
            provider=self.provider_name,
            usage=usage,
        )

        for li in data.get("line_items", []):
            result.line_items.append(
                ExtractedLineItem(
                    line_number=li.get("line_number", 0),
                    description=_parse_field(li, "description"),
                    quantity=_parse_field(li, "quantity"),
                    unit_price=_parse_field(li, "unit_price"),
                    total=_parse_field(li, "total"),
                )
            )

        fields = [result.invoice_number, result.vendor_name, result.amount]
        confidences = [f.confidence for f in fields if f.value is not None]
        result.overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        return result

    async def test_connection(self) -> bool:
        try:
            api_key = self.config.get("api_key", "")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            return resp.status_code == 200
        except Exception:
            return False
