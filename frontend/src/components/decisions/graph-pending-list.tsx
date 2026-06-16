'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { checkSimilarGraphEdges, decideGraphEdge } from '@/lib/decisions/api';
import type { GraphPendingEdge } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, Network, Pencil, Save, XCircle } from 'lucide-react';
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from '@/components/ui/select';

const RELATIONSHIP_OPTIONS = [
  { value: 'MET_WITH', group: 'Person → Person', desc: 'In-person meetings' },
  { value: 'SPOUSE_OF', group: 'Person → Person', desc: 'Marriage' },
  { value: 'FAMILY_OF', group: 'Person → Person', desc: 'Extended family' },
  { value: 'FRIEND_OF', group: 'Person → Person', desc: 'Personal friendships' },
  { value: 'WORKS_AT', group: 'Person → Organization', desc: 'Employment' },
  { value: 'CLIENT_OF', group: 'Person → Organization', desc: 'Client relationship' },
  { value: 'VENDOR_TO', group: 'Person → Organization', desc: 'Vendor relationship' },
  { value: 'WORKS_ON', group: 'Person → Project', desc: 'Project involvement' },
  { value: 'LEADS', group: 'Person → Project', desc: 'Leadership' },
  { value: 'ATTENDED', group: 'Person → Event', desc: 'Attended an event' },
  { value: 'INVOLVES', group: 'Person ↔ Event', desc: 'Mutual involvement' },
  { value: 'ASSOCIATED_WITH', group: 'Person → Concept', desc: 'Associated with a concept' },
  { value: 'PART_OF', group: 'Event → Project', desc: 'Event is part of project' },
  { value: 'INVOLVES', group: 'Event → Person', desc: 'Event involves a person' },
  { value: 'EVOKES', group: 'Event → Concept', desc: 'Evokes a concept' },
  { value: 'BELONGS_TO', group: 'Task → Project', desc: 'Task belongs to project' },
  { value: 'BLOCKS', group: 'Task → Task', desc: 'Task blocks another' },
  { value: 'DEPENDS_ON', group: 'Task → Task', desc: 'Task depends on another' },
  { value: 'RELATES_TO', group: 'Task → Concept', desc: 'Task relates to a concept' },
  { value: 'DEPENDS_ON', group: 'Project → Project', desc: 'Project depends on another' },
  { value: 'EVOKES', group: 'Project → Concept', desc: 'Project evokes a concept' },
  { value: 'RELATES_TO', group: 'Project → Concept', desc: 'Project relates to a concept' },
  { value: 'ASSOCIATED_WITH', group: 'Organization → Concept', desc: 'Associated with a concept' },
  { value: 'MENTIONS', group: 'Memory → Person', desc: 'Memory mentions a person' },
  { value: 'MENTIONS', group: 'Memory → Project', desc: 'Memory mentions a project' },
  { value: 'MENTIONS', group: 'Memory → Organization', desc: 'Memory mentions an organization' },
  { value: 'MENTIONS', group: 'Memory → Event', desc: 'Memory mentions an event' },
  { value: 'EVOKES', group: 'Memory → Concept', desc: 'Memory evokes a concept' },
];

const REL_GROUPS = [...new Set(RELATIONSHIP_OPTIONS.map(o => o.group))];

export function GraphPendingList({ items: initialItems }: { items: GraphPendingEdge[] }) {
  const [items, setItems] = useState<GraphPendingEdge[]>(initialItems);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editForm, setEditForm] = useState({ source: '', target: '', rel: '' });
  const [contextOpen, setContextOpen] = useState<number | null>(null);
  const [contextText, setContextText] = useState('');
  const [similarEdges, setSimilarEdges] = useState<Record<number, any[]>>({});
  const [detailsExpanded, setDetailsExpanded] = useState<Record<number, boolean>>({});

  useEffect(() => {
    setItems(initialItems);
    
    Promise.all(initialItems.map(async (item) => {
      try {
        const matches = await checkSimilarGraphEdges(item.source_label, item.target_label, item.relationship);
        if (matches && matches.length > 0) {
          // exclude self
          const realMatches = matches.filter((m: any) => !(m.is_pending && m.id === item.id));
          if (realMatches.length > 0) {
            setSimilarEdges(prev => ({ ...prev, [item.id]: realMatches }));
          }
        }
      } catch (e) {}
    }));
  }, [initialItems]);

  const handleDecision = async (id: number, decision: 'approve' | 'reject') => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    setContextOpen((prev) => prev === id ? null : prev);
    setContextText('');
    try {
      const contextPayload = decision === 'approve' && contextOpen === id && contextText.trim()
        ? { new_context: contextText.trim() }
        : undefined;
      await decideGraphEdge(id, decision, contextPayload);
      toast.success(decision === 'approve' ? 'Edge approved' : 'Edge rejected');
    } catch (error) {
      console.error('Failed to decide graph edge:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to save decision. Item has been restored.');
    }
  };

  const handleSaveEdit = async (id: number) => {
    const item = items.find((i) => i.id === id);
    setItems((prev) => prev.filter((i) => i.id !== id));
    try {
      await decideGraphEdge(id, 'approve', {
        new_source: editForm.source,
        new_target: editForm.target,
        new_rel: editForm.rel,
      });
      setEditingId(null);
      toast.success('Edge edited and approved');
    } catch (error) {
      console.error('Failed to edit graph edge:', error);
      if (item) setItems((prev) => [...prev, item]);
      toast.error('Failed to save edit. Item has been restored.');
    }
  };

  const startEdit = (item: GraphPendingEdge) => {
    setEditingId(item.id);
    setContextOpen(null);
    setContextText('');
    setEditForm({
      source: item.source_label,
      target: item.target_label,
      rel: item.relationship,
    });
  };

  if (items.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        <Network className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
        No pending graph edges.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {items.map((item) => (
        <Card key={item.id}>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold flex items-center gap-2">
                Pending Edge Validation
                {item.epistemic_status && (
                  <span className={`text-[10px] px-1.5 py-0.5 rounded-sm font-semibold uppercase tracking-wider ${
                    item.epistemic_status === 'asserted' ? 'bg-emerald-500/20 text-emerald-400' :
                    item.epistemic_status === 'hypothetical' ? 'bg-purple-500/20 text-purple-400' :
                    'bg-zinc-700 text-zinc-300'
                  }`}>
                    {item.epistemic_status}
                  </span>
                )}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            {editingId === item.id ? (
              <div className="space-y-3 mb-4">
                <div className="grid grid-cols-3 gap-2">
                  <div className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">Source</label>
                    <Input
                      value={editForm.source}
                      onChange={(e) => setEditForm(prev => ({ ...prev, source: e.target.value }))}
                      className="h-8 text-sm"
                    />
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">Relationship</label>
                    <Select value={editForm.rel} onValueChange={(v) => setEditForm(prev => ({ ...prev, rel: v ?? '' }))}>
                      <SelectTrigger className="w-full h-8 text-sm">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {REL_GROUPS.map(group => (
                          <SelectGroup key={group}>
                            <SelectLabel>{group}</SelectLabel>
                            {RELATIONSHIP_OPTIONS.filter(o => o.group === group).map(opt => (
                              <SelectItem key={opt.value} value={opt.value}>
                                {opt.value}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">Target</label>
                    <Input
                      value={editForm.target}
                      onChange={(e) => setEditForm(prev => ({ ...prev, target: e.target.value }))}
                      className="h-8 text-sm"
                    />
                  </div>
                </div>
                <div className="flex gap-2 justify-end">
                  <Button size="sm" variant="outline" onClick={() => setEditingId(null)}>
                    <XCircle className="h-4 w-4 mr-1" /> Cancel
                  </Button>
                  <Button size="sm" className="bg-green-600 hover:bg-green-700" onClick={() => handleSaveEdit(item.id)}>
                    <Save className="h-4 w-4 mr-1" /> Save & Approve
                  </Button>
                </div>
              </div>
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2 mb-3 bg-zinc-900/50 p-3 rounded-md font-mono text-sm">
                  <span className="text-blue-400 font-semibold">{item.source_label}</span>
                  <span className="text-zinc-500">→</span>
                  <span className="text-amber-400 font-bold">{item.relationship}</span>
                  <span className="text-zinc-500">→</span>
                  <span className="text-cyan-400 font-semibold">{item.target_label}</span>
                </div>
                
                {item.eval_context && item.eval_context.justification && (
                  <p className="text-sm text-zinc-300 mb-3 bg-zinc-900/30 p-2 rounded border-l-2 border-amber-500/50">
                    <span className="font-semibold text-zinc-500 mr-1">Why:</span>
                    {item.eval_context.justification}
                  </p>
                )}
                
                {item.source_text && (
                  <div className="mb-3">
                    {detailsExpanded[item.id] ? (
                      <div className="relative">
                        <p className="text-sm text-muted-foreground italic border-l-2 border-zinc-700 pl-2 whitespace-pre-wrap">
                          {item.source_text}
                        </p>
                        <button 
                          onClick={() => setDetailsExpanded(prev => ({ ...prev, [item.id]: false }))}
                          className="text-xs text-zinc-500 hover:text-zinc-300 mt-1"
                        >
                          Show less
                        </button>
                      </div>
                    ) : (
                      <p className="text-sm text-muted-foreground italic border-l-2 border-zinc-700 pl-2">
                        {item.source_text.length > 200 ? (
                          <>
                            {item.source_text.slice(0, 200)}...
                            <button 
                              onClick={() => setDetailsExpanded(prev => ({ ...prev, [item.id]: true }))}
                              className="text-xs text-blue-400 hover:underline ml-1"
                            >
                              Show more
                            </button>
                          </>
                        ) : item.source_text}
                      </p>
                    )}
                  </div>
                )}
                
                {similarEdges[item.id] && similarEdges[item.id].length > 0 && (
                  <div className="mb-3 bg-red-900/20 border border-red-700/50 p-2 rounded-md">
                    <p className="text-xs font-medium text-red-400">
                      ⚠️ Duplicate edge detected (already exists in {similarEdges[item.id][0].is_pending ? 'pending' : 'graph_edges'}).
                    </p>
                  </div>
                )}
                
                {contextOpen === item.id ? (
                  <div className="mb-3">
                    <textarea
                      className="w-full h-16 rounded-md border border-zinc-700 bg-zinc-900/50 p-2 text-xs text-zinc-300 resize-none placeholder:text-zinc-600"
                      placeholder="Why are you approving this? (optional)"
                      value={contextText}
                      onChange={(e) => setContextText(e.target.value)}
                    />
                    <button
                      className="text-xs text-zinc-500 hover:text-zinc-300 mt-1"
                      onClick={() => { setContextOpen(null); setContextText(''); }}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    className="text-xs text-zinc-500 hover:text-zinc-300 mb-3"
                    onClick={() => setContextOpen(item.id)}
                  >
                    + Add context
                  </button>
                )}
                <div className="flex items-center justify-between mt-4">
                  <div className="flex items-center gap-2 text-xs text-muted-foreground/60">
                    <span>pe{item.id}</span>
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
                      variant="outline"
                      onClick={() => startEdit(item)}
                    >
                      <Pencil className="h-4 w-4 mr-1" />
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-red-400 hover:bg-red-500/20"
                      onClick={() => handleDecision(item.id, 'reject')}
                    >
                      <X className="h-4 w-4 mr-1" />
                      Reject
                    </Button>
                  </div>
                </div>
              </>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
