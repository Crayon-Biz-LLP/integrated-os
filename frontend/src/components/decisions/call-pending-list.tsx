'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { decideCallItem } from '@/lib/decisions/api';
import type { CallPendingItem } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, Mic, FileText, Lightbulb } from 'lucide-react';

export function CallPendingList({ items: initialItems }: { items: CallPendingItem[] }) {
  const [items, setItems] = useState<CallPendingItem[]>(initialItems);

  useEffect(() => {
    setItems(initialItems);
  }, [initialItems]);

  const handleDecision = async (id: number, decision: 'approve' | 'reject') => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      await decideCallItem(id, decision);
    } catch (error) {
      console.error('Failed to decide call item:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to save decision. Item has been restored.');
    }
  };

  const actionTypeIcon = (type: string) => {
    switch (type) {
      case 'task': return <Mic className="h-3.5 w-3.5" />;
      case 'decision': return <Lightbulb className="h-3.5 w-3.5" />;
      default: return <FileText className="h-3.5 w-3.5" />;
    }
  };

  const actionTypeLabel = (type: string) => {
    switch (type) {
      case 'task': return 'Task';
      case 'decision': return 'Decision';
      default: return 'Note';
    }
  };

  if (items.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        <Mic className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
        No pending call items. You're all caught up.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {items.map((item) => (
        <Card key={item.id}>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold">{item.suggested_title}</CardTitle>
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="text-xs flex items-center gap-1">
                  {actionTypeIcon(item.action_type)}
                  {actionTypeLabel(item.action_type)}
                </Badge>
                {item.suggested_project && (
                  <Badge variant="outline" className="text-xs">{item.suggested_project}</Badge>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {item.summary && (
              <p className="text-sm text-muted-foreground mb-3 line-clamp-2">{item.summary}</p>
            )}
            <div className="flex items-center gap-2 text-xs text-muted-foreground/60 mb-3">
              <span>c{item.id}</span>
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
