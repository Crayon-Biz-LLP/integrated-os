export interface Message {
  id: number;
  content: string;
  created_at: string;
  direction: 'incoming' | 'outgoing';
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
