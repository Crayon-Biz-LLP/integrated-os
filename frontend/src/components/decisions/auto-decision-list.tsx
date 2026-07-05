'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { verifyAutoDecision, rejectAutoDecision } from '@/lib/decisions/api';
import type { AutoDecisionItem } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, Bot, RefreshCw, Calendar, TrendingDown, GitBranch, Network, Brain } from 'lucide-react';

const DECISION_TYPE_CONFIG: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  channel_approval: { label: 'Auto-Approved', icon: <Check className="h-3.5 w-3.5" />, color: 'bg-green-500/10 text-green-500' },
  graph_node_approval: { label: 'Node Approved', icon: <Network className="h-3.5 w-3.5" />, color: 'bg-blue-500/10 text-blue-500' },
  graph_edge_approval: { label: 'Edge Approved', icon: <GitBranch className="h-3.5 w-3.5" />, color: 'bg-purple-500/10 text-purple-500' },
  concept_auto_creation: { label: 'Concept Created', icon: <Brain className="h-3.5 w-3.5" />, color: 'bg-indigo-500/10 text-indigo-500' },
  edge_auto_creation: { label: 'Edge Created', icon: <GitBranch className="h-3.5 w-3.5" />, color: 'bg-violet-500/10 text-violet-500' },
  task_auto_expiry: { label: 'Task Expired', icon: <Calendar className="h-3.5 w-3.5" />, color: 'bg-amber-500/10 text-amber-500' },
  priority_decay: { label: 'Priority Decay', icon: <TrendingDown className="h-3.5 w-3.5" />, color: 'bg-orange-500/10 text-orange-500' },
};

function getTypeConfig(decisionType: string) {
  return DECISION_TYPE_CONFIG[decisionType] || {
    label: decisionType.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase()),
    icon: <Bot className="h-3.5 w-3.5" />,
    color: 'bg-gray-500/10 text-gray-500',
  };
}

function DecisionBadge({ decisionType }: { decisionType: string }) {
  const config = getTypeConfig(decisionType);
  return (
    <Badge variant="outline" className={`text-xs flex items-center gap-1 ${config.color}`}>
      {config.icon}
      {config.label}
    </Badge>
  );
}

export function AutoDecisionList({ initialItems }: { initialItems: AutoDecisionItem[] }) {
  const [items, setItems] = useState<AutoDecisionItem[]>(initialItems);

  useEffect(() => {
    setItems(initialItems);
  }, [initialItems]);

  const handleVerify = useCallback(async (id: number) => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      const success = await verifyAutoDecision(id);
      if (!success) {
        if (item) setItems((prev) => [...prev, item]);
        toast.error('Failed to verify. Item restored.');
      } else {
        toast.success('Verified — Rhodey learns this was correct.');
      }
    } catch (error) {
      console.error('Failed to verify auto-decision:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to verify. Item restored.');
    }
  }, [items]);

  const handleReject = useCallback(async (id: number) => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      const success = await rejectAutoDecision(id);
      if (!success) {
        if (item) setItems((prev) => [...prev, item]);
        toast.error('Failed to reject. Item restored.');
      } else {
        toast.warning('Rejected — Rhodey learns this was wrong.');
      }
    } catch (error) {
      console.error('Failed to reject auto-decision:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to reject. Item restored.');
    }
  }, [items]);

  if (items.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        <Bot className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
        No auto-decisions to review. Once Rhodey starts making autonomous decisions, they'll appear here.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {items.map((item) => (
        <Card key={item.id}>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold">{item.title}</CardTitle>
              <div className="flex items-center gap-2">
                <DecisionBadge decisionType={item.decision_type} />
                <Badge variant="outline" className="text-xs">
                  {Math.round(item.confidence * 100)}%
                </Badge>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {item.context && (
              <p className="text-sm text-muted-foreground mb-3 line-clamp-2">{item.context}</p>
            )}
            <div className="flex items-center gap-2 text-xs text-muted-foreground/60 mb-3 flex-wrap">
              <span>#{item.id}</span>
              <span>·</span>
              <span>Source: {item.source}</span>
              <span>·</span>
              <span>{formatDistanceToNow(parseISO(item.decided_at), { addSuffix: true })}</span>
              {item.expires_at && (
                <>
                  <span>·</span>
                  <span>Expires {formatDistanceToNow(parseISO(item.expires_at), { addSuffix: true })}</span>
                </>
              )}
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                className="bg-green-600 hover:bg-green-700"
                onClick={() => handleVerify(item.id)}
              >
                <Check className="h-4 w-4 mr-1" />
                Verify
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-red-400 hover:bg-red-500/20"
                onClick={() => handleReject(item.id)}
              >
                <X className="h-4 w-4 mr-1" />
                Reject
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
