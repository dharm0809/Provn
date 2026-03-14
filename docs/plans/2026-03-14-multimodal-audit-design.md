# Multimodal Audit — Design Document

**Date:** 2026-03-14
**Status:** Approved
**Goal:** Track, classify, and audit every piece of data (text, images, documents) that enters or leaves the model — storing metadata and cryptographic proof only, never file bytes.

---

## Architecture Overview

```
                        ┌─────────────────────┐
                        │     OpenWebUI        │
                        │                      │
                        │  Pipeline Plugin ─────────── POST /v1/attachments/notify
                        │  (file upload hook)  │       (metadata + hash at upload time)
                        │                      │
                        └──────────┬───────────┘
                                   │ /v1/chat/completions
                                   │ (messages with images, RAG context)
                                   ▼
                        ┌─────────────────────┐
                        │      Gateway         │
                        │                      │
                        │  1. Attachment        │ ◄── Extract file metadata from request
                        │     Tracker          │      + correlate with webhook notifications
                        │                      │
                        │  2. Image Safety     │ ◄── LlamaGuard Vision (11B)
                        │     Analyzer         │      BLOCK + alert on S4, BLOCK on others
                        │                      │
                        │  3. Image OCR        │ ◄── Tesseract → PII/toxicity analysis
                        │     + PII Scanner    │      on extracted text
                        │                      │
                        │  4. Execution Record │ ◄── file_metadata[], image_verdicts[],
                        │     (enriched)       │      ocr_pii_results[], attachment_hashes[]
                        │                      │
                        └──────────┬───────────┘
                                   │
                                   ▼
                              ┌─────────┐
                              │  Ollama  │
                              └─────────┘
```

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| File storage | Metadata + SHA3-512 hash only, never file bytes | Avoids storage costs and file security concerns; hash provides tamper-proof evidence |
| Data sources | Both gateway parsing + OpenWebUI webhook | More data = better audit trail |
| Image safety | Classify inline via LlamaGuard Vision, discard image after | Safety verdict without storing the image |
| OCR engine | Tesseract (pytesseract) | Fast, local, no GPU contention, good enough for PII detection in photos |
| BLOCK behavior | HTTP 403 with human-readable reason + category; S4 fires CRITICAL alert | User knows why they were blocked; security team notified on severe violations |
| All new components | Opt-in via config flags, fail-open | Gateway never stops working because an analyzer is down |

## What We Store (per execution record)

```json
{
  "execution_id": "...",
  "file_metadata": [
    {
      "filename": "contract.pdf",
      "mimetype": "application/pdf",
      "size_bytes": 245000,
      "hash_sha3_512": "abc123...",
      "source": "openwebui_upload",
      "uploaded_by": "user@example.com",
      "chunks_injected": 3
    }
  ],
  "image_analysis": [
    {
      "image_index": 0,
      "hash_sha3_512": "def456...",
      "safety_verdict": "pass",
      "safety_category": null,
      "ocr_text_extracted": true,
      "ocr_pii_found": false,
      "ocr_pii_types": [],
      "ocr_toxicity_found": false
    }
  ]
}
```

---

## Component C2: Document/File Tracking

### Two data sources, correlated by hash

**Source 1 — OpenWebUI Pipeline Plugin** (`plugins/openwebui/attachment_notifier.py`)
- Fires on file upload event inside OpenWebUI
- POSTs to `POST /v1/attachments/notify` with:
  - `filename`, `mimetype`, `size_bytes`
  - `hash_sha3_512` (computed from file bytes before discarding)
  - `chat_id` (maps to session_id)
  - `user_id`, `user_email`
  - `upload_timestamp`
- Gateway stores in bounded in-memory cache: `{hash → metadata}` with 1-hour TTL
- New WAL table: `gateway_file_notifications` for persistence

**Source 2 — Gateway request parsing** (`src/gateway/middleware/attachment_tracker.py`)
- Runs before orchestrator, after body is read
- Detects:
  - `image_url` blocks in message content → compute SHA3-512 of base64 bytes
  - OpenWebUI's `metadata.files` field in request body (if present) → extract filenames, types
  - RAG context markers in message text → count injected chunks
- Correlates image/file hashes against the notification cache from Source 1
- Attaches `file_metadata[]` to `request.state` for the orchestrator to pick up

### New endpoints

- `POST /v1/attachments/notify` — receives webhook from OpenWebUI plugin. Requires API key auth. Skips completeness middleware.
- `GET /v1/lineage/attachments?session_id=X` — returns all file metadata for a session. Read-only, no auth (same as lineage).

---

## Component C1: Image Safety Classification

### New analyzer: `src/gateway/content/image_safety.py` — `ImageSafetyAnalyzer`

**Flow:**
1. Attachment tracker (C2) detects `image_url` blocks and extracts base64 bytes
2. Before forwarding to model, images are passed to `ImageSafetyAnalyzer`
3. Sends each image to LlamaGuard Vision (11B) via Ollama `/api/chat`
4. Gets S1-S14 verdict back
5. On BLOCK verdict:
   - S4 (child safety): HTTP 403 with `{"error": "Request blocked: image content violates safety policy (child_safety)", "category": "S4"}` + CRITICAL log alert
   - Other BLOCK categories: HTTP 403 with `{"error": "Request blocked: image content violates safety policy (<category>)", "category": "<SN>"}`
   - Execution record written with `disposition: "denied_content"`, image hash, and verdict
6. On PASS/WARN: verdicts stored in `image_analysis[]`, request proceeds

**Config:**
- `WALACOR_IMAGE_SAFETY_ENABLED=false` (opt-in)
- `WALACOR_IMAGE_SAFETY_MODEL=llama-guard3-vision:11b`
- `WALACOR_IMAGE_SAFETY_TIMEOUT_MS=10000`
- `WALACOR_IMAGE_SAFETY_MAX_IMAGES=5` (skip if exceeded, log warning)

**Fail-open:** if unavailable/timeout → PASS with confidence=0.0.

**Runs pre-inference** — blocks before the model sees bad content.

---

## Component C4: Image OCR + PII Detection

### New module: `src/gateway/content/image_ocr.py` — `ImageOCRAnalyzer`

**Flow:**
1. Same images extracted by attachment tracker (C2)
2. After image safety (C1) passes, OCR runs on each image
3. `pytesseract.image_to_string()` extracts text (via Pillow to load image)
4. Extracted text fed through existing `PIIDetector` and `ToxicityDetector`
5. Results stored in `image_analysis[]`:
   - `ocr_text_extracted: true/false`
   - `ocr_text_length: int`
   - `ocr_pii_found: true/false`
   - `ocr_pii_types: ["credit_card", "ssn"]`
   - `ocr_toxicity_found: true/false`
6. High-risk PII (credit card, SSN) → BLOCK with reason; low-risk → WARN

**Processing:**
- `asyncio.to_thread()` wraps Tesseract (CPU-bound, ~100-500ms)
- Max image size: skip OCR if > 10MB
- Fail-open: if Tesseract unavailable → `ocr_text_extracted: false`, no block

**Config:**
- `WALACOR_IMAGE_OCR_ENABLED=false` (opt-in)
- `WALACOR_IMAGE_OCR_MAX_SIZE_MB=10`

**Dependencies** (new optional extra in pyproject.toml):
```
ocr = ["pytesseract>=0.3", "Pillow>=9.0"]
```
Tesseract binary: `apt install tesseract-ocr` on EC2.

---

## Processing Order — Full Request Lifecycle

```
1. User uploads PDF in OpenWebUI
   └─► Pipeline plugin fires POST /v1/attachments/notify
       (filename, hash, user, chat_id) → stored in gateway cache

2. User sends message (with image attachment) via OpenWebUI
   └─► OpenWebUI injects RAG chunks + sends to gateway

3. Gateway receives POST /v1/chat/completions
   │
   ├─ Completeness middleware (attempt record)
   ├─ API key / JWT auth
   │
   ├─ Attachment Tracker (C2)
   │   ├─ Extract image_url blocks → base64 decode
   │   ├─ Compute SHA3-512 hash per image
   │   ├─ Parse OpenWebUI metadata.files field
   │   ├─ Detect RAG context chunks
   │   └─ Correlate hashes with notification cache
   │
   ├─ Image Safety (C1)
   │   ├─ Send images to LlamaGuard Vision
   │   ├─ BLOCK → 403 + alert + execution record
   │   └─ PASS/WARN → continue
   │
   ├─ Image OCR + PII (C4)
   │   ├─ Tesseract extracts text from images
   │   ├─ Run PII + toxicity on extracted text
   │   ├─ BLOCK (high-risk PII) → 403
   │   └─ PASS/WARN → continue
   │
   ├─ Existing pipeline (unchanged)
   │   ├─ Adapter parse_request
   │   ├─ Pre-inference policy
   │   ├─ Forward to Ollama
   │   ├─ Post-inference content analysis (text)
   │   ├─ Tool loop (if applicable)
   │   └─ Build execution record
   │
   └─ Execution record now includes:
       ├─ file_metadata[] (from C2)
       ├─ image_analysis[] (from C1 + C4)
       └─ All existing fields (unchanged)
```

## Lineage Dashboard Updates

- Execution detail view: new "Attachments" section with file metadata cards
- Image analysis verdicts alongside text content analysis
- File hash displayed for verification
- Session view: file icon badge on executions with attachments (similar to tool badge)

## Configuration Summary

| Variable | Default | Purpose |
|----------|---------|---------|
| `WALACOR_IMAGE_SAFETY_ENABLED` | `false` | Enable LlamaGuard Vision image classification |
| `WALACOR_IMAGE_SAFETY_MODEL` | `llama-guard3-vision:11b` | Ollama model for image safety |
| `WALACOR_IMAGE_SAFETY_TIMEOUT_MS` | `10000` | Image classification timeout |
| `WALACOR_IMAGE_SAFETY_MAX_IMAGES` | `5` | Max images to analyze per request |
| `WALACOR_IMAGE_OCR_ENABLED` | `false` | Enable Tesseract OCR + PII on images |
| `WALACOR_IMAGE_OCR_MAX_SIZE_MB` | `10` | Skip OCR for images larger than this |
| `WALACOR_ATTACHMENT_TRACKING_ENABLED` | `true` | Enable file/document metadata tracking |

## New Files

| File | Purpose |
|------|---------|
| `src/gateway/middleware/attachment_tracker.py` | Request body parsing, image extraction, hash computation, notification cache |
| `src/gateway/content/image_safety.py` | LlamaGuard Vision analyzer for images |
| `src/gateway/content/image_ocr.py` | Tesseract OCR + PII/toxicity on extracted text |
| `plugins/openwebui/attachment_notifier.py` | OpenWebUI pipeline plugin for file upload webhooks |
| `tests/unit/test_attachment_tracker.py` | Tests for C2 |
| `tests/unit/test_image_safety.py` | Tests for C1 |
| `tests/unit/test_image_ocr.py` | Tests for C4 |

## Priority Order

C2 (document tracking) → C1 (image safety) → C4 (OCR + PII)

C2 first because it provides the data extraction layer that C1 and C4 depend on.
