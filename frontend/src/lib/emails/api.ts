import { createClient } from '@/lib/supabase';
import type {
  Email,
  EmailFilters,
  EmailStats,
  EmailPendingTask,
  EmailDraft,
} from './types';

export async function fetchEmails(filters: EmailFilters): Promise<Email[]> {
  const supabase = createClient();
  let query = supabase
    .from('emails')
    .select(`
      id,
      subject,
       sender,
       sender_email,
       body_summary,
      classification,
      source,
      received_at,
      linked_project_id,
      linked_person_id,
      linked_project:projects(name),
      linked_person:people(name)
    `)
    .order('received_at', { ascending: false })
    .limit(100);

  if (filters.classification !== 'all') {
    query = query.eq('classification', filters.classification);
  }
  if (filters.source !== 'all') {
    query = query.eq('source', filters.source);
  }
  if (filters.search) {
    query = query.or(`subject.ilike.%${filters.search}%,sender_email.ilike.%${filters.search}%,sender.ilike.%${filters.search}%`);
  }

  const { data, error } = await query;
  if (error) throw error;
  return (data || []) as unknown as Email[];
}

export async function fetchEmailStats(): Promise<EmailStats> {
  const res = await fetch(`/api/emails/stats`, { cache: "no-store" });
  if (!res.ok) throw new Error("Failed to fetch email stats");
  return res.json();
}

export async function approveShortcode(shortcode: number): Promise<void> {
  const res = await fetch(`/api/email-action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ shortcode, action: 'approve' }),
  });
  if (!res.ok) throw new Error("Failed to approve shortcode");
}

export async function rejectShortcode(shortcode: number): Promise<void> {
  const res = await fetch(`/api/email-action`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ shortcode, action: 'reject' }),
  });
  if (!res.ok) throw new Error("Failed to reject shortcode");
}

export async function fetchPendingTasks(): Promise<EmailPendingTask[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('email_pending_tasks')
    .select(`
      *,
       email:emails(subject, sender_email, sender)
    `)
    .is('danny_decision', null)
    .order('created_at', { ascending: false });

  if (error) throw error;
  return data || [];
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
  const supabase = createClient();
  const { data, error } = await supabase
    .from('email_drafts')
    .select(`
      *,
       email:emails(subject, sender_email, sender, source)
    `)
    .eq('status', 'pending')
    .order('created_at', { ascending: false });

  if (error) throw error;
  return data || [];
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
  const supabase = createClient();
  const { error } = await supabase
    .from('email_drafts')
    .update({ status: 'rejected' })
    .eq('id', id);

  if (error) throw error;
}

export async function updateDraftBody(id: number, body: string): Promise<void> {
  const supabase = createClient();
  const { error } = await supabase
    .from('email_drafts')
    .update({ draft_body: body })
    .eq('id', id);

  if (error) throw error;
}
