"""Tests for multi-page PDF extraction support.

Verifies that:
- _pdf_to_images() returns all pages (not just page 0)
- Vision-mode requests include all page images
- max_pages cap is respected
"""

import base64

import pytest


def _make_pdf(num_pages: int) -> bytes:
    """Create a minimal multi-page PDF using PyMuPDF."""
    import pymupdf

    doc = pymupdf.open()
    for i in range(num_pages):
        page = doc.new_page(width=200, height=200)
        page.insert_text((50, 100), f"Page {i + 1}", fontsize=20)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class TestPdfToImages:
    """Tests for OllamaAdapter._pdf_to_images()."""

    def test_returns_all_pages(self):
        from app.services.extraction_adapters.ollama import OllamaAdapter

        pdf = _make_pdf(3)
        images = OllamaAdapter._pdf_to_images(pdf)
        assert len(images) == 3
        for img in images:
            # Each should be valid PNG (starts with PNG magic bytes)
            assert img[:4] == b"\x89PNG"

    def test_single_page(self):
        from app.services.extraction_adapters.ollama import OllamaAdapter

        pdf = _make_pdf(1)
        images = OllamaAdapter._pdf_to_images(pdf)
        assert len(images) == 1

    def test_respects_max_pages(self):
        from app.services.extraction_adapters.ollama import OllamaAdapter

        pdf = _make_pdf(25)
        images = OllamaAdapter._pdf_to_images(pdf, max_pages=5)
        assert len(images) == 5

    def test_default_max_pages_is_20(self):
        from app.services.extraction_adapters.ollama import OllamaAdapter

        pdf = _make_pdf(22)
        images = OllamaAdapter._pdf_to_images(pdf)
        assert len(images) == 20

    def test_empty_pdf_returns_empty_list(self):
        from app.services.extraction_adapters.ollama import OllamaAdapter

        images = OllamaAdapter._pdf_to_images(b"not a pdf")
        assert images == []

    def test_corrupt_bytes_returns_empty_list(self):
        from app.services.extraction_adapters.ollama import OllamaAdapter

        images = OllamaAdapter._pdf_to_images(b"")
        assert images == []


class TestOllamaVisionMultiPage:
    """Tests that Ollama adapter builds multi-image requests for scanned PDFs."""

    @pytest.mark.asyncio
    async def test_vision_request_contains_all_page_images(self, monkeypatch):
        """Verify that a 3-page scanned PDF sends 3 images in the Ollama request."""
        from unittest.mock import AsyncMock, MagicMock

        from app.services.extraction_adapters.ollama import OllamaAdapter

        pdf = _make_pdf(3)
        adapter = OllamaAdapter({"model": "test-model", "base_url": "http://localhost:11434"})

        # Force vision mode by making text extraction return None
        monkeypatch.setattr(adapter, "_extract_pdf_text", staticmethod(lambda _: None))

        # Capture the request body
        captured_body = {}

        async def mock_post(url, json=None, **kwargs):
            captured_body.update(json or {})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "message": {
                    "content": '{"invoice_number": {"value": "INV-001", "confidence": 0.9}}'
                }
            }
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = mock_post

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)

        await adapter.extract(file_bytes=pdf, mime_type="application/pdf")

        # Should have 3 base64 images in the request
        messages = captured_body.get("messages", [])
        assert len(messages) == 1
        images = messages[0].get("images", [])
        assert len(images) == 3

        # Each should be valid base64
        for img_b64 in images:
            decoded = base64.b64decode(img_b64)
            assert decoded[:4] == b"\x89PNG"

    @pytest.mark.asyncio
    async def test_non_pdf_image_sends_single_image(self, monkeypatch):
        """Non-PDF files (PNG, JPEG) should still send a single image."""
        from unittest.mock import AsyncMock, MagicMock

        from app.services.extraction_adapters.ollama import OllamaAdapter

        adapter = OllamaAdapter({"model": "test-model", "base_url": "http://localhost:11434"})

        captured_body = {}

        async def mock_post(url, json=None, **kwargs):
            captured_body.update(json or {})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "message": {
                    "content": '{"invoice_number": {"value": "INV-001", "confidence": 0.9}}'
                }
            }
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = mock_post

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)

        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        await adapter.extract(file_bytes=fake_png, mime_type="image/png")

        messages = captured_body.get("messages", [])
        images = messages[0].get("images", [])
        assert len(images) == 1


class TestOpenAIVisionMultiPage:
    """Tests that OpenAI adapter builds multi-image content blocks for scanned PDFs."""

    @pytest.mark.asyncio
    async def test_vision_request_contains_all_page_images(self, monkeypatch):
        """Verify that a 3-page scanned PDF sends 3 image_url blocks."""
        from unittest.mock import AsyncMock, MagicMock

        from app.services.extraction_adapters.openai_vision import OpenAIVisionAdapter

        pdf = _make_pdf(3)
        adapter = OpenAIVisionAdapter({"api_key": "test-key", "model": "gpt-4o"})

        # Force vision mode
        from app.services.extraction_adapters.ollama import OllamaAdapter

        monkeypatch.setattr(OllamaAdapter, "_extract_pdf_text", staticmethod(lambda _: None))

        captured_body = {}

        async def mock_post(url, json=None, **kwargs):
            captured_body.update(json or {})
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [
                    {
                        "message": {
                            "content": '{"invoice_number": {"value": "INV-001", "confidence": 0.9}}'
                        }
                    }
                ]
            }
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = mock_post

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: mock_client)

        await adapter.extract(file_bytes=pdf, mime_type="application/pdf")

        messages = captured_body.get("messages", [])
        content = messages[0].get("content", [])
        # Should have 3 image_url blocks + 1 text block
        image_blocks = [c for c in content if c.get("type") == "image_url"]
        text_blocks = [c for c in content if c.get("type") == "text"]
        assert len(image_blocks) == 3
        assert len(text_blocks) == 1
