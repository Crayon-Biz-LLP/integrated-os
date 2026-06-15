import math
from datetime import datetime, timezone
from core.services.db import get_supabase
from core.llm.config import CONTEXT_TOKEN_BUDGETS

LAMBDA_DECAY = 0.023
W_DIST = {0: 1.0, 1: 1.0, 2: 0.5}
W_EPI  = {'asserted': 1.0, 'inferred': 0.8, 'hypothetical': 0.4}

def compute_css(node: dict, depth: int, as_of: datetime) -> float:
    count = node.get('reference_count', 0)
    epi = node.get('epistemic_status', 'inferred')
    last_ref = node.get('last_referenced_at')
    
    freq = 1 + math.log1p(count)
    dist = W_DIST.get(depth, 0.25)
    epi_w = W_EPI.get(epi, 0.5)
    
    if last_ref:
        if isinstance(last_ref, str):
            last_ref = datetime.fromisoformat(last_ref.replace('Z', '+00:00'))
        days = (as_of - last_ref).total_seconds() / 86400
    else:
        days = 30
        
    decay = math.exp(-LAMBDA_DECAY * days)
    return freq * dist * decay * epi_w


def _update_salience(node_ids: list[str]):
    if not node_ids:
        return
    supabase = get_supabase()
    supabase.rpc('increment_node_salience', {'p_node_ids': node_ids}).execute()


def assemble_context(
    entity_label: str,
    intent: str,
    budget_key: str,
    as_of: datetime = None
) -> list[dict]:
    as_of = as_of or datetime.now(timezone.utc)
    budget = CONTEXT_TOKEN_BUDGETS.get(budget_key, 500)
    
    supabase = get_supabase()
    rows = supabase.rpc('get_context_for', {
        'p_entity_label': entity_label,
        'p_intent':       intent,
        'p_as_of':        as_of.isoformat()
    }).execute().data or []
    
    scored = sorted(
        [{'node': r, 'css': compute_css(r, r['depth'], as_of)} for r in rows],
        key=lambda x: x['css'], reverse=True
    )
    
    packed, used = [], 0
    node_ids = []
    
    for item in scored:
        node_data = item['node']
        # Rough token approximation
        tokens = len(str(node_data)) // 4
        if used + tokens > budget:
            break
        packed.append(node_data)
        node_ids.append(node_data['node_id'])
def _resolve_sender_to_person(sender_email: str) -> dict | None:
    supabase = get_supabase()
    # Assuming people table has email or we match by something. 
    # For now, let's just do a basic lookup in graph_nodes directly, or people table.
    res = supabase.table('people').select('*').ilike('name', f'%{sender_email}%').execute()
    if res.data:
        return res.data[0]
    return None

def enrich_sender_context(sender_email: str) -> dict | None:
    person = _resolve_sender_to_person(sender_email)
    if not person:
        return None
    return assemble_context(
        entity_label=person['name'],
        intent='summary',
        budget_key='email_triage'
    )

def _render_pulse(sections: list) -> str:
    output = []
    for sec in sections:
        output.append(f"Project: {sec['project']}")
        if sec['blockers']:
            output.append("  Blockers: " + ", ".join([str(b) for b in sec['blockers']]))
        if sec['key_people']:
            output.append("  Key People: " + ", ".join([str(p) for p in sec['key_people']]))
    return "\n".join(output)

def generate_morning_pulse(active_projects: list[str]) -> str:
    sections = []
    for project in active_projects:
        blockers   = assemble_context(project, 'blockers', 'morning_pulse')
        key_people = assemble_context(project, 'people',   'morning_pulse')
        sections.append({
            'project':    project,
            'blockers':   blockers,
            'key_people': key_people,
        })
    return _render_pulse(sections)
