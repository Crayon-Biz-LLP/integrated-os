export type EmailClassification = 'actionable' | 'fyi' | 'ignored';
export type EmailSource = 'gmail' | 'outlook';
export type DraftStatus = 'pending' | 'approved' | 'rejected';
export type TaskDecision = 'yes' | 'no' | 'expired' | null;

export interface Email {
  id: number;
  subject: string;
  sender: string | null;
  sender_email: string;
  body_summary: string | null;
  classification: EmailClassification;
  source: EmailSource;
  received_at: string;
  linked_project_id: number | null;
  linked_person_id: number | null;
  linked_project?: { name: string } | null;
  linked_person?: { name: string } | null;
}

export interface EmailPendingTask {
  id: number;
  email_id: number;
  suggested_title: string;
  suggested_project: string | null;
  is_human_sender: boolean;
  created_at: string;
  danny_decision: TaskDecision;
  email?: {
    subject: string;
    sender_email: string;
    sender: string | null;
  } | null;
}

export interface EmailDraft {
  id: number;
  email_id: number;
  draft_body: string;
  status: DraftStatus;
  created_at: string;
  email?: {
    subject: string;
    sender_email: string;
    sender: string | null;
    source: EmailSource;
  } | null;
}

export interface EmailFilters {
  classification: EmailClassification | 'all';
  source: EmailSource | 'all';
  search: string;
}

export interface EmailStats {
  total: number;
  actionable: number;
  fyi: number;
  pending_tasks: number;
  pending_drafts: number;
}
