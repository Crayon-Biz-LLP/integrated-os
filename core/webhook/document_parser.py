"""Document Intelligence — Gemini-native document understanding.

Rewritten Sep 2026 after the Kron blank-card investigation. Root cause chain:
the old path extracted text with PyMuPDF, stuffed it into the chat mega-prompt
with a NOTE intent bias, and used an unconstrained `params` object — so the
planner could (and did) return schema-valid empty actions (`params: {}`),
rendering blank suggestion-card rows.

New architecture (validated on the real Kron and Cricket PDFs in the Test
tenant — see scripts/proto_gemini_native_doc.py):

  Stage 1 (playbook): the REAL PDF goes to Gemini as a native document part
  with a required-field schema (next_steps[].action / implied_type /
  evidence_quote). The empty-action escape hatch is structurally impossible.
  Stage 2 (critic): a second pass verifies every proposed step against the
  document; unsupported steps are dropped, verified misses are INCLUDED
  (flagged) since the card is HITL anyway.
  Entities: typed with role, mapped into the card's suggested_entities shape.

Fallback chain (document understanding → text path → classic flow) preserves
the old behavior when bytes are missing or the model call fails.
"""

import json
from typing import Optional

from core.lib.audit_logger import audit_log_sync
from core.llm.constants import DOCUMENT_MODEL
from core.llm.providers import call_gemini

# Cap so a pathological upload can't push an absurd payload to the API.
_MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB


# ── Stage 1: playbook schema ─────────────────────────────────────────────────
# Required fields make the old flake class ("params: {}") structurally
# impossible: the model cannot emit a next_step without an action + type.
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
                    "role": {"type": "string", "description": "the entity's role in this document"},
                    "kind": {"type": "string", "enum": ["person", "organization", "product", "other"]},
                },
                "required": ["name", "kind"],
            },
        },
    },
    "required": ["document_type", "one_line_purpose", "explicit_ask", "next_steps"],
}

# ── Stage 2: critic schema ────────────────────────────────────────────────────
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
        "missed_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "implied_type": {"type": "string", "enum": ["task", "event", "note"]},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["action", "implied_type"],
            },
        },
        "fabrications": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdicts", "missed_actions"],
}

_PLAYBOOK_PROMPT = """You are reading a document the user received. Read the attached document carefully.

Answer as JSON:
- document_type: one word (proposal|meeting_minutes|contract|invoice|report|other)
- one_line_purpose: what this document is, in one sentence
- explicit_ask: what the document asks OF THE RECIPIENT (look for a "Next Steps" / call-to-action section). Quote it if present. "none" if truly nothing.
- next_steps: EVERY distinct concrete action this document implies for the recipient — list each phase, bullet, or separate ask as its OWN item; do NOT merge separate phases or separate asks into one item. BUT several bullets that describe the SAME single meeting/session = ONE event item, with the bullets summarized in its detail. Every item MUST have:
    - "action": an imperative sentence (e.g. "Schedule the 30-minute alignment session with the recipient's core team")
    - "implied_type": task|event|note (a meeting/session to schedule = event; something to build/do = task; information to retain = note)
    - "detail": one short sentence of specifics
    - "when": the phase/deadline if the document names one (e.g. "Phase 1"), else omit
    - "evidence_quote": the exact phrase in the document that justifies this step
  If the document implies nothing actionable, return an empty array.
- entities: the REAL people/organizations/products named, with their role in this document. Do NOT treat section titles, feature names, or abstract phrases as people or organizations.

Rules: read the actual document attached. Every next_step must be grounded in the document text — no invention."""

_CRITIC_PROMPT = """You are a strict auditor. Below is a document and a proposed extraction of its implied next steps.

DOCUMENT:
{doc_text}

PROPOSED EXTRACTION:
{extraction}

For each proposed next_step, decide supported (true/false): is it genuinely grounded in the document? One-line quote-based reasoning in "reason".
For missed_actions: real actions implied by the document (especially any explicit Next Steps section) that the extraction missed — each with "action" (imperative sentence), "implied_type" (task|event|note) and "evidence_quote". Do NOT pad; only real misses.
For fabrications: anything in the extraction that is NOT in the document.
Return JSON only."""


async def _playbook_pass(pdf_bytes: bytes) -> Optional[dict]:
    """Stage 1: send the REAL document to Gemini with the constrained schema."""
    from google.genai import types

    contents = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        _PLAYBOOK_PROMPT,
    ]
    text, _, _ = await call_gemini(
        model=DOCUMENT_MODEL,
        prompt=_PLAYBOOK_PROMPT,
        contents=contents,
        config={"response_mime_type": "application/json", "response_schema": PLAYBOOK_SCHEMA},
        timeout_s=120,
    )
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("playbook returned non-object JSON")
    return parsed


async def _critic_pass(doc_text: str, stage1: dict) -> Optional[dict]:
    """Stage 2: verify stage-1 output against the document text."""
    text, _, _ = await call_gemini(
        model=DOCUMENT_MODEL,
        prompt=_CRITIC_PROMPT.format(doc_text=doc_text[:20000], extraction=json.dumps(stage1)),
        config={"response_mime_type": "application/json", "response_schema": CRITIC_SCHEMA},
        timeout_s=120,
    )
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("critic returned non-object JSON")
    return parsed


def _kind_to_node_type(kind: str) -> str:
    """Map playbook entity kinds onto graph node types the card/confirm accept.

    person/organization pass through. product/other → concept (the graph has
    no product type yet — revisit when products become first-class).
    """
    if kind in ("person", "organization"):
        return kind
    return "concept"


def _step_to_action(step: dict, source: str = "playbook") -> dict:
    """Map a next_step onto the executor-action shape the confirm flow accepts."""
    implied = step.get("implied_type") or "task"
    if implied not in ("task", "event", "note"):
        implied = "task"
    action = {
        "operation": f"create_{implied}",
        "human_label": (step.get("action") or "").strip(),
        "confidence": 0.9 if source == "playbook" else 0.85,
    }
    params: dict = {}
    detail = (step.get("detail") or "").strip()
    when = (step.get("when") or "").strip()
    if implied == "note":
        if detail:
            params["content"] = detail
    else:
        if detail:
            params["description"] = detail
        if when:
            params["deadline"] = when
    evidence = (step.get("evidence_quote") or "").strip()
    if evidence:
        params["evidence_quote"] = evidence
    if params:
        action["params"] = params
    return action


def _build_breakdown(stage1: dict, critic: Optional[dict], entities: list) -> dict:
    """Assemble the card payload — same shape the Flutter SuggestionCard renders.

    Breakdown keys: document_type, summary, suggested_actions, suggested_entities.
    Critic verdicts/recovery provenance ride in `intelligence` for observability.
    """
    supported_actions: list = []
    rejected: list = []

    verdict_map = {}
    if critic:
        for v in critic.get("verdicts") or []:
            if isinstance(v, dict) and v.get("action"):
                verdict_map[str(v["action"]).strip().lower()] = v

    for step in stage1.get("next_steps") or []:
        if not isinstance(step, dict) or not (step.get("action") or "").strip():
            continue
        v = verdict_map.get(str(step.get("action")).strip().lower())
        if critic and v is not None and v.get("supported") is False:
            rejected.append(step.get("action"))
            continue
        supported_actions.append(_step_to_action(step, "playbook"))

    # Critic-recovered misses: include (HITL card — the user can uncheck),
    # flagged via confidence + metadata so the origin is visible.
    recovered = 0
    if critic:
        for m in critic.get("missed_actions") or []:
            if not isinstance(m, dict) or not (m.get("action") or "").strip():
                continue
            supported_actions.append(_step_to_action(m, "critic"))
            recovered += 1

    # P3-equivalent server-side guard: the card must never render a blank row.
    supported_actions = [a for a in supported_actions if a.get("human_label")]

    summary = stage1.get("one_line_purpose") or ""
    breakdown = {
        "document_type": stage1.get("document_type") or "document",
        "summary": summary,
        "suggested_actions": supported_actions,
        "suggested_entities": entities,
        "intelligence": {
            "engine": "gemini_native_document_understanding",
            "model": DOCUMENT_MODEL,
            "explicit_ask": stage1.get("explicit_ask") or "none",
            "critic_recovered_actions": recovered,
            "critic_rejected_actions": rejected,
            "fabrications": (critic or {}).get("fabrications") or [],
        },
    }
    return breakdown


async def parse_document(extracted_text: str, pdf_bytes: Optional[bytes] = None,
                         mime_type: str = "application/pdf") -> Optional[dict]:
    """Parse a document into the suggestion-card breakdown.

    Preferred path: Gemini-native document understanding on the real bytes
    (stage-1 playbook + stage-2 critic). Fallback: the legacy text path when
    bytes are unavailable or the model call fails.
    """
    # Native path (requires retainable PDF bytes)
    if pdf_bytes and mime_type == "application/pdf" and len(pdf_bytes) <= _MAX_PDF_BYTES:
        try:
            stage1 = await _playbook_pass(pdf_bytes)
            try:
                critic = await _critic_pass(extracted_text or "", stage1)
            except Exception as critic_err:
                audit_log_sync("document_parser", "WARNING",
                               f"Critic pass failed (keeping playbook output): {critic_err}")
                critic = None

            entities = []
            for e in stage1.get("entities") or []:
                if not isinstance(e, dict) or not (e.get("name") or "").strip():
                    continue
                entities.append({
                    "type": _kind_to_node_type(str(e.get("kind") or "other")),
                    "label": str(e.get("name")).strip(),
                    "confidence": 0.9,
                    "source": "document_understanding",
                    "role": (e.get("role") or "").strip(),
                })

            breakdown = _build_breakdown(stage1, critic, entities)
            audit_log_sync("document_parser", "INFO",
                           f"Native parse ok: {len(breakdown['suggested_actions'])} actions "
                           f"({breakdown['intelligence']['critic_recovered_actions']} recovered, "
                           f"{len(breakdown['intelligence']['critic_rejected_actions'])} rejected), "
                           f"{len(entities)} entities")
            if breakdown["suggested_actions"] or entities:
                return breakdown
            audit_log_sync("document_parser", "INFO",
                           "Native parse produced nothing actionable — falling back to text path")
        except Exception as native_err:
            audit_log_sync("document_parser", "WARNING",
                           f"Native document understanding failed, falling back to text path: {native_err}")

    # Legacy text path (bytes missing, non-PDF, or native failed)
    return await _parse_document_text(extracted_text)


async def _parse_document_text(extracted_text: str) -> Optional[dict]:
    """Legacy extraction: unified suggestion extractor + context, on text only.

    Kept as the fallback chain's last step (old documents, native failures).
    """
    from core.lib.suggestion_extractor import extract_suggestions
    from core.lib.entity_context import extract_context_from_source

    actions, breakdown = await extract_suggestions(extracted_text, intent="NOTE")
    if not breakdown:
        return None

    ctx = await extract_context_from_source(extracted_text, timing="card")
    breakdown["suggested_entities"] = ctx.detected_entities
    breakdown["entity_context"] = ctx.to_dict()

    return breakdown
