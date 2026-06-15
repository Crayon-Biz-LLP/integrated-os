'use client';

import { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { checkSimilarGraphNodes, decideGraphNode, mergeGraphNodeIntoExisting, searchGraphNodes } from '@/lib/decisions/api';
import type { GraphPendingNode } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, Box, GitMerge, Loader2, Pencil } from 'lucide-react';
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
  const debounceRef = useRef<NodeJS.Timeout | null>(null);

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
  const [editedLabels, setEditedLabels] = useState<Record<number, string>>({});
  const [editingLabelId, setEditingLabelId] = useState<number | null>(null);
  const [similarNodes, setSimilarNodes] = useState<Record<number, any[]>>({});
  const [ignoredSimilar, setIgnoredSimilar] = useState<Record<number, boolean>>({});
  const [detailsExpanded, setDetailsExpanded] = useState<Record<number, boolean>>({});

  useEffect(() => {
    setItems(initialItems);
    
    // Proactive merge check for non-project nodes
    const toCheck = initialItems.filter(i => i.type !== 'project');
    Promise.all(toCheck.map(async (item) => {
      try {
        const matches = await checkSimilarGraphNodes(item.label, item.type);
        if (matches && matches.length > 0) {
          setSimilarNodes(prev => ({ ...prev, [item.id]: matches }));
        }
      } catch (e) {}
    }));
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
    if (decision === 'approve' && editedLabels[id]) {
      payload = { ...payload, label: editedLabels[id] };
    }

    try {
      const result = await decideGraphNode(id, decision, payload);
      if (result.action === 'merge_proposed') {
        toast.warning(result.message || 'Similar node exists. Please use Merge action instead.');
      } else {
        setItems((prev) => prev.filter((i) => i.id !== id));
        toast.success(decision === 'approve' ? 'Node approved' : 'Node rejected');
      }
    } catch (error) {
      console.error('Failed to decide graph node:', error);
      toast.error('Failed to save decision.');
    }
  };

  const handleMerge = async (id: number, targetId: string) => {
    const item = items.find((i) => i.id === id);
    if (!item) return;

    setItems((prev) => prev.filter((i) => i.id !== id));
    setMergingId(null);
    try {
      await mergeGraphNodeIntoExisting(id, targetId);
      toast.success('Merge proposed. Check the Merges tab to finalize.');
    } catch (error) {
      console.error('Failed to merge graph node:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to merge node. Item has been restored.');
    }
  };

  const handleLabelEdit = (id: number, newLabel: string) => {
    setEditedLabels((prev) => ({ ...prev, [id]: newLabel }));
  };

  const saveLabelEdit = (id: number) => {
    setEditingLabelId(null);
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
              <span className="flex items-center gap-2">
                <span className="flex items-center">
                  <Box className="mr-2 h-4 w-4" />
                  {item.type.toUpperCase()} NODE
                </span>
                {item.epistemic_status && (
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-sm font-semibold uppercase tracking-wider ${
                    item.epistemic_status === 'asserted' ? 'bg-emerald-500/20 text-emerald-400' :
                    item.epistemic_status === 'hypothetical' ? 'bg-purple-500/20 text-purple-400' :
                    'bg-zinc-700 text-zinc-300'
                  }`}>
                    {item.epistemic_status}
                  </span>
                )}
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
                    <div className="flex items-center gap-2">
                      {editingLabelId === item.id ? (
                        <Input
                          value={editedLabels[item.id] ?? item.label}
                          onChange={(e) => handleLabelEdit(item.id, e.target.value)}
                          onBlur={() => saveLabelEdit(item.id)}
                          onKeyDown={(e) => { if (e.key === 'Enter') saveLabelEdit(item.id); }}
                          className="h-8 text-lg font-semibold max-w-xs"
                          autoFocus
                        />
                      ) : (
                        <>
                          <h4 className="font-semibold text-lg">
                            {editedLabels[item.id] || item.label}
                          </h4>
                          <button
                            onClick={() => {
                              setEditedLabels((prev) => ({ ...prev, [item.id]: prev[item.id] || item.label }));
                              setEditingLabelId(item.id);
                            }}
                            className="text-muted-foreground hover:text-foreground transition-colors"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                        </>
                      )}
                    </div>
                  <p className="text-xs text-muted-foreground mt-1">Source: {item.source_text}</p>
                  
                  {item.eval_context?.linked_entity && (
                    <p className="text-sm mt-2"><span className="font-semibold text-zinc-400">Linked:</span> {item.eval_context.linked_entity} <span className="text-zinc-500">—{item.eval_context.relationship || 'EVOKES'}→</span> concept</p>
                  )}
                  
                  {item.eval_context && (item.eval_context.justification || item.eval_context.frequency) && (
                    <div className="mt-2 text-sm border-l-2 border-zinc-700 pl-3">
                      <button 
                        onClick={() => setDetailsExpanded(prev => ({ ...prev, [item.id]: !prev[item.id] }))}
                        className="text-xs text-zinc-500 hover:text-zinc-300 flex items-center mb-1"
                      >
                        {detailsExpanded[item.id] ? '▼ Hide details' : '► Show details'}
                      </button>
                      
                      {detailsExpanded[item.id] && (
                        <div className="space-y-2 mt-2 bg-zinc-900/30 p-2 rounded-md">
                          {item.eval_context.justification && (
                            <p><span className="font-semibold text-zinc-400">Why:</span> {item.eval_context.justification}</p>
                          )}
                          {item.type === 'practice' && item.eval_context.frequency && (
                            <p><span className="font-semibold text-zinc-400">Frequency:</span> {item.eval_context.frequency}</p>
                          )}
                        </div>
                      )}
                    </div>
                  )}

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

                  {!ignoredSimilar[item.id] && similarNodes[item.id] && similarNodes[item.id].length > 0 && (
                    <div className="mt-3 bg-yellow-900/20 border border-yellow-700/50 p-3 rounded-md">
                      <p className="text-xs font-medium text-yellow-500 mb-2">⚠️ Similar existing nodes found:</p>
                      <div className="space-y-2">
                        {similarNodes[item.id].map((sim: any) => (
                          <div key={sim.id} className="flex items-center justify-between text-sm">
                            <span className="text-zinc-300">
                              "{sim.label}" <span className="text-xs text-zinc-500">({Math.round(sim.score * 100)}%) {sim.is_pending ? '(pending)' : ''}</span>
                            </span>
                            <Button 
                              size="sm" 
                              variant="outline" 
                              className="h-7 text-xs border-yellow-700/50 hover:bg-yellow-900/40"
                              onClick={() => handleMerge(item.id, sim.id)}
                            >
                              Merge into this
                            </Button>
                          </div>
                        ))}
                      </div>
                      <button 
                        className="text-xs text-zinc-500 hover:text-zinc-300 mt-3 underline decoration-zinc-700 underline-offset-2"
                        onClick={() => setIgnoredSimilar(prev => ({ ...prev, [item.id]: true }))}
                      >
                        Ignore suggestions
                      </button>
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