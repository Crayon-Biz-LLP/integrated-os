import type { Email, EmailFilters, EmailStats, EmailPendingTask, EmailDraft } from './types';

function buildQuery(filters: EmailFilters): string {
  const params = new URLSearchParams();
  if (filters.classification !== 'all') params.set('classification', filters.classification);
  if (filters.source !== 'all') params.set('source', filters.source);
  if (filters.search) params.set('search', filters.search);
  const qs = params.toString();
  return `/api/emails${qs ? `?${qs}` : ''}`;
}

export async function fetchEmails(filters: EmailFilters): Promise<Email[]> {
  const res = await fetch(buildQuery(filters), { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch emails');
  return res.json();
}

export async function fetchEmailStats(): Promise<EmailStats> {
  const res = await fetch('/api/emails/stats', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch email stats');
  return res.json();
}

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

export async function fetchPendingTasks(): Promise<EmailPendingTask[]> {
  const res = await fetch('/api/emails/pending-tasks', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch pending tasks');
  return res.json();
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

export async function fetchPendingDrafts(): Promise<EmailDraft[]> {
  const res = await fetch('/api/emails/drafts', { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch pending drafts');
  return res.json();
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
