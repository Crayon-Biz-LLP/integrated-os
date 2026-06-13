'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { decideMergeProposal } from '@/lib/decisions/api';
import type { GraphMergeProposal } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { GitMerge, X, Check } from 'lucide-react';

export function MergePendingList({ items: initialItems }: { items: GraphMergeProposal[] }) {
  const [items, setItems] = useState<GraphMergeProposal[]>(initialItems);

  useEffect(() => {
    setItems(initialItems);
  }, [initialItems]);

  const handleDecision = async (id: number, decision: 'accept' | 'reject') => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      await decideMergeProposal(id, decision);
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
      {items.map((item) => (
        <Card key={item.id}>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold">
                Node Merge Proposal
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap items-center gap-2 mb-3 bg-zinc-900/50 p-3 rounded-md font-mono text-sm">
              <span className="text-yellow-400 font-semibold">{item.label}</span>
              <span className="text-zinc-500">({item.type})</span>
              <span className="text-zinc-500">→</span>
              <span className="text-green-400 font-semibold">{item.merge_candidate_label || 'canonical'}</span>
              <span className="text-zinc-500">({item.type})</span>
            </div>
            <p className="text-sm text-muted-foreground mb-3">
              This node appears to be a duplicate of an existing node. Merge to consolidate or keep both as separate entries.
            </p>
            <div className="flex items-center justify-between mt-4">
              <div className="flex items-center gap-2 text-xs text-muted-foreground/60">
                <span>m{item.id}</span>
                <span>·</span>
                <span>{formatDistanceToNow(parseISO(item.created_at), { addSuffix: true })}</span>
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
      ))}
    </div>
  );
}
