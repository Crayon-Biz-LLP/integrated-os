export async function decideCallItem(id: number, decision: 'approve' | 'reject'): Promise<void> {
  const res = await fetch('/api/call-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: decision }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide call item' }));
    throw new Error(err.detail || 'Failed to decide call item');
  }
}

export async function decideWhatsAppMessage(id: number, decision: 'approve' | 'reject'): Promise<void> {
  const res = await fetch('/api/whatsapp-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: decision }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide WhatsApp message' }));
    throw new Error(err.detail || 'Failed to decide WhatsApp message');
  }
}

export async function decideGraphEdge(id: number, decision: 'approve' | 'reject', updates?: { new_source?: string; new_target?: string; new_rel?: string; new_context?: string; }): Promise<void> {
  const res = await fetch('/api/graph-edge-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: decision, ...updates }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide graph edge' }));
    throw new Error(err.detail || 'Failed to decide graph edge');
  }
}

export async function decideMergeProposal(id: number, decision: 'accept' | 'reject', updates?: { new_label?: string }): Promise<void> {
  const res = await fetch('/api/graph-merge-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: decision, ...updates }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide merge proposal' }));
    throw new Error(err.detail || 'Failed to decide merge proposal');
  }
}