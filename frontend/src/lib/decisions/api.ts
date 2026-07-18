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

export async function submitClarification(shortcode: string, answer: string): Promise<any> {
  const res = await fetch('/api/clarification', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shortcode, answer }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to submit clarification' }));
    throw new Error(err.detail || 'Failed to submit clarification');
  }
  return res.json();
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

export async function decideGraphNode(id: number, decision: 'approve' | 'reject' | 'unreject', updates?: { context?: string; label?: string }): Promise<any> {
  const res = await fetch('/api/graph-node-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: decision, ...updates }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide graph node' }));
    throw new Error(err.detail || 'Failed to decide graph node');
  }
  return res.json();
}

export async function batchDecideCallItems(ids: number[], decision: 'approve' | 'reject'): Promise<{ processed: number; failed: number }> {
  const res = await fetch('/api/call-action/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, action: decision }),
  });
  if (!res.ok) throw new Error('Batch call action failed');
  return res.json();
}

export async function batchDecideWhatsAppMessages(ids: number[], decision: 'approve' | 'reject'): Promise<{ processed: number; failed: number }> {
  const res = await fetch('/api/whatsapp-action/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, action: decision }),
  });
  if (!res.ok) throw new Error('Batch WhatsApp action failed');
  return res.json();
}

export async function batchDecideGraphEdges(ids: number[], decision: 'approve' | 'reject'): Promise<{ processed: number; failed: number }> {
  const res = await fetch('/api/graph-edge-action/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, action: decision }),
  });
  if (!res.ok) throw new Error('Batch graph edge action failed');
  return res.json();
}

export async function batchDecideGraphNodes(ids: number[], decision: 'approve' | 'reject'): Promise<{ processed: number; failed: number }> {
  const res = await fetch('/api/graph-node-action/batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, action: decision }),
  });
  if (!res.ok) throw new Error('Batch graph node action failed');
  return res.json();
}

export async function mergeGraphNodeIntoExisting(pendingId: number | string, targetId: string, scope: 'pending' | 'live' | 'rejected' = 'pending'): Promise<void> {
  const res = await fetch('/api/graph-node-merge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: pendingId, target_id: targetId, scope }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to merge graph node' }));
    throw new Error(err.detail || 'Failed to merge graph node');
  }
}

export async function searchGraphNodes(query: string, type?: string, scope?: string): Promise<{ id: string; label: string; type: string }[]> {
  const params = new URLSearchParams({ q: query });
  if (type) params.append('type', type);
  if (scope) params.append('scope', scope);
  
  const res = await fetch(`/api/graph-nodes/search?${params.toString()}`);
  if (!res.ok) {
    return [];
  }
  return await res.json();
}


export async function renamePendingGraphNode(id: number | string, newLabel: string, scope: 'pending' | 'live' | 'rejected' = 'pending'): Promise<void> {
  const res = await fetch(`/api/graph-node/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label: newLabel, scope }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to rename graph node' }));
    throw new Error(err.detail || 'Failed to rename graph node');
  }
}

export async function changePendingGraphNodeType(id: number | string, newType: string, scope: 'pending' | 'live' | 'rejected' = 'pending'): Promise<void> {
  const res = await fetch(`/api/graph-node/${id}/type`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: newType, scope }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to change graph node type' }));
    throw new Error(err.detail || 'Failed to change graph node type');
  }
}

export async function deletePendingGraphNode(id: number | string, scope: 'pending' | 'live' | 'rejected' = 'pending'): Promise<{ message: string }> {
  const params = new URLSearchParams({ scope });
  const res = await fetch(`/api/graph-node/${id}?${params.toString()}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to delete graph node' }));
    throw new Error(err.detail || 'Failed to delete graph node');
  }
  return res.json();
}

export async function checkSimilarGraphNodes(label: string, type: string) {
  const params = new URLSearchParams({ label, type, threshold: '0.85' });
  const res = await fetch(`/api/graph-nodes/similar?${params.toString()}`);
  if (!res.ok) return [];
  return res.json();
}

export async function checkSimilarGraphEdges(source: string, target: string, rel: string) {
  const params = new URLSearchParams({ source, target, rel });
  const res = await fetch(`/api/graph-edges/similar?${params.toString()}`);
  if (!res.ok) return [];
  return res.json();
}

export async function fetchAutoDecisions(limit = 100): Promise<any[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  const res = await fetch(`/api/auto-decisions?${params.toString()}`);
  if (!res.ok) {
    console.error("Failed to fetch auto-decisions:", await res.text());
    return [];
  }
  return await res.json();
}

export async function verifyAutoDecision(id: number): Promise<boolean> {
  const res = await fetch(`/api/auto-decisions/${id}/verify`, { method: 'PATCH' });
  if (!res.ok) {
    console.error("Failed to verify auto-decision:", await res.text());
    return false;
  }
  return true;
}

export async function rejectAutoDecision(id: number): Promise<boolean> {
  const res = await fetch(`/api/auto-decisions/${id}/reject`, { method: 'PATCH' });
  if (!res.ok) {
    console.error("Failed to reject auto-decision:", await res.text());
    return false;
  }
  return true;
}
export async function fetchLiveGraphNodes(): Promise<any[]> {
  const res = await fetch('/api/graph-nodes/live');
  if (!res.ok) {
    const errText = await res.text();
    let errJson;
    try {
      errJson = JSON.parse(errText);
    } catch {
      // Ignore
    }
    const msg = errJson?.error || errJson?.detail || errText || `Failed with status ${res.status}`;
    throw new Error(`Failed to fetch live nodes: ${msg}`);
  }
  const json = await res.json();
  return (json.data || []);
}
