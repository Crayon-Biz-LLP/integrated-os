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

export async function decideMergeProposal(id: number, decision: 'accept' | 'reject', swap?: boolean): Promise<void> {
  const res = await fetch('/api/graph-merge-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: decision, swap }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide merge proposal' }));
    throw new Error(err.detail || 'Failed to decide merge proposal');
  }
}

export async function decideGraphNode(id: number, decision: 'approve' | 'reject', updates?: { org_tag?: string; context?: string }): Promise<void> {
  const res = await fetch('/api/graph-node-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: decision, ...updates }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide graph node' }));
    throw new Error(err.detail || 'Failed to decide graph node');
  }
}

export async function mergeGraphNodeIntoExisting(pendingId: number, targetId: string, orgTag?: string): Promise<void> {
  const res = await fetch('/api/graph-node-merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: pendingId, target_id: targetId, org_tag: orgTag }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to merge graph node' }));
    throw new Error(err.detail || 'Failed to merge graph node');
  }
}

export async function searchGraphNodes(query: string, type?: string): Promise<{ id: string; label: string; type: string }[]> {
  const params = new URLSearchParams({ q: query });
  if (type) params.append('type', type);
  
  const res = await fetch(`/api/graph-nodes/search?${params.toString()}`);
  if (!res.ok) {
    return [];
  }
  const json = await res.json();
  return json.data || [];
}