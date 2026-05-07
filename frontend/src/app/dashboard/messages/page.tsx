'use client';

import { useState, useEffect, useRef } from 'react';
import { Send, MessageSquare, ArrowDown, Loader2 } from 'lucide-react';
import { fetchMessages, sendMessage } from '@/lib/messages/api';
import { Message } from '@/lib/messages/types';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export default function MessagesPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [inputText, setInputText] = useState('');
  const [sending, setSending] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const loadMessages = async () => {
    setLoading(true);
    try {
      const data = await fetchMessages(100);
      setMessages(data);
    } catch (error) {
      console.error('Failed to load messages:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadMessages();
  }, []);

  useEffect(() => {
    // Scroll to bottom on initial load AND when new messages arrive
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const handleSend = async () => {
    if (!inputText.trim() || sending) return;
    
    setSending(true);
    try {
      await sendMessage(inputText);
      setInputText('');
      await loadMessages();
    } catch (error) {
      console.error('Failed to send message:', error);
      alert('Failed to send message. Check console for details.');
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const formatTime = (dateStr: string) => {
    const date = new Date(dateStr);
    return date.toLocaleTimeString('en-GB', { 
      hour: '2-digit', 
      minute: '2-digit',
      hour12: false 
    });
  };

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    
    if (date.toDateString() === today.toDateString()) return 'Today';
    if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  };

  // Safe metadata parser (handles double-escaped JSON)
  const parseMetadata = (meta: string | Record<string, any>): Record<string, any> => {
    if (typeof meta === 'object' && meta !== null) return meta;
    if (typeof meta !== 'string') return {};
    
    let cleaned = meta;
    // Handle double-escaped JSON: "\"{\\\\"key\\\":...}\""
    if (cleaned.startsWith('"') && cleaned.endsWith('"')) {
      try {
        cleaned = JSON.parse(cleaned); // Unwrap first layer
      } catch {
        // If that fails, try replacing escaped quotes
        cleaned = cleaned.replace(/\\"/g, '"').slice(1, -1);
      }
    }
    
    try {
      return JSON.parse(cleaned);
    } catch (e) {
      console.error('Metadata parse error:', meta);
      return {};
    }
  };

  // Filter to only show chat messages and bot responses (not task raw_dumps)
  const chatMessages = messages.filter(m => 
    m.message_type === 'chat' || 
    m.message_type === 'briefing' || 
    m.message_type === 'acknowledgment' ||
    m.sender === 'user' ||
    m.direction === 'outgoing'
  );

  // Reverse to show OLDEST first (like Telegram/WhatsApp - newest at bottom near input)
  const sortedMessages = [...chatMessages].reverse();

  // Group messages by date
  const groupedMessages = sortedMessages.reduce((groups: Record<string, Message[]>, msg) => {
    const date = formatDate(msg.created_at);
    if (!groups[date]) groups[date] = [];
    groups[date].push(msg);
    return groups;
  }, {});

  const getSenderLabel = (msg: Message): string => {
    if (msg.message_type === 'acknowledgment' || msg.message_type === 'briefing' || msg.sender === 'system') {
      return 'Rhodey';
    }
    // All user messages (web or telegram) show as "You"
    if (msg.sender === 'user' || msg.direction === 'outgoing') return 'You';
    return 'You';
  };

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] lg:h-[calc(100vh-4rem)]">
      {/* Header */}
      <div className="border-b border-border/60 px-4 py-3 bg-background/60 backdrop-blur-sm">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-5 w-5 text-primary" />
          <h1 className="text-lg font-semibold">Messages</h1>
          <span className="ml-auto text-xs text-muted-foreground font-mono">
            {messages.length} messages
          </span>
        </div>
        <p className="text-xs text-muted-foreground/70 mt-0.5">
          Send and receive messages via Telegram
        </p>
      </div>

      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {loading && (
          <div className="space-y-3">
            {[1, 2, 3].map(i => (
              <div key={i} className="flex gap-2">
                <div className="h-8 w-8 rounded-full bg-muted animate-pulse" />
                <div className="space-y-2 flex-1">
                  <div className="h-4 w-3/4 rounded bg-muted animate-pulse" />
                  <div className="h-3 w-1/2 rounded bg-muted animate-pulse" />
                </div>
              </div>
            ))}
          </div>
        )}

        {!loading && messages.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
            <MessageSquare className="h-12 w-12 mb-3 text-muted-foreground/50" />
            <p className="text-sm font-medium">No messages yet</p>
            <p className="text-xs text-muted-foreground/70 mt-1">
              Send your first message below
            </p>
          </div>
        )}

        {!loading && Object.entries(groupedMessages).map(([date, msgs]) => (
          <div key={date}>
            <div className="flex items-center gap-2 my-4">
              <div className="flex-1 border-t border-border/40" />
              <span className="text-xs text-muted-foreground font-medium">{date}</span>
              <div className="flex-1 border-t border-border/40" />
            </div>
            
            <div className="space-y-3">
                {msgs.map((msg) => {
                const isOutgoing = msg.direction === 'outgoing';
                const metadata = parseMetadata(msg.metadata);
                
                const senderLabel = getSenderLabel(msg);
                // Only user messages (sender: "user") get teal color, NOT system messages with direction: "outgoing"
                const isUser = msg.sender === 'user';
                
                return (
                  <div
                    key={msg.id}
                    className={cn(
                      'flex',
                      isUser ? 'justify-end' : 'justify-start'
                    )}
                  >
                    <div
                      className={cn(
                        'max-w-[80%] rounded-2xl px-4 py-2.5 text-sm',
                        isUser
                          ? 'bg-teal-600 text-white rounded-br-md'  // Teal for user messages
                          : 'bg-muted text-foreground rounded-bl-md'
                      )}
                    >
                      <p className="text-[10px] font-medium text-muted-foreground mb-1">
                        {senderLabel}
                      </p>
                      <p className="whitespace-pre-wrap break-words leading-relaxed">
                        {msg.content}
                      </p>
                      <p
                        className={cn(
                          'text-[10px] mt-1 font-mono',
                          isUser ? 'text-white/60' : 'text-muted-foreground/50'
                        )}
                      >
                        {formatTime(msg.created_at)}
                      </p>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
        
        <div ref={messagesEndRef} />
      </div>

      {/* Input Area */}
      <div className="border-t border-border/60 p-4 bg-background/60 backdrop-blur-sm">
        <div className="flex gap-2 items-end">
          <textarea
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Type a message..."
            rows={1}
            className="flex-1 resize-none rounded-xl border border-border/60 bg-background px-4 py-3 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-150"
          />
          <Button
            onClick={handleSend}
            disabled={!inputText.trim() || sending}
            size="icon"
            className="h-11 w-11 rounded-xl"
          >
            {sending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
          </Button>
        </div>
        <p className="text-[10px] text-muted-foreground/40 mt-2 text-center">
          Press Enter to send, Shift+Enter for new line
        </p>
      </div>
    </div>
  );
}
