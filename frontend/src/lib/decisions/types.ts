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

export interface GraphPendingEdge {
  id: number;
  source_label: string;
  target_label: string;
  relationship: string;
  source_text: string;
  confidence: number;
  status: 'pending' | 'approved' | 'rejected' | 'awaiting_clarification';
  created_at: string;
  source_type?: string;
  target_type?: string;
  clarification?: {
    shortcode: string;
    question: string;
    question_type: string;
    answer?: string;
  };
  eval_context?: {
    justification?: string;
    [key: string]: any;
  };
  epistemic_status?: string;
}

export interface GraphMergeProposal {
  id: number;
  label: string;
  type: string;
  merge_candidate_id: string;
  merge_candidate_label?: string;
  status: 'merge_proposed';
  created_at: string;
}

export interface GraphPendingNode {
  id: number;
  label: string;
  type: string;
  source_text: string;
  status: 'pending' | 'flagged' | 'approved' | 'rejected' | 'awaiting_clarification';
  created_at: string;
  clarification?: {
    shortcode: string;
    question: string;
    question_type: string;
    answer?: string;
  };
  eval_context?: {
    justification?: string;
    linked_entity?: string;
    relationship?: string;
    frequency?: string;
    confidence?: number;
    health_score?: number;
    typical_time?: string;
    [key: string]: any;
  };
  epistemic_status?: string;
}


