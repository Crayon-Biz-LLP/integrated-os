"""READ-ONLY prototype (Test tenant): Gemini-native document understanding.

The "steal the 20%" experiment, end to end:
  B1 (playbook)   — proposal-type natural-language schema, REQUIRED action fields
                    (the model cannot return an empty step)
  Layout input    — the document goes to Gemini as a real PDF part (rebuilded
                    from stored text via PyMuPDF, since original bytes are not
                    retained), not as text soup in a mega-prompt
  B3 (critic)     — second LLM pass audits stage-1 output against the source:
                    what's unsupported (fabricated)? what's missed?

No DB writes. Two LLM calls on gemini-3.6-flash. Renders what the suggestion
card would show.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from tests.fixtures.test_tenant import fresh_supabase, resolve_test_tenant_uid  # noqa: E402

MODEL = os.getenv("PROTO_MODEL", "gemini-3.6-flash")

# ── B1: proposal playbook schema — actions REQUIRE content fields ────────────
PLAYBOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string"},
        "one_line_purpose": {"type": "string"},
        "explicit_ask": {"type": "string"},
        "next_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "detail": {"type": "string"},
                    "implied_type": {"type": "string", "enum": ["task", "event", "note"]},
                    "when": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["action", "implied_type"],
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "kind": {"type": "string", "enum": ["person", "organization", "product", "other"]},
                },
                "required": ["name", "kind"],
            },
        },
    },
    "required": ["document_type", "one_line_purpose", "explicit_ask", "next_steps"],
}

# ── B3: critic schema — verify every step against the source ─────────────────
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "supported": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["action", "supported"],
            },
        },
        "missed_actions": {"type": "array", "items": {"type": "string"}},
        "fabrications": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdicts", "missed_actions"],
}


def rebuild_pdf(text: str) -> bytes:
    """Rebuild a real PDF from stored extracted text (original bytes not retained).

    Simple layout: wrapped lines, first line as title. The point is a real
    document part for Gemini — not pixel-perfect fidelity.
    """
    import fitz  # PyMuPDF — already a project dependency

    doc = fitz.open()
    page = doc.new_page()  # A4 default 595x842
    margin, y = 50.0, 60.0
    max_w = page.rect.width - 2 * margin

    def wrap(line, fontsize):
        words, lines, cur = line.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if fitz.get_text_length(trial, fontname="helv", fontsize=fontsize) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    for i, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            y += 8
            continue
        fontsize = 14 if i == 0 else 10
        for seg in wrap(line, fontsize):
            if y > page.rect.height - 50:
                page = doc.new_page()
                y = 60
            page.insert_text((margin, y), seg, fontname="helv", fontsize=fontsize)
            y += fontsize + 4
    return doc.tobytes()


PLAYBOOK_PROMPT = """You are reading a document the user received. Read the attached document carefully.

Answer as JSON:
- document_type: one word (proposal|meeting_minutes|contract|invoice|report|other)
- one_line_purpose: what this document is, in one sentence
- explicit_ask: what the document asks OF THE RECIPIENT (look for a "Next Steps" / call-to-action section). Quote it if present. "none" if truly nothing.
- next_steps: the concrete actions this document implies for the recipient. Every item MUST have a non-empty "action" (an imperative sentence, e.g. "Schedule the 30-minute alignment session with the recipient's core team") and "implied_type" (task|event|note). Include "evidence_quote": the exact phrase in the document that justifies this step. If the document implies nothing actionable, return an empty array.
- entities: the real people/organizations/products named, with their role. Do NOT treat section titles, feature names, or abstract phrases as people or organizations.

Rules: read the actual document attached. Every next_step must be grounded in the document text — no invention."""


async def stage1_playbook(pdf_bytes: bytes) -> dict:
    from google.genai import types
    from core.llm.providers import call_gemini

    contents = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        PLAYBOOK_PROMPT,
    ]
    text, _, _ = await call_gemini(
        model=MODEL,
        prompt=PLAYBOOK_PROMPT,
        contents=contents,
        config={"response_mime_type": "application/json", "response_schema": PLAYBOOK_SCHEMA},
        timeout_s=120,
    )
    return json.loads(text)


async def stage2_critic(doc_text: str, stage1: dict) -> dict:
    from core.llm.providers import call_gemini

    prompt = f"""You are a strict auditor. Below is a document and a proposed extraction of its implied next steps.

DOCUMENT:
{doc_text}

PROPOSED EXTRACTION:
{json.dumps(stage1, indent=1)}

For each proposed next_step, decide supported (true/false): is it genuinely grounded in the document? Quote-based reasoning in "reason".
Then list missed_actions: real actions implied by the document (especially any explicit Next Steps section) that the extraction missed.
List fabrications: anything in the extraction that is NOT in the document.
Return JSON only."""

    text, _, _ = await call_gemini(
        model=MODEL,
        prompt=prompt,
        config={"response_mime_type": "application/json", "response_schema": CRITIC_SCHEMA},
        timeout_s=120,
    )
    return json.loads(text)


def render_card(stage1: dict, critic: dict | None):
    print("\n=== WHAT THE SUGGESTION CARD WOULD SHOW ===")
    print(f"Summary: {stage1.get('one_line_purpose', '')}")
    ask = stage1.get("explicit_ask", "")
    if ask and ask.lower() != "none":
        print(f"Explicit ask: {ask}")
    print("Suggested actions:")
    for s in stage1.get("next_steps", []):
        kind = s.get("implied_type", "task").upper()
        verdict = ""
        if critic:
            v = next((v for v in critic.get("verdicts", []) if v.get("action") == s.get("action")), None)
            if v:
                verdict = "  ✅verified" if v.get("supported") else f"  ❌{v.get('reason', '')[:60]}"
        print(f"  ☑ [{kind}] {s.get('action')}")
        if s.get("detail"):
            print(f"      {s['detail'][:110]}")
        if verdict:
            print(f"     {verdict.strip()}")
    if critic:
        missed = critic.get("missed_actions") or []
        fab = critic.get("fabrications") or []
        if missed:
            print("Critic — missed by stage 1:")
            for m in missed:
                print(f"  + {m}")
        if fab:
            print("Critic — fabricated:")
            for f in fab:
                print(f"  - {f}")
        if not missed and not fab:
            print("Critic: nothing missed, nothing fabricated.")


def main():
    uid = resolve_test_tenant_uid()
    if not uid:
        print("Test tenant not resolvable — aborting")
        sys.exit(1)
    sb = fresh_supabase()
    u = sb.table("users").select("name").eq("id", uid).maybe_single().execute()
    if not u.data or u.data.get("name") != "Test":
        print(f"ABORT: resolved tenant is {u.data} — not the Test tenant")
        sys.exit(1)
    print(f"Test tenant confirmed: {u.data['name']} ({uid})")

    # Prefer the REAL PDF (dropped in project root by the user) — actual
    # layout for Gemini's document understanding. Fall back to the stored
    # text only if the file is missing.
    pdf_path = os.getenv("PROTO_PDF", "Kron Tech - Concept Brief.pdf")
    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            pdf = f.read()
        import fitz
        _d = fitz.open(stream=pdf, filetype="pdf")
        text = "\n".join(p.get_text() for p in _d)
        _d.close()
        print(f"Real PDF: {pdf_path} ({len(pdf)} bytes, text {len(text)} chars)")
    elif os.path.exists("Kron Tech - Concept Brief.pdf"):
        doc = sb.table("documents").select("extracted_text").eq("owner_id", uid).eq("id", 12).execute()
        text = doc.data[0]["extracted_text"]
        pdf = rebuild_pdf(text)
        print(f"Real PDF not found — rebuilt from stored text ({len(pdf)} bytes)")

    stage1 = asyncio.run(stage1_playbook(pdf))
    print("\n=== STAGE 1: PLAYBOOK (Gemini-native PDF + required-field schema) ===")
    print(f"document_type: {stage1.get('document_type')}")
    print(f"one_line_purpose: {stage1.get('one_line_purpose')}")
    print(f"next_steps: {len(stage1.get('next_steps') or [])}")
    ents = stage1.get("entities") or []
    print(f"\nENTITIES ({len(ents)}):")
    for e in ents:
        print(f"  [{e.get('kind')}] {e.get('name')} — {e.get('role', '')[:90]}")
    print("\n(full stage-1 JSON follows)")
    print(json.dumps(stage1, indent=1)[:4000])

    critic = asyncio.run(stage2_critic(text, stage1))
    print("\n=== STAGE 2: CRITIC ===")
    print(json.dumps(critic, indent=1)[:1600])

    render_card(stage1, critic)
    print("\nDone (read-only prototype — no rows written anywhere).")


if __name__ == "__main__":
    main()
