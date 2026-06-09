export interface CallPendingItem {
  id: number;
  recording_id: string | null;
  suggested_title: string;
  suggested_project: string | null;
  action_type: 'task' | 'decision' | 'note';
  summary: string | null;
  people_mentioned: string | null;
  created_at: string;
  danny_decision: string | null;
  shown_in_brief: boolean | null;
}

export interface WhatsAppPendingMessage {
  id: number;
  sender_name: string;
  sender_phone: string;
  message_text: string;
  classification: string;
  summary: string | null;
  suggested_title: string | null;
  suggested_project: string | null;
  linked_person_name: string | null;
  has_memory_value: boolean;
  received_at: string;
  created_at: string;
  danny_decision: string | null;
  shown_in_brief: boolean | null;
}


