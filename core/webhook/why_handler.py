"""
Why Handler — explains the last bot decision in plain language.

Triggered by:
  - "/why"
  - Conversational phrases: "why did you", "how come", "explain why", etc.

Resolution:
  1. Look up the conversation thread for this chat_id
  2. Read last_decision_chain_id from the thread
  3. Query audit_logs WHERE service='decision_audit'
     AND metadata->>'decision_chain_id' = <chain_id>
  4. Format per stage and send via send_telegram()
"""

import json
from core.webhook.utils import supabase
from core.webhook.telegram import send_telegram


_STAGE_LABELS = {
    "classification": "Classification",
    "routing": "Routing",
    "context_registry": "Context Filter",
    "retrieval": "Retrieval",
}

_REASON_LABELS = {
    "no_entity_overlap": "no entity overlap with your query",
    "below_threshold": "similarity below threshold",
    "fact_source_priority": "lower-priority fact source",
    "cross_project_adjacency": "cross-project (adjacent, not exact)",
    "top_k_truncated": "cut by top-k limit",
    "neutral_downgraded": "no named entities — score halved",
    "hard_gate_rejected": "hard gate: entity required, none matched",
    "soft_gate_downgraded": "soft gate: entity mismatch, score reduced",
    "semantic_skipped_no_anchor": "semantic search skipped — no entity anchor",
    "retrieved": "retrieved",
}


def _resolve_chain_id(chat_id: int, session_id: str) -> str:
    """Return the most recent decision_chain_id for this chat."""
    try:
        if session_id:
            res = supabase.table('conversation_threads') \
                .select('last_decision_chain_id') \
                .eq('id', session_id) \
                .maybe_single() \
                .execute()
            if res.data and res.data.get('last_decision_chain_id'):
                return res.data['last_decision_chain_id']

        # Fallback: latest non-archived thread for this chat
        res = supabase.table('conversation_threads') \
            .select('last_decision_chain_id') \
            .eq('chat_id', chat_id) \
            .is_('archived_at', 'null') \
            .not_.is_('last_decision_chain_id', 'null') \
            .order('last_active_at', desc=True) \
            .limit(1) \
            .execute()
        if res.data:
            return res.data[0].get('last_decision_chain_id', '')
    except Exception:
        pass
    return ''


def _fetch_decision_records(chain_id: str) -> list:
    """Fetch all audit_log rows for this decision chain, ordered by creation."""
    try:
        res = supabase.table('audit_logs') \
            .select('id, message, metadata, created_at') \
            .eq('service', 'decision_audit') \
            .order('created_at', desc=False) \
            .execute()

        # Filter in Python since PostgREST JSON path filtering varies by version
        records = []
        for row in (res.data or []):
            meta = row.get('metadata')
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    continue
            if isinstance(meta, dict) and meta.get('decision_chain_id') == chain_id:
                row['_meta'] = meta
                records.append(row)
        return records
    except Exception:
        return []


def _fmt_item(item: dict, with_reason: bool = False) -> str:
    content = (item.get('content') or '').strip()[:80]
    source = item.get('source', '')
    score = item.get('score', 0)
    line = f"  • [{source}] {content}"
    if score and score != 1.0:
        line += f" (score: {score:.2f})"
    if with_reason:
        reason_key = item.get('reason', '')
        reason_label = _REASON_LABELS.get(str(reason_key), str(reason_key))
        line += f" — {reason_label}"
    return line


def format_decision_chain(records: list) -> str:
    if not records:
        return "No decision records found for the last response."

    lines = ["*Decision audit for my last response:*\n"]

    for rec in records:
        meta = rec.get('_meta', {})
        stage = meta.get('stage', '')
        label = _STAGE_LABELS.get(stage, stage.title())
        summary = meta.get('summary', '')
        included = meta.get('included_items', [])
        excluded = meta.get('excluded_items', [])
        entities = meta.get('resolved_entities', [])
        query_text = meta.get('query_text', '')

        lines.append(f"*{label}*")
        if summary:
            lines.append(f"_{summary}_")
        if query_text and stage not in ('routing', 'classification'):
            lines.append(f"Query: `{query_text[:60]}`")
        if entities:
            lines.append(f"Anchors: {', '.join(entities[:5])}")

        if stage == 'context_registry':
            if included:
                lines.append(f"Kept ({len(included)}):")
                for item in included[:5]:
                    lines.append(_fmt_item(item))
            if excluded:
                lines.append(f"Excluded ({len(excluded)}):")
                for item in excluded[:5]:
                    lines.append(_fmt_item(item, with_reason=True))

        elif stage == 'retrieval':
            if included:
                sources = [item.get('source', item.get('id', '')) for item in included]
                lines.append(f"Sources: {', '.join(sources[:10])}")

        lines.append("")

    return "\n".join(lines).strip()


async def handle_why(chat_id: int, session_id: str):
    """Fetch and format the decision chain for the last bot response."""
    chain_id = _resolve_chain_id(chat_id, session_id)
    if not chain_id:
        await send_telegram(
            chat_id,
            "_No decision records found. Ask a question first, then try /why._",
            skip_validation=True
        )
        return

    records = _fetch_decision_records(chain_id)
    reply = format_decision_chain(records)
    await send_telegram(chat_id, reply, skip_validation=True)
