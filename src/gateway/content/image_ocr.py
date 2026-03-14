"""Image OCR + PII detection via Tesseract.

Extracts text from images using Tesseract OCR, then runs the gateway's
existing PII and toxicity detection on the extracted text.
Fail-open: if Tesseract is not installed, returns graceful empty result.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image
    _TESSERACT_AVAILABLE = True
except ImportError:
    _TESSERACT_AVAILABLE = False
    pytesseract = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment,misc]

# PII patterns — same as pii_detector.py and stream_safety.py
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"\b(?:sk-|pk_live_|rk_live_|sk_live_)[a-zA-Z0-9]{20,}\b")),
]

_BLOCK_PII_TYPES = {"credit_card", "ssn", "aws_access_key", "api_key"}

# Toxicity deny terms — basic set, matches toxicity_detector.py
_TOXICITY_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:kill|murder|assassinate)\s+(?:him|her|them|people)\b", re.IGNORECASE),
]


class ImageOCRAnalyzer:
    """Extract text from images via Tesseract, then scan for PII/toxicity."""

    def __init__(self, max_size_mb: int = 10):
        self._max_size_bytes = max_size_mb * 1024 * 1024

    async def extract_text(self, image_bytes: bytes) -> str | None:
        """Extract text from image bytes. Returns None if skipped or failed."""
        if not _TESSERACT_AVAILABLE:
            logger.debug("Tesseract not available, skipping OCR")
            return None

        if len(image_bytes) > self._max_size_bytes:
            logger.warning("Image too large for OCR: %d bytes > %d max", len(image_bytes), self._max_size_bytes)
            return None

        try:
            def _do_ocr() -> str:
                img = Image.open(io.BytesIO(image_bytes))
                return pytesseract.image_to_string(img)

            return await asyncio.to_thread(_do_ocr)
        except Exception:
            logger.warning("Tesseract OCR failed", exc_info=True)
            return None

    async def analyze_image(self, image_bytes: bytes) -> dict[str, Any]:
        """Run OCR + PII/toxicity on an image. Returns analysis dict."""
        text = await self.extract_text(image_bytes)

        if text is None:
            return {
                "ocr_text_extracted": False,
                "ocr_text_length": 0,
                "ocr_pii_found": False,
                "ocr_pii_types": [],
                "ocr_pii_block": False,
                "ocr_toxicity_found": False,
            }

        # PII scan
        pii_types: list[str] = []
        for pii_type, pattern in _PII_PATTERNS:
            if pattern.search(text):
                pii_types.append(pii_type)

        pii_block = bool(set(pii_types) & _BLOCK_PII_TYPES)

        # Toxicity scan
        toxicity_found = any(p.search(text) for p in _TOXICITY_PATTERNS)

        return {
            "ocr_text_extracted": True,
            "ocr_text_length": len(text),
            "ocr_pii_found": len(pii_types) > 0,
            "ocr_pii_types": pii_types,
            "ocr_pii_block": pii_block,
            "ocr_toxicity_found": toxicity_found,
        }


async def evaluate_image_ocr(
    analyzer: ImageOCRAnalyzer,
    images: list[dict[str, Any]],
) -> tuple[bool, Any, list[dict[str, Any]]]:
    """Run OCR + PII on extracted images.

    Returns (is_blocked, error_response_or_None, ocr_results).
    """
    from starlette.responses import JSONResponse

    results: list[dict[str, Any]] = []

    for img in images:
        ocr_result = await analyzer.analyze_image(img["raw_bytes"])
        ocr_result["image_index"] = img.get("index", 0)
        ocr_result["hash_sha3_512"] = img.get("hash_sha3_512", "")
        results.append(ocr_result)

        if ocr_result.get("ocr_pii_block"):
            pii_types = ", ".join(ocr_result.get("ocr_pii_types", []))
            logger.warning("OCR PII BLOCK: types=%s hash=%.16s...", pii_types, img.get("hash_sha3_512", ""))
            error_body = {
                "error": f"Request blocked: image contains sensitive data detected via OCR ({pii_types})",
                "pii_types": ocr_result.get("ocr_pii_types", []),
            }
            return True, JSONResponse(error_body, status_code=403), results

    return False, None, results
