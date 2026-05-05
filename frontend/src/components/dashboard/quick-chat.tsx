'use client';

import { useState, useEffect, useRef } from 'react';
import { Message } from '@/lib/messages/types';
import { fetchMessages, sendMessage } from '@/lib/messages/api';
import { Button } from '@/components/ui/button';
import { Send } from 'lucide-react';

// Safe metadata parser
const parseMetadata = (meta: string | Record<string, any>): Record<string, any> => {
  if (typeof meta === 'object' && meta !== null) return meta;
  if (typeof meta !== 'string') return {};
  try {
    return JSON.parse(meta);
  } catch {
    return {};
  }
};

export function QuickChat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-refresh every 30 seconds
  useEffect(() => {
    const loadMessages = async () => {
      try {
        const data = await fetchMessages(5);
        setMessages(data);
      } catch (error) {
        console.error('Failed to load messages:', error);
      }
    };

    loadMessages();
    
    const interval = setInterval(loadMessages, 30000);
    return () => clearInterval(interval);
  }, []);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = async () => {
    if (!input.trim() || sending) return;
    
    setSending(true);
    try {
      await sendMessage(input);
      setInput('');
      const data = await fetchMessages(5);
      setMessages(data);
    } catch (error) {
      console.error('Failed to send:', error);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="card-premium p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">💬 Quick Chat</h2>
        <a 
          href="/dashboard/messages"
          className="inline-flex items-center justify-center rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted"
        >
          Open Full Chat →
        </a>
      </div>
      
      <div className="space-y-3 max-h-64 overflow-y-auto">
        {messages.map((msg) => {
          const isUser = msg.sender === 'user';
          const senderLabel = msg.sender === 'system' ? 'Rhodey' : 'You';
          
          return (
            <div 
              key={msg.id} 
              className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}
            >
              <div 
                className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm ${
                  isUser 
                    ? 'bg-teal-600 text-white rounded-br-md' 
                    : 'bg-muted text-foreground rounded-bl-md'
                }`}
              >
                <p className="text-[10px] font-medium text-muted-foreground mb-1">
                  {senderLabel}
                </p>
                <p className="whitespace-pre-wrap break-words leading-relaxed">
                  {msg.content}
                </p>
                <p className={`text-[10px] mt-1 font-mono ${
                  isUser ? 'text-white/60' : 'text-muted-foreground/50'
                }`}>
                  {new Date(msg.created_at).toLocaleTimeString('en-US', { 
                    hour: 'numeric', 
                    minute: '2-digit' 
                  })}
                </p>
              </div>
            </div>
          );
        })}
        <div ref={messagesEndRef} />
      </div>
      
      <div className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder="Type message..."
          className="flex-1 px-4 py-2 border rounded-lg text-sm bg-background"
        />
        <Button onClick={handleSend} disabled={sending} size="sm">
          {sending ? '...' : <Send className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  );
}
