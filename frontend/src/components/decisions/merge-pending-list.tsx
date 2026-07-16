'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { decideMergeProposal } from '@/lib/decisions/api';
import type { GraphMergeProposal } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { GitMerge, X, ArrowLeftRight } from 'lucide-react';

export function MergePendingList({ items: initialItems }: { items: GraphMergeProposal[] }) {
  const [items, setItems] = useState<GraphMergeProposal[]>(initialItems);
  const [swappedIds, setSwappedIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    setItems(initialItems);
  }, [initialItems]);

  const toggleSwap = (id: number) => {
    setSwappedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleDecision = async (id: number, decision: 'accept' | 'reject') => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      await decideMergeProposal(id, decision, swappedIds.has(id));
      toast.success(decision === 'accept' ? 'Nodes merged' : 'Merge rejected');
    } catch (error) {
      console.error('Failed to decide merge proposal:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to save decision. Item has been restored.');
    }
  };

  if (items.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        <GitMerge className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
        No pending merge proposals.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {items.map((item) => {
        const isSwapped = swappedIds.has(item.id);
        const sourceLabel = isSwapped ? (item.merge_candidate_label || item.target_label) : item.source_label;
        const targetLabel = isSwapped ? item.source_label : (item.merge_candidate_label || item.target_label);

        return (
          <Card key={item.id}>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base font-semibold">
                  Node Merge Proposal
                </CardTitle>
                <Button 
                  size="sm" 
                  variant="outline" 
                  className="h-7 px-2 text-xs" 
                  onClick={() => toggleSwap(item.id)}
                >
                  <ArrowLeftRight className="h-3 w-3 mr-1" />
                  Swap Direction
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap items-center gap-2 mb-3 bg-zinc-900/50 p-3 rounded-md font-mono text-sm">
                <span className="text-yellow-400 font-semibold">{sourceLabel}</span>
                <span className="text-zinc-500">({item.source_type})</span>
                <span className="text-zinc-500">→</span>
                <span className="text-green-400 font-semibold">{targetLabel}</span>
              </div>
              <p className="text-sm text-muted-foreground mb-3">
                The node on the left will be merged into the node on the right. The left node will act as an alias.
              </p>
              <div className="flex items-center justify-between mt-4">
                <div className="flex items-center gap-2 text-xs text-muted-foreground/60">
                  <span>m{item.id}</span>
                  <span>·</span>
                  <span>{formatDistanceToNow(parseISO(item.proposed_at), { addSuffix: true })}</span>
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    className="bg-green-600 hover:bg-green-700"
                    onClick={() => handleDecision(item.id, 'accept')}
                  >
                    <GitMerge className="h-4 w-4 mr-1" />
                    Merge
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-red-400 hover:bg-red-500/20"
                    onClick={() => handleDecision(item.id, 'reject')}
                  >
                    <X className="h-4 w-4 mr-1" />
                    Keep Both
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
