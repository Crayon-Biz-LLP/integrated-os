'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { decideWhatsAppMessage } from '@/lib/decisions/api';
import type { WhatsAppPendingMessage } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, MessageSquare, User } from 'lucide-react';

export function WhatsAppPendingList({ items: initialItems }: { items: WhatsAppPendingMessage[] }) {
  const [items, setItems] = useState<WhatsAppPendingMessage[]>(initialItems);

  useEffect(() => {
    setItems(initialItems);
  }, [initialItems]);

  const handleDecision = async (id: number, decision: 'approve' | 'reject') => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      await decideWhatsAppMessage(id, decision);
    } catch (error) {
      console.error('Failed to decide WhatsApp message:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to save decision. Item has been restored.');
    }
  };

  if (items.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        <MessageSquare className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
        No pending WhatsApp messages. You're all caught up.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {items.map((item) => (
        <Card key={item.id}>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold">
                {item.suggested_title || item.message_text}
              </CardTitle>
              <div className="flex items-center gap-2">
                {item.suggested_project && (
                  <Badge variant="outline" className="text-xs">{item.suggested_project}</Badge>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="flex items-center gap-2 text-sm text-muted-foreground mb-2">
              <User className="h-3.5 w-3.5" />
              <span>{item.sender_name}</span>
              {item.sender_phone && item.sender_phone !== item.sender_name && (
                <span className="text-muted-foreground/60 text-xs">{item.sender_phone}</span>
              )}
            </div>
            {item.summary && (
              <p className="text-sm text-muted-foreground mb-2 italic">"{item.summary}"</p>
            )}
            <div className="flex items-center gap-2 text-xs text-muted-foreground/60 mb-3">
              <span>w{item.id}</span>
              <span>·</span>
              <span>{formatDistanceToNow(parseISO(item.created_at), { addSuffix: true })}</span>
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                className="bg-green-600 hover:bg-green-700"
                onClick={() => handleDecision(item.id, 'approve')}
              >
                <Check className="h-4 w-4 mr-1" />
                Approve
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-red-400 hover:bg-red-500/20"
                onClick={() => handleDecision(item.id, 'reject')}
              >
                <X className="h-4 w-4 mr-1" />
                Drop
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
