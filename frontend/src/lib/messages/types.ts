export interface Message {
  id: number;
  content: string;
  created_at: string;
  direction: 'incoming' | 'outgoing';
  sender: 'user' | 'telegram' | 'system';
  message_type: 'chat' | 'task' | 'note' | 'briefing' | 'clarification' | 'system';
  status: string;
  metadata: string | Record<string, any>;
}

export interface MessagesResponse {
  messages: Message[];
}

export interface SendMessageResponse {
  success: boolean;
  message: string;
}
