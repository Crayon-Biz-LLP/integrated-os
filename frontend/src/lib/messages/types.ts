export interface Message {
  id: number;
  content: string;
  created_at: string;
  direction: 'incoming' | 'outgoing';
  sender: 'user' | 'telegram' | 'system';
  message_type: 'chat' | 'task' | 'note' | 'briefing' | 'clarification' | 'acknowledgment' | 'system' | 'response';
  status: string;
  metadata: string | Record<string, any>;
  source: string;
}

export interface MessagesResponse {
  messages: Message[];
}

export interface SendMessageResponse {
  success: boolean;
  message: string;
}
