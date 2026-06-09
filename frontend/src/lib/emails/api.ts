export async function approveShortcode(shortcode: number): Promise<void> {
  const res = await fetch('/api/email-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shortcode, action: 'approve' }),
  });
  if (!res.ok) throw new Error('Failed to approve shortcode');
}

export async function rejectShortcode(shortcode: number): Promise<void> {
  const res = await fetch('/api/email-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shortcode, action: 'reject' }),
  });
  if (!res.ok) throw new Error('Failed to reject shortcode');
}

export async function decideTask(id: number, decision: 'yes' | 'no'): Promise<void> {
  const action = decision === 'yes' ? 'approve' : 'reject';
  const res = await fetch('/api/email-action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Failed to decide task' }));
    throw new Error(err.detail || 'Failed to decide task');
  }
}

export async function approveDraft(id: number): Promise<{ success: boolean; error?: string }> {
  const res = await fetch('/api/send-draft', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ draft_id: id }),
  });
  return res.json();
}

export async function rejectDraft(id: number): Promise<void> {
  const res = await fetch('/api/emails/drafts', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, action: 'rejected' }),
  });
  if (!res.ok) throw new Error('Failed to reject draft');
}

export async function updateDraftBody(id: number, body: string): Promise<void> {
  const res = await fetch('/api/emails/drafts', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, draft_body: body }),
  });
  if (!res.ok) throw new Error('Failed to update draft body');
}
