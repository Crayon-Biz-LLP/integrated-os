'use client';

import { useState, useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { checkSimilarGraphNodes, renamePendingGraphNode, deletePendingGraphNode, mergeGraphNodeIntoExisting, searchGraphNodes, fetchLiveGraphNodes, decideGraphNode } from '@/lib/decisions/api';
import type { GraphPendingNode } from '@/lib/decisions/types';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Loader2, Trash2, Pencil, GitMerge, Check, X } from 'lucide-react';
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
    if (query.length < 2) {
      setResults([]);
      return;
    }
    
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const data = await searchGraphNodes(query, nodeType, scope);
        setResults(Array.isArray(data) ? data : (data as any).data || []);
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
      {results.length > 0 && (
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

export function EntityTableList({ items: initialItems }: { items: GraphPendingNode[] }) {
  const [items, setItems] = useState<GraphPendingNode[]>(initialItems);
  const [scope, setScope] = useState<'pending' | 'live'>('pending');
  const [loading, setLoading] = useState(false);
  
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editLabel, setEditLabel] = useState("");
  
  const [mergingId, setMergingId] = useState<number | null>(null);
  
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [deleteConfirmText, setDeleteConfirmText] = useState("");

  useEffect(() => {
    if (scope === 'pending') {
      setItems(initialItems);
    } else {
      setLoading(true);
      fetchLiveGraphNodes().then(data => {
        setItems(data);
        setLoading(false);
      }).catch(e => {
        console.error(e);
        setLoading(false);
      });
    }
  }, [initialItems, scope]);

  const handleDecision = async (id: number, decision: 'approve' | 'reject') => {
    try {
      await decideGraphNode(id, decision);
      setItems(prev => prev.filter(i => i.id !== id));
      toast.success(decision === 'approve' ? 'Approved successfully' : 'Rejected successfully');
    } catch (e: any) {
      toast.error(e.message || `Failed to ${decision}`);
    }
  };

  const handleRename = async (id: number) => {
    if (!editLabel.trim()) return;
    
    try {
      await renamePendingGraphNode(id, editLabel, scope);
      setItems(prev => prev.map(i => i.id === id ? { ...i, label: editLabel } : i));
      setEditingId(null);
      toast.success("Renamed successfully");
    } catch (e: any) {
      toast.error(e.message || "Failed to rename");
    }
  };

  const handleMerge = async (sourceId: number, targetId: string, targetLabel: string) => {
    try {
      await mergeGraphNodeIntoExisting(sourceId, targetId, scope);
      setItems(prev => prev.filter(i => i.id !== sourceId));
      setMergingId(null);
      toast.success(`Merged into ${targetLabel}`);
    } catch (e: any) {
      toast.error(e.message || "Failed to merge");
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
    } catch (e: any) {
      toast.error(e.message || "Failed to delete");
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex justify-end border-b pb-4 mb-4">
        <div className="inline-flex items-center rounded-md bg-muted p-1 text-muted-foreground">
          <button
            onClick={() => setScope('pending')}
            className={`inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-background transition-all ${scope === 'pending' ? 'bg-background text-foreground shadow-sm' : 'hover:bg-background/50 hover:text-foreground'}`}
          >
            Pending
          </button>
          <button
            onClick={() => setScope('live')}
            className={`inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-background transition-all ${scope === 'live' ? 'bg-background text-foreground shadow-sm' : 'hover:bg-background/50 hover:text-foreground'}`}
          >
            Live
          </button>
        </div>
      </div>
      
      {loading ? (
        <div className="p-8 text-center"><Loader2 className="h-6 w-6 animate-spin mx-auto text-muted-foreground" /></div>
      ) : items.length === 0 ? (
        <div className="rounded-md border p-8 text-center text-muted-foreground">
          No entities found in {scope} view.
        </div>
      ) : (
        <>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[30%]">Label</TableHead>
              <TableHead className="w-[15%]">Type</TableHead>
              <TableHead className="w-[15%]">Source</TableHead>
              <TableHead className="w-[15%]">Created</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((item) => (
              <TableRow key={item.id}>
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
                  <span className="inline-flex items-center rounded-md bg-secondary px-2 py-1 text-xs font-medium ring-1 ring-inset ring-secondary-foreground/10">
                    {item.type}
                  </span>
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
                      {scope === 'pending' && (
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
      </>
      )}
    </div>
  );
}
