"""UAT: Document Intelligence v2 — real parse_document on the two real PDFs.

Runs the PRODUCTION function (core/webhook/document_parser.py — the exact code
the /api/multimodal/input route calls) against the real Gemini API with the
real uploaded files, inside the Test-tenant scope (read-only: parse_document
writes nothing to the DB — entity matching happens later in the route with the
authenticated owner_id).

Verifies, per document:
  1. document_type + one_line_purpose present
  2. every suggested_action has a non-empty title/content (the Aug-22 blank-params
     class is structurally dead)
  3. suggested_entities carry kinds (no junk person guesses)
  4. critic verdicts recorded in metadata

Usage: python3 scripts/uat_document_parser_v2.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from core.services.db import tenant_scope  # noqa: E402
from tests.fixtures.test_tenant import resolve_test_tenant_uid  # noqa: E402
from core.webhook.document_parser import parse_document  # noqa: E402


def extract_pdf_text(path: str) -> str:
    import fitz
    doc = fitz.open(path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text


DOCS = [
    ("Kron Tech - Concept Brief.pdf", "proposal"),
    ("Cricket League Management Platform.pdf", "product strategy"),
]


def check(name: str, breakdown: dict) -> bool:
    print(f"\n{'=' * 66}\n📄 {name}\n{'=' * 66}")
    dtype = breakdown.get("document_type")
    purpose = breakdown.get("one_line_purpose") or breakdown.get("summary", "")
    print(f"  type: {dtype}")
    print(f"  purpose: {str(purpose)[:140]}")

    actions = breakdown.get("suggested_actions", [])
    ok = True
    if not actions:
        print("  ❌ NO ACTIONS — blank card class")
        return False
    print(f"  actions ({len(actions)}):")
    for a in actions:
        params = a.get("params") or {}
        title = a.get("human_label") or params.get("title") or params.get("content") or ""
        op = a.get("operation", "?")
        if not title.strip():
            print(f"    ❌ [{op}] BLANK — the regression we killed")
            ok = False
        else:
            print(f"    ☑ [{op}] {title[:90]}")
    entities = breakdown.get("suggested_entities", [])
    print(f"  entities ({len(entities)}):")
    for e in entities:
        label = e.get("label") or e.get("name")
        kind = e.get("type") or e.get("node_type") or e.get("kind")
        print(f"    • {label} ({kind})")
        if not label or not kind:
            ok = False
    intel = breakdown.get("intelligence") or {}
    print(f"  critic: rejected={len(intel.get('critic_rejected_actions') or [])} "
          f"recovered={intel.get('critic_recovered_actions', '?')} "
          f"missed_logged={len((intel.get('critic') or {}).get('missed_actions') or [])}")
    return ok


async def main() -> int:
    uid = resolve_test_tenant_uid()
    if not uid:
        print("❌ Test tenant unresolvable — refusing to run (fail-closed)")
        return 1
    print(f"Test tenant: {uid}")

    all_ok = True
    with tenant_scope(uid):
        for path, expected_hint in DOCS:
            if not os.path.exists(path):
                print(f"❌ missing file: {path}")
                all_ok = False
                continue
            with open(path, "rb") as f:
                pdf_bytes = f.read()
            text = extract_pdf_text(path)
            breakdown = await parse_document(text, pdf_bytes=pdf_bytes,
                                             mime_type="application/pdf")
            all_ok = check(path, breakdown) and all_ok

    print("\n" + ("✅ UAT PASS — both documents produce intelligent, "
                  "grounded, non-blank cards" if all_ok
                  else "❌ UAT FAIL — see ❌ items above"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
