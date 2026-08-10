'use client';

import React, { useState, useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { renamePendingGraphNode, deletePendingGraphNode, mergeGraphNodeIntoExisting, searchGraphNodes, fetchLiveGraphNodes, decideGraphNode, batchDecideGraphNodes, changePendingGraphNodeType, submitClarification, updateGraphNodeEnrichment, fetchAliasesForEntity, createEntityAlias, deleteEntityAlias, fetchEntityTasks, type EnrichmentUpdates, type EntityAlias } from '@/lib/decisions/api';
import type { GraphPendingNode } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Loader2, Trash2, Pencil, GitMerge, Check, X, Settings2, Save, Tag, Plus, Search } from 'lucide-react';
import { Textarea } from "@/components/ui/textarea";
import { errMsg } from "@/lib/errors";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

function MergeSearchInput({ 
  nodeType, 
  scope,
  onSelect 
}: { 
  nodeType: string; 
  scope: string;
  onSelect: (targetId: string, targetLabel: string) => void 
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<{id: string; label: string}[]>([]);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (query.length < 2) return;
    
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const data = await searchGraphNodes(query, nodeType, scope);
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
  }, [query, nodeType, scope]);

  return (
    <div className="relative w-full max-w-sm">
      <Input
        placeholder="Type to search existing nodes..."
        value={query}
        onChange={e => setQuery(e.target.value)}
        className="h-8 text-sm pr-8"
      />
      {loading && <Loader2 className="absolute right-2 top-2 h-4 w-4 animate-spin text-muted-foreground" />}
      {query.length >= 2 && results.length > 0 && (
        <div className="absolute top-full left-0 mt-1 w-full bg-popover border rounded-md shadow-md z-50 max-h-48 overflow-y-auto">
          {results.map(r => (
            <button
              key={r.id}
              className="w-full text-left px-3 py-2 text-sm hover:bg-muted focus:bg-muted outline-none"
              onClick={() => onSelect(r.id, r.label)}
            >
              {r.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function EntityTableList({ items: initialItems, rejectedItems = [], defaultScope = 'pending', showPendingScope = true }: { items: GraphPendingNode[], rejectedItems?: GraphPendingNode[], defaultScope?: 'pending' | 'live' | 'rejected', showPendingScope?: boolean }) {
  const [items, setItems] = useState<GraphPendingNode[]>(initialItems);
  const [scope, setScope] = useState<'pending' | 'live' | 'rejected'>(defaultScope);
  const [loading, setLoading] = useState(defaultScope === 'live');
  const [prevScope, setPrevScope] = useState(scope);
  const [prevInitial, setPrevInitial] = useState(initialItems);
  const [prevRejected, setPrevRejected] = useState(rejectedItems);
  if (prevScope !== scope || prevInitial !== initialItems || prevRejected !== rejectedItems) {
    setPrevScope(scope);
    setPrevInitial(initialItems);
    setPrevRejected(rejectedItems);
    if (scope === 'pending') setItems(initialItems);
    else if (scope === 'rejected') setItems(rejectedItems);
  }
  const [filterType, setFilterType] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState('');
  
  const [editingId, setEditingId] = useState<number | string | null>(null);
  const [editLabel, setEditLabel] = useState("");
  
  const [changingTypeId, setChangingTypeId] = useState<number | string | null>(null);
  
  const [mergingId, setMergingId] = useState<number | string | null>(null);
  
  const [enrichId, setEnrichId] = useState<number | string | null>(null);
  const [enrichDraft, setEnrichDraft] = useState<EnrichmentUpdates>({});
  const [enrichSaving, setEnrichSaving] = useState(false);
  const [enrichAliases, setEnrichAliases] = useState<EntityAlias[]>([]);
  const [enrichAliasesLoading, setEnrichAliasesLoading] = useState(false);
  const [enrichNewAlias, setEnrichNewAlias] = useState('');
  const [enrichAddingAlias, setEnrichAddingAlias] = useState(false);
  const [enrichTasks, setEnrichTasks] = useState<Array<{ id: number; title: string; status: string; priority: string; organization_name?: string | null }>>([]);
  const [enrichTasksLoading, setEnrichTasksLoading] = useState(false);
  
  const [deleteId, setDeleteId] = useState<number | string | null>(null);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");
  const [clarificationAnswers, setClarificationAnswers] = useState<Record<number, string>>({});

  useEffect(() => {
    if (scope === 'live') {
      const run = async () => {
        setLoading(true);
        try {
          const data = await fetchLiveGraphNodes();
          setItems(data);
        } catch (e) {
          console.error(e);
          toast.error(e instanceof Error ? e.message : "Failed to load live nodes");
          setItems([]);
        } finally {
          setLoading(false);
        }
      };
      void run();
    }
  }, [scope]);

  const handleDecision = async (id: number | string, decision: 'approve' | 'reject' | 'unreject') => {
    try {
      const result = await decideGraphNode(id as number, decision);
      if (result.action === 'merge_proposed') {
        toast.warning(result.message || 'Similar node exists. Please use Merge action instead.');
        // Don't remove from list, let them merge it
      } else {
        setItems(prev => prev.filter(i => i.id !== id));
        toast.success(decision === 'approve' ? 'Approved successfully' : decision === 'unreject' ? 'Un-rejected successfully' : 'Rejected successfully');
      }
    } catch (e) {
      toast.error(errMsg(e, `Failed to ${decision}`));
    }
  };

  const handleClarification = async (item: GraphPendingNode, answerText: string) => {
    if (!item.clarification) return;
    const previousItems = [...items];
    setItems((prev) => prev.filter((i) => i.id !== item.id));
    try {
      await submitClarification(item.clarification.shortcode, answerText);
      toast.success('Clarification submitted');
    } catch (err) {
      console.error(err);
      setItems(previousItems);
      toast.error('Failed to submit clarification');
    }
  };

  const handleRename = async (id: number | string) => {
    if (!editLabel.trim()) return;
    
    try {
      await renamePendingGraphNode(id, editLabel, scope);
      setItems(prev => prev.map(i => i.id === id ? { ...i, label: editLabel } : i));
      setEditingId(null);
      toast.success("Renamed successfully");
    } catch (e) {
      toast.error(errMsg(e, "Failed to rename"));
    }
  };

  const handleChangeType = async (id: number | string, newType: string) => {
    try {
      await changePendingGraphNodeType(id, newType, scope);
      setItems(prev => prev.map(i => i.id === id ? { ...i, type: newType } : i));
      setChangingTypeId(null);
      toast.success("Changed type successfully");
    } catch (e) {
      toast.error(errMsg(e, "Failed to change type"));
    }
  };

  const handleMerge = async (sourceId: number | string, targetId: string, targetLabel: string) => {
    try {
      await mergeGraphNodeIntoExisting(sourceId, targetId, scope);
      setItems(prev => prev.filter(i => i.id !== sourceId));
      setMergingId(null);
      toast.success(`Merged into ${targetLabel}`);
    } catch (e) {
      toast.error(errMsg(e, "Failed to merge"));
    }
  };

  const handleDelete = async () => {
    if (!deleteId) return;
    try {
      const res = await deletePendingGraphNode(deleteId, scope);
      setItems(prev => prev.filter(i => i.id !== deleteId));
      setDeleteId(null);
      setDeleteConfirmText("");
      toast.success(res.message || "Deleted successfully");
    } catch (e) {
      toast.error(errMsg(e, "Failed to delete"));
    }
  };

  // ── Enrichment editing (live scope) ────────────────────────────

  const openEnrichmentEditor = (item: GraphPendingNode) => {
    const e = item.metadata?.enrichment || {};
    setEnrichDraft({
      role: e.role ?? null,
      strategic_weight: typeof e.strategic_weight === 'number' ? e.strategic_weight : (e.strategic_weight != null ? Number(e.strategic_weight) : null),
      is_active: e.is_active ?? true,
      org_type: e.org_type ?? null,
      description: e.description ?? null,
      organization_name: e.organization_name ?? null,
    });
    setEnrichAliases([]);
    setEnrichNewAlias('');
    setEnrichTasks([]);
    setEnrichId(item.id);

    // Person nodes also carry aliases + active tasks (People tab consolidated here).
    if (item.type === 'person' && item.label) {
      setEnrichAliasesLoading(true);
      fetchAliasesForEntity(item.label)
        .then(setEnrichAliases)
        .catch(() => toast.error('Failed to load aliases'))
        .finally(() => setEnrichAliasesLoading(false));

      setEnrichTasksLoading(true);
      fetchEntityTasks(item.id, item.label)
        .then(setEnrichTasks)
        .catch(() => toast.error('Failed to load tasks'))
        .finally(() => setEnrichTasksLoading(false));
    }
  };

  const addAlias = async (canonicalName: string) => {
    const alias = enrichNewAlias.trim();
    if (!alias || enrichAddingAlias) return;
    setEnrichAddingAlias(true);
    try {
      const result = await createEntityAlias(alias, canonicalName);
      const newAlias = result.alias;
      if (result.success && newAlias) {
        setEnrichAliases(prev => [...prev, newAlias]);
        setEnrichNewAlias('');
        toast.success('Alias added');
      } else {
        toast.error(result.message || 'Failed to add alias');
      }
    } catch (e) {
      toast.error(errMsg(e, 'Failed to add alias'));
    } finally {
      setEnrichAddingAlias(false);
    }
  };

  const removeAlias = async (alias: string, canonicalName: string) => {
    try {
      const result = await deleteEntityAlias(alias, canonicalName);
      if (result.success) {
        setEnrichAliases(prev => prev.filter(a => a.alias !== alias));
        toast.success('Alias removed');
      } else {
        toast.error(result.message || 'Failed to remove alias');
      }
    } catch (e) {
      toast.error(errMsg(e, 'Failed to remove alias'));
    }
  };

  const saveEnrichment = async () => {
    if (enrichId === null) return;
    setEnrichSaving(true);
    try {
      const result = await updateGraphNodeEnrichment(enrichId, enrichDraft);
      // Merge the returned enrichment back into local state so the row refreshes.
      setItems(prev => prev.map(i => {
        if (i.id !== enrichId) return i;
        const meta = { ...(i.metadata || {}) };
        meta.enrichment = { ...(meta.enrichment || {}), ...(result.enrichment || {}) };
        return { ...i, metadata: meta };
      }));
      setEnrichId(null);
      setEnrichDraft({});
      toast.success(result.message || "Details updated");
    } catch (e) {
      toast.error(errMsg(e, "Failed to update details"));
    } finally {
      setEnrichSaving(false);
    }
  };

  const [batchProcessing, setBatchProcessing] = useState(false);

  const handleBatch = async (decision: 'approve' | 'reject') => {
    setBatchProcessing(true);
    try {
      const batchIds = items.filter(i => !i.clarification).map(i => i.id as number);
      const skipped = items.length - batchIds.length;
      if (batchIds.length === 0) {
        toast.info('All items are in clarification — no batch action taken.');
        setBatchProcessing(false);
        return;
      }
      const result = await batchDecideGraphNodes(batchIds, decision);
      setItems([]);
      if (result.failed > 0) {
        toast.error(`${decision === 'approve' ? 'Approved' : 'Rejected'} ${result.processed}, ${result.failed} failed${skipped ? `, ${skipped} skipped` : ''}`);
      } else {
        toast.success(`${decision === 'approve' ? 'Approved' : 'Rejected'} ${result.processed} items${skipped ? `, ${skipped} skipped` : ''}`);
      }
    } catch {
      toast.error('Batch operation failed. Refetch the list and try again.');
    }
    setBatchProcessing(false);
  };

  const typeFiltered = items.filter(item => {
    if (filterType === 'all') return true;
    if (filterType === 'other') {
      return !['person', 'organization', 'concept'].includes(item.type);
    }
    return item.type === filterType;
  });

  const filteredItems = typeFiltered
    .filter(item => {
      const q = searchQuery.trim().toLowerCase();
      if (!q) return true;
      const label = (item.label || '').toLowerCase();
      if (label.includes(q)) return true;
      // Also match enrichment fields (role, org, description) for people/orgs.
      const e = item.metadata?.enrichment || {};
      return [e.role, e.org_type, e.organization_name, e.description]
        .some(v => typeof v === 'string' && v.toLowerCase().includes(q));
    })
    .sort((a, b) => a.label.localeCompare(b.label));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap justify-between items-center gap-3 border-b pb-4 mb-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search label, role, org…"
              className="h-9 w-56 pl-8 text-sm"
            />
          </div>
          <select
            value={filterType}
            onChange={(e) => setFilterType(e.target.value)}
            className="h-9 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 focus:ring-ring"
          >
            <option value="all">All Types</option>
            <option value="person">People</option>

            <option value="organization">Organizations</option>
            <option value="concept">Concepts</option>
            <option value="other">Others (Places, Events, etc.)</option>
          </select>
          {searchQuery.trim() && (
            <span className="text-xs text-muted-foreground">
              {filteredItems.length} of {typeFiltered.length}
            </span>
          )}
        </div>
        <div className="inline-flex items-center rounded-md bg-muted p-1 text-muted-foreground">
          {showPendingScope && (
            <button
              onClick={() => setScope('pending')}
              className={`inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-background transition-all ${scope === 'pending' ? 'bg-background text-foreground shadow-sm' : 'hover:bg-background/50 hover:text-foreground'}`}
            >
              Pending
            </button>
          )}
          <button
            onClick={() => setScope('live')}
            className={`inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-background transition-all ${scope === 'live' ? 'bg-background text-foreground shadow-sm' : 'hover:bg-background/50 hover:text-foreground'}`}
          >
            Live
          </button>
          <button
            onClick={() => setScope('rejected')}
            className={`inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-background transition-all ${scope === 'rejected' ? 'bg-background text-foreground shadow-sm' : 'hover:bg-background/50 hover:text-foreground'}`}
          >
            Rejected
          </button>
        </div>
      </div>

      {scope === 'pending' && filteredItems.length > 0 && (
        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">{filteredItems.length} pending</p>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" className="text-green-600 border-green-600/30 hover:bg-green-50" onClick={() => handleBatch('approve')} disabled={batchProcessing}>
              {batchProcessing ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Check className="h-4 w-4 mr-1" />}
              Approve All
            </Button>
            <Button size="sm" variant="outline" className="text-red-500 border-red-500/30 hover:bg-red-50" onClick={() => handleBatch('reject')} disabled={batchProcessing}>
              {batchProcessing ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <X className="h-4 w-4 mr-1" />}
              Reject All
            </Button>
          </div>
        </div>
      )}
      
      {loading ? (
        <div className="p-8 text-center"><Loader2 className="h-6 w-6 animate-spin mx-auto text-muted-foreground" /></div>
      ) : filteredItems.length === 0 ? (
        <div className="rounded-md border p-8 text-center text-muted-foreground">
          No entities found {filterType !== 'all' ? `matching "${filterType}"` : `in ${scope} view`}.
        </div>
      ) : (
        <>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[25%]">Label</TableHead>
              <TableHead className="w-[12%]">Type</TableHead>
              <TableHead className="w-[28%]">Details</TableHead>
              <TableHead className="w-[12%]">Source</TableHead>
              <TableHead className="w-[12%]">Created</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filteredItems.map((item) => (
              <React.Fragment key={item.id}>
              <TableRow>
                <TableCell className="font-medium">
                  {editingId === item.id ? (
                    <div className="flex items-center gap-2">
                      <Input
                        value={editLabel}
                        onChange={(e) => setEditLabel(e.target.value)}
                        className="h-8"
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') handleRename(item.id);
                          if (e.key === 'Escape') setEditingId(null);
                        }}
                        autoFocus
                      />
                      <Button size="sm" onClick={() => handleRename(item.id)}>Save</Button>
                      <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>Cancel</Button>
                    </div>
                  ) : mergingId === item.id ? (
                    <div className="flex flex-col gap-2">
                      <span className="text-muted-foreground text-xs">Merge &apos;{item.label}&apos; into:</span>
                      <MergeSearchInput
                        nodeType={item.type}
                        scope={scope}
                        onSelect={(targetId, targetLabel) => handleMerge(item.id, targetId, targetLabel)}
                      />
                      <Button size="sm" variant="ghost" className="w-fit h-7 text-xs" onClick={() => setMergingId(null)}>Cancel</Button>
                    </div>
                  ) : (
                    item.label
                  )}
                </TableCell>
                <TableCell>
                  {changingTypeId === item.id ? (
                    <select
                      className="h-7 rounded-md border border-input bg-background px-2 py-1 text-xs shadow-sm"
                      defaultValue={item.type}
                      onChange={(e) => handleChangeType(item.id, e.target.value)}
                      onBlur={() => setChangingTypeId(null)}
                      autoFocus
                    >
                      <option value="person">person</option>

                      <option value="organization">organization</option>
                      <option value="concept">concept</option>
                      <option value="place">place</option>
                      <option value="event">event</option>
                      <option value="animal">animal</option>
                      <option value="emotional_state">emotional_state</option>
                    </select>
                  ) : (
                    <button 
                      onClick={() => setChangingTypeId(item.id)}
                      className="inline-flex items-center rounded-md bg-secondary px-2 py-1 text-xs font-medium ring-1 ring-inset ring-secondary-foreground/10 hover:bg-secondary/80 cursor-pointer"
                      title="Click to change type"
                    >
                      {item.type}
                    </button>
                  )}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  <EntityDetails type={item.type} enrichment={item.metadata?.enrichment} />
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {item.source_text}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {formatDistanceToNow(parseISO(item.created_at), { addSuffix: true })}
                </TableCell>
                <TableCell className="text-right">
                  {editingId !== item.id && mergingId !== item.id && (
                    <div className="flex justify-end gap-1">
                      {scope === 'pending' && !item.clarification && (
                        <>
                          <Button
                            size="icon"
                            variant="ghost"
                            className="text-green-600 hover:text-green-700 hover:bg-green-50 h-8 w-8"
                            onClick={() => handleDecision(item.id, 'approve')}
                            title="Approve"
                          >
                            <Check className="h-4 w-4" />
                          </Button>
                          <Button
                            size="icon"
                            variant="ghost"
                            className="text-amber-600 hover:text-amber-700 hover:bg-amber-50 h-8 w-8"
                            onClick={() => handleDecision(item.id, 'reject')}
                            title="Reject"
                          >
                            <X className="h-4 w-4" />
                          </Button>
                          <div className="w-px h-4 bg-border self-center mx-1" />
                        </>
                      )}
                      {scope === 'rejected' && (
                        <>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-blue-600 hover:text-blue-700 hover:bg-blue-50 h-8"
                            onClick={() => handleDecision(item.id, 'unreject')}
                            title="Un-reject"
                          >
                            Un-reject
                          </Button>
                          <div className="w-px h-4 bg-border self-center mx-1" />
                        </>
                      )}
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8"
                        onClick={() => {
                          setEditLabel(item.label);
                          setEditingId(item.id);
                        }}
                        title="Rename"
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8"
                        onClick={() => setMergingId(item.id)}
                        title="Merge into existing"
                      >
                        <GitMerge className="h-4 w-4" />
                      </Button>
                      {scope === 'live' && (
                        <Button
                          size="icon"
                          variant="ghost"
                          className="h-8 w-8"
                          onClick={() => openEnrichmentEditor(item)}
                          title="Edit details (role, weight, active, …)"
                        >
                          <Settings2 className="h-4 w-4" />
                        </Button>
                      )}
                      <Button
                        size="icon"
                        variant="ghost"
                        className="text-red-500 hover:text-red-600 hover:bg-red-50 h-8 w-8"
                        onClick={() => setDeleteId(item.id)}
                        title="Delete with cascade"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  )}
                </TableCell>
              </TableRow>
              {item.clarification && (
                <TableRow className="bg-purple-900/10 hover:bg-purple-900/10">
                  <TableCell colSpan={6} className="py-3">
                    <div className="flex flex-col gap-2">
                      <p className="text-sm font-medium text-purple-300">
                        🤔 {item.clarification.question}
                      </p>
                      {item.clarification.question_type === 'grounding' ? (
                        <div className="flex gap-2 max-w-md">
                          <Input
                            value={clarificationAnswers[item.id] || ''}
                            onChange={(e) => setClarificationAnswers(prev => ({ ...prev, [item.id]: e.target.value }))}
                            placeholder="Type answer here..."
                            className="h-8 text-sm flex-1 bg-zinc-900/50"
                          />
                          <Button size="sm" className="bg-purple-600 hover:bg-purple-700 h-8" onClick={() => handleClarification(item, clarificationAnswers[item.id] || '')}>
                            Submit
                          </Button>
                        </div>
                      ) : (
                        <div className="flex gap-2">
                          <Button size="sm" variant="outline" className="h-8 text-green-400 border-green-900 hover:bg-green-900/30" onClick={() => handleClarification(item, 'yes')}>
                            Yes
                          </Button>
                          <Button size="sm" variant="outline" className="h-8 text-red-400 border-red-900 hover:bg-red-900/30" onClick={() => handleClarification(item, 'no')}>
                            No
                          </Button>
                        </div>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              )}
              </React.Fragment>
            ))}
          </TableBody>
        </Table>
      </div>

      <Dialog open={deleteId !== null} onOpenChange={(o) => !o && setDeleteId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Are you absolutely sure?</DialogTitle>
            <DialogDescription>
              This will reject the node <strong>&quot;{items.find(i => i.id === deleteId)?.label}&quot;</strong> 
              AND automatically reject all pending edges referencing it, plus any concept nodes that were orphaned.
            </DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <p className="text-sm font-medium mb-2">Type &quot;DELETE&quot; to confirm:</p>
            <Input 
              value={deleteConfirmText} 
              onChange={e => setDeleteConfirmText(e.target.value)} 
              placeholder="DELETE"
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => { setDeleteId(null); setDeleteConfirmText(""); }}>Cancel</Button>
            <Button 
              onClick={(e) => {
                e.preventDefault();
                handleDelete();
              }}
              disabled={deleteConfirmText !== 'DELETE'}
              className="bg-red-600 hover:bg-red-700 text-white"
            >
              Confirm Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={enrichId !== null} onOpenChange={(o) => !o && setEnrichId(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Edit details</DialogTitle>
            <DialogDescription>
              Update how Rhodey understands &quot;{items.find(i => i.id === enrichId)?.label}&quot;.
              These values live on the graph node and shape prioritization + retrieval.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-2">
              <label htmlFor="enrich-role" className="text-sm font-medium">Role</label>
              <Input
                id="enrich-role"
                placeholder="e.g. Wife, Auditor, Vendor…"
                value={enrichDraft.role ?? ''}
                onChange={(e) => setEnrichDraft(d => ({ ...d, role: e.target.value || null }))}
              />
            </div>
            <div className="grid gap-2">
              <label htmlFor="enrich-org" className="text-sm font-medium">Organization</label>
              <Input
                id="enrich-org"
                placeholder="e.g. CrayonBiz LLP"
                value={enrichDraft.organization_name ?? ''}
                onChange={(e) => setEnrichDraft(d => ({ ...d, organization_name: e.target.value || null }))}
              />
            </div>
            <div className="grid gap-2">
              <label htmlFor="enrich-type" className="text-sm font-medium">Org type</label>
              <Input
                id="enrich-type"
                placeholder="e.g. company, nonprofit, family…"
                value={enrichDraft.org_type ?? ''}
                onChange={(e) => setEnrichDraft(d => ({ ...d, org_type: e.target.value || null }))}
              />
            </div>
            <div className="grid gap-2">
              <label htmlFor="enrich-weight" className="text-sm font-medium">Strategic weight (1–10)</label>
              <Input
                id="enrich-weight"
                type="number"
                min={1}
                max={10}
                placeholder="5"
                value={enrichDraft.strategic_weight ?? ''}
                onChange={(e) => setEnrichDraft(d => ({ ...d, strategic_weight: e.target.value === '' ? null : Number(e.target.value) }))}
              />
            </div>
            <div className="grid gap-2">
              <label htmlFor="enrich-desc" className="text-sm font-medium">Description</label>
              <Textarea
                id="enrich-desc"
                placeholder="One line about this entity…"
                rows={2}
                value={enrichDraft.description ?? ''}
                onChange={(e) => setEnrichDraft(d => ({ ...d, description: e.target.value || null }))}
              />
            </div>
            <div className="flex items-center justify-between rounded-md border p-3">
              <div>
                <p className="text-sm font-medium">Active</p>
                <p className="text-xs text-muted-foreground">Inactive entities are deprioritized in retrieval.</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={enrichDraft.is_active ?? true}
                onClick={() => setEnrichDraft(d => ({ ...d, is_active: !(d.is_active ?? true) }))}
                className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${enrichDraft.is_active ? 'bg-green-600' : 'bg-muted'}`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${enrichDraft.is_active ? 'translate-x-4' : 'translate-x-0.5'}`}
                />
              </button>
            </div>

            {(() => {
              const label = items.find(i => i.id === enrichId)?.label ?? '';
              const isPerson = items.find(i => i.id === enrichId)?.type === 'person';
              if (!isPerson) return null;
              return (
                <>
                  {/* Aliases (person nodes) */}
                  <div className="rounded-md border p-3 space-y-2">
                    <p className="text-sm font-medium">Aliases</p>
                    {enrichAliasesLoading ? (
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
                      </div>
                    ) : enrichAliases.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No aliases — add nicknames or alternate names.</p>
                    ) : (
                      <div className="flex flex-wrap gap-1.5">
                        {enrichAliases.map((a) => (
                          <span
                            key={`${a.canonical_name}:${a.alias}`}
                            className="group inline-flex items-center gap-1 rounded-full bg-primary/5 border border-primary/10 px-2 py-0.5 text-xs"
                          >
                            <Tag className="h-3 w-3 text-muted-foreground" />
                            {a.alias}
                            <button
                              onClick={() => removeAlias(a.alias, a.canonical_name)}
                              className="opacity-50 hover:opacity-100 text-muted-foreground hover:text-destructive"
                              aria-label={`Delete alias ${a.alias}`}
                            >
                              <X className="h-3 w-3" />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                    <div className="flex items-center gap-2">
                      <Input
                        value={enrichNewAlias}
                        onChange={(e) => setEnrichNewAlias(e.target.value)}
                        placeholder="e.g. Nickname…"
                        className="h-8 text-sm flex-1"
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && enrichNewAlias.trim() && !enrichAddingAlias) {
                            addAlias(label);
                          }
                        }}
                      />
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-8"
                        disabled={!enrichNewAlias.trim() || enrichAddingAlias}
                        onClick={() => addAlias(label)}
                      >
                        {enrichAddingAlias ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
                      </Button>
                    </div>
                  </div>

                  {/* Active tasks (person nodes) */}
                  <div className="rounded-md border p-3 space-y-2">
                    <p className="text-sm font-medium">
                      Active Tasks
                      {!enrichTasksLoading && enrichTasks.length > 0 && (
                        <span className="ml-1 text-xs text-muted-foreground">({enrichTasks.length})</span>
                      )}
                    </p>
                    {enrichTasksLoading ? (
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
                      </div>
                    ) : enrichTasks.length === 0 ? (
                      <p className="text-xs text-muted-foreground">No active tasks mention this entity.</p>
                    ) : (
                      <div className="max-h-40 overflow-y-auto space-y-1.5">
                        {enrichTasks.slice(0, 15).map((t) => (
                          <div key={t.id} className="text-xs flex items-center gap-2">
                            <span
                              className={`shrink-0 rounded-full px-2 py-0.5 font-medium ${
                                t.priority === 'high' || t.priority === 'urgent'
                                  ? 'bg-amber-500/10 text-amber-600'
                                  : 'bg-muted text-muted-foreground'
                              }`}
                            >
                              {t.priority}
                            </span>
                            <span className="truncate">{t.title}</span>
                          </div>
                        ))}
                        {enrichTasks.length > 15 && (
                          <p className="text-[11px] text-muted-foreground">…{enrichTasks.length - 15} more</p>
                        )}
                      </div>
                    )}
                  </div>
                </>
              );
            })()}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEnrichId(null)}>Cancel</Button>
            <Button onClick={saveEnrichment} disabled={enrichSaving}>
              {enrichSaving ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Save className="h-4 w-4 mr-1" />}
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      </>
      )}
    </div>
  );
}

/**
 * Compact enrichment summary for the Details column.
 * Person: role + weight. Org: org_type + description. Others: nothing.
 */
function EntityDetails({ type, enrichment }: { type: string; enrichment?: Record<string, unknown> }) {
  if (!enrichment) return <span className="text-muted-foreground">—</span>;

  const bits: { text: string; highlight?: boolean }[] = [];
  if (type === 'person') {
    if (typeof enrichment.role === 'string') bits.push({ text: enrichment.role });
    if (typeof enrichment.strategic_weight === 'number') {
      bits.push({
        text: `weight ${enrichment.strategic_weight}/10`,
        highlight: enrichment.strategic_weight >= 8,
      });
    }
  } else if (type === 'organization') {
    if (typeof enrichment.org_type === 'string') bits.push({ text: enrichment.org_type });
    if (enrichment.description) bits.push({ text: String(enrichment.description) });
  }
  if (bits.length === 0) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {bits.slice(0, 3).map((b, i) => (
        <span
          key={i}
          className={`rounded bg-secondary/60 px-1.5 py-0.5 ${b.highlight ? 'text-primary font-medium' : ''}`}
        >
          {b.text}
        </span>
      ))}
      {bits.length > 3 && <span className="text-muted-foreground">+{bits.length - 3}</span>}
    </div>
  );
}
