'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { decideGraphNode } from '@/lib/decisions/api';
import type { GraphPendingNode } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, Box } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

const VALID_ORG_TAGS = ['PERSONAL', 'QHORD', 'SOLVSTRAT', 'ASHRAYA', 'CRAYON'];

export function NodePendingList({ items: initialItems }: { items: GraphPendingNode[] }) {
  const [items, setItems] = useState<GraphPendingNode[]>(initialItems);
  const [projectOrgTags, setProjectOrgTags] = useState<Record<number, string>>({});

  useEffect(() => {
    setItems(initialItems);
  }, [initialItems]);

  const handleDecision = async (id: number, decision: 'approve' | 'reject') => {
    const item = items.find((i) => i.id === id);
    if (!item) return;

    let payload: any = undefined;
    if (decision === 'approve' && item.type === 'project') {
      const orgTag = projectOrgTags[id];
      if (!orgTag) {
        toast.error('Please select an Org Tag for the project');
        return;
      }
      payload = { org_tag: orgTag };
    }

    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      await decideGraphNode(id, decision, payload);
      toast.success(decision === 'approve' ? 'Node approved' : 'Node rejected');
    } catch (error) {
      console.error('Failed to decide graph node:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to save decision. Item has been restored.');
    }
  };

  if (items.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        <Box className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
        No pending graph nodes.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {items.map((item) => (
        <Card key={item.id} className="overflow-hidden">
          <CardHeader className="bg-muted/30 py-3 px-4 border-b">
            <CardTitle className="text-sm font-medium flex justify-between items-center text-muted-foreground">
              <span className="flex items-center">
                <Box className="mr-2 h-4 w-4" />
                {item.type.toUpperCase()} NODE
              </span>
              <span className="text-xs font-normal">
                {formatDistanceToNow(parseISO(item.created_at), { addSuffix: true })}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="p-4">
            <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
              <div className="space-y-3 flex-grow">
                <div>
                  <h4 className="font-semibold text-lg">{item.label}</h4>
                  <p className="text-xs text-muted-foreground mt-1">Source: {item.source_text}</p>
                  {item.status === 'flagged' && (
                    <span className="inline-block mt-2 px-2 py-1 bg-yellow-100 text-yellow-800 text-xs font-medium rounded">
                      Flagged: Requires structural anchor
                    </span>
                  )}
                  {item.type === 'project' && (
                    <div className="mt-4 flex items-center gap-3">
                      <span className="text-sm text-muted-foreground whitespace-nowrap">Org Tag:</span>
                      <Select
                        value={projectOrgTags[item.id] || ''}
                        onValueChange={(val) => setProjectOrgTags((prev) => ({ ...prev, [item.id]: val || '' }))}
                      >
                        <SelectTrigger className="w-[180px] h-8 text-sm">
                          <SelectValue placeholder="Select Org Tag" />
                        </SelectTrigger>
                        <SelectContent>
                          {VALID_ORG_TAGS.map((tag) => (
                            <SelectItem key={tag} value={tag} className="text-sm">{tag}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  )}
                </div>
              </div>
              <div className="flex items-start gap-2 sm:ml-auto">
                <Button
                  size="sm"
                  variant="outline"
                  className="bg-red-50 text-red-600 hover:bg-red-100 hover:text-red-700 border-red-200"
                  onClick={() => handleDecision(item.id, 'reject')}
                >
                  <X className="h-4 w-4 mr-1" />
                  Reject
                </Button>
                <Button
                  size="sm"
                  variant="default"
                  className="bg-emerald-600 hover:bg-emerald-700 text-white"
                  onClick={() => handleDecision(item.id, 'approve')}
                >
                  <Check className="h-4 w-4 mr-1" />
                  Approve
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}