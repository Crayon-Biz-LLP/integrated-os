'use client';

import { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { decideGraphNode, mergeGraphNodeIntoExisting, searchGraphNodes } from '@/lib/decisions/api';
import type { GraphPendingNode } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, Box, GitMerge, Loader2 } from 'lucide-react';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';

const VALID_ORG_TAGS = ['PERSONAL', 'QHORD', 'SOLVSTRAT', 'ASHRAYA', 'CRAYON'];

function MergeDropdown({ 
  nodeType, 
  onSelect 
}: { 
  nodeType: string; 
  onSelect: (targetId: string) => void 
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<{id: string; label: string}[]>([]);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<NodeJS.Timeout>();

  useEffect(() => {
    if (query.length < 2) {
      setResults([]);
      return;
    }
    
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const data = await searchGraphNodes(query, nodeType);
        setResults(data);
      } catch (e) {
        console.error(e);
      } finally {
        setLoading(false);
      }
    }, 300);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query, nodeType]);

  return (
    <div className="relative w-full max-w-sm mt-3">
      <Input
        placeholder="Type to search existing nodes..."
        value={query}
        onChange={e => setQuery(e.target.value)}
        className="h-8 text-sm pr-8"
      />
      {loading && <Loader2 className="absolute right-2 top-2 h-4 w-4 animate-spin text-muted-foreground" />}
      {results.length > 0 && (
        <div className="absolute top-full left-0 mt-1 w-full bg-popover border rounded-md shadow-md z-10 max-h-48 overflow-y-auto">
          {results.map(r => (
            <button
              key={r.id}
              className="w-full text-left px-3 py-2 text-sm hover:bg-muted focus:bg-muted outline-none"
              onClick={() => onSelect(r.id)}
            >
              {r.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function NodePendingList({ items: initialItems }: { items: GraphPendingNode[] }) {
  const [items, setItems] = useState<GraphPendingNode[]>(initialItems);
  const [projectOrgTags, setProjectOrgTags] = useState<Record<number, string>>({});
  const [mergingId, setMergingId] = useState<number | null>(null);

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

  const handleMerge = async (id: number, targetId: string) => {
    const item = items.find((i) => i.id === id);
    if (!item) return;

    let orgTag: string | undefined = undefined;
    if (item.type === 'project') {
      orgTag = projectOrgTags[id];
      if (!orgTag) {
        toast.error('Please select an Org Tag before merging a project');
        return;
      }
    }

    setItems((prev) => prev.filter((i) => i.id !== id));
    setMergingId(null);
    try {
      await mergeGraphNodeIntoExisting(id, targetId, orgTag);
      toast.success('Merge proposed. Check the Merges tab to finalize.');
    } catch (error) {
      console.error('Failed to merge graph node:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to merge node. Item has been restored.');
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
                  {mergingId === item.id && (
                    <div className="mt-3 bg-muted/50 p-3 rounded-md border">
                      <p className="text-xs font-medium mb-1">Merge into existing node:</p>
                      <MergeDropdown 
                        nodeType={item.type} 
                        onSelect={(targetId) => handleMerge(item.id, targetId)} 
                      />
                      <Button 
                        variant="ghost" 
                        size="sm" 
                        className="mt-2 h-7 text-xs"
                        onClick={() => setMergingId(null)}
                      >
                        Cancel Merge
                      </Button>
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
                {mergingId !== item.id && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="border-indigo-200 bg-indigo-50 text-indigo-600 hover:bg-indigo-100 hover:text-indigo-700"
                    onClick={() => setMergingId(item.id)}
                  >
                    <GitMerge className="h-4 w-4 mr-1" />
                    Merge into...
                  </Button>
                )}
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