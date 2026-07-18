# Part 60: Hybrid Document Extraction

## Problem
When sending PDF/DOCX meeting notes (~2,600 chars) via Telegram, Gemini vision extracted only **479 chars** of summarized content — the model's training bias toward description overrode the "transcribe verbatim" instruction.

## Root Cause
Two regressions from the LLM consolidation refactoring:
1. `multimodal.py` was wired to `CLASSIFICATION_MODEL` (gemini-3.1-flash-lite) — too weak for document vision tasks
2. `generate_content_with_fallback` wrapper duplicated the extraction prompt in both `prompt=` and `contents=` parameters, confusing the model

## Solution: Hybrid Document Extraction

Created `core/lib/document_extractor.py` — a single entry point for all document formats:

```
PDF    → PyMuPDF (algorithmic, 50ms, free, 100% verbatim)
DOCX   → python-docx (paragraphs + tables)
XLSX   → openpyxl (all sheets, flattened cells)
PPTX   → python-pptx (all slides, shapes, tables)
Text   → direct bytes.decode
Images → Gemini vision fallback (scanned docs, photos)
Audio  → Gemini audio transcription (unchanged)
```

### Architecture

```
multimodal.py receives file bytes + mime_type
         │
         ▼
   ┌──────────────────────────┐
   │  extract_text()          │
   │  (core/lib/extractor)    │
   ├──────────────────────────┤
   │  PDF?        → PyMuPDF  │── 50ms, free, verbatim
   │  DOCX?       → docx      │
   │  XLSX?       → openpyxl  │
   │  PPTX?       → pptx      │
   │  TXT?        → decode()  │
   │  Image/Audio → None      │
   └──────┬───────────────────┘
          │ (returns None for images)
          ▼
   ┌──────────────────────────┐
   │  Gemini vision fallback  │── Only for images & scanned docs
   │  (prompt="" fix applied) │
   └──────┬───────────────────┘
          │
          ▼
   Standard pipeline: classify → plan → execute
```

### Key Improvements

| Metric | Before (Gemini vision) | After (Hybrid) |
|---|---|---|
| Speed | ~2s (API round trip) | ~50ms (local) |
| Cost | Per-document API fee | Free for 90% |
| Verbatim text? | ❌ Summarized to 479/2600 chars | ✅ 100% verbatim |
| Format support | PDF, DOCX | PDF, DOCX, XLSX, PPTX, TXT |
| Scanned docs | ✅ Works | ✅ Gemini fallback |
| Regression risk | High (API behavior changes) | Near-zero (algorithmic) |

### Files Changed
- `core/lib/document_extractor.py` — NEW (85 lines): hybrid extractor
- `core/webhook/multimodal.py` — MODIFIED: uses local extraction, fixes prompt duplication, uses SYNTHESIS_MODEL
- `core/webhook/handler.py` — MODIFIED: adds XLSX and PPTX MIME types
- `requirements.txt` — MODIFIED: added pymupdf, python-pptx; removed pypdf
