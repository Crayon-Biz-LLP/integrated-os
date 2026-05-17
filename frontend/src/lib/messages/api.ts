import { Message, MessagesResponse, SendMessageResponse } from './types';

export async function fetchMessages(limit = 50, offset = 0): Promise<Message[]> {
  const res = await fetch(`/api/messages?limit=${limit}&offset=${offset}`);
  if (!res.ok) throw new Error('Failed to fetch messages');
  const data: MessagesResponse = await res.json();
  return data.messages || [];
}

export async function sendMessage(message: string): Promise<SendMessageResponse> {
  const res = await fetch('/api/send-message', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error('Failed to send message');
  return res.json();
}
