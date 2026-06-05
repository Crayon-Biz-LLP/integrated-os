'use client';

import { useState, useMemo, useCallback } from 'react';
import type { Resource, ResourceCluster } from '@/lib/resources/types';
import { ResourceDetailSheet } from '@/components/resources/resource-detail-sheet';
import { updateResourceCluster, fetchResource, fetchRelatedResources } from '@/lib/resources/api';
import { Kanban, List, Search, ChevronDown, ChevronRight, Globe, FileText } from 'lucide-react';
import { Input } from '@/components/ui/input';

const categoryColors: Record<string, string> = {
  TECHTOOL: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  COMPETITOR: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  LEADPOTENTIAL: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  MARKETTREND: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  ASHRAYA: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  PERSONAL: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
};

function getDisplayTitle(resource: Resource): string {
  return resource.title || resource.hostname || resource.url || 'Untitled';
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function ClustersShell({
  initialResources,
  initialClusters,
}: {
  initialResources: Resource[];
  initialClusters: ResourceCluster[];
}) {
  const [resources, setResources] = useState(initialResources);
  const [search, setSearch] = useState('');
  const [view, setView] = useState<'board' | 'list'>('board');
  const [selectedResource, setSelectedResource] = useState<Resource | null>(null);
  const [relatedResources, setRelatedResources] = useState<Resource[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);

  // Filter resources
  const filteredResources = useMemo(() => {
    let result = [...resources];
    if (search) {
      const q = search.toLowerCase();
      result = result.filter(
        (r) =>
          r.title?.toLowerCase().includes(q) ||
          r.summary?.toLowerCase().includes(q) ||
          r.strategic_note?.toLowerCase().includes(q) ||
          r.category?.toLowerCase().includes(q)
      );
    }
    return result;
  }, [resources, search]);

  // Group by cluster
  const { unmapped, grouped } = useMemo(() => {
    const u: Resource[] = [];
    const g: Record<number, Resource[]> = {};
    for (const c of initialClusters) {
      g[c.id] = [];
    }
    for (const r of filteredResources) {
      if (r.cluster_id === null || !g[r.cluster_id]) {
        u.push(r);
      } else {
        g[r.cluster_id].push(r);
      }
    }
    return { unmapped: u, grouped: g };
  }, [filteredResources, initialClusters]);

  const handleResourceClick = useCallback(async (resource: Resource) => {
    setSelectedResource(resource);
    setDetailOpen(true);

    if (resource.cluster_id) {
      try {
        const related = await fetchRelatedResources(resource.id);
        setRelatedResources(related);
      } catch {
        setRelatedResources([]);
      }
    } else {
      setRelatedResources([]);
    }
  }, []);

  const handleClusterChange = useCallback(async (resourceId: number, clusterId: number | null) => {
    try {
      await updateResourceCluster(resourceId, clusterId);
      
      // Update local state so it immediately reflects without reload
      setResources(prev => prev.map(r => r.id === resourceId ? { ...r, cluster_id: clusterId } : r));

      if (selectedResource?.id === resourceId) {
        const updated = await fetchResource(resourceId);
        setSelectedResource(updated);
        if (updated.cluster_id) {
          const related = await fetchRelatedResources(resourceId);
          setRelatedResources(related);
        } else {
          setRelatedResources([]);
        }
      }
    } catch (err: any) {
      console.error('Failed to update cluster:', err);
      alert('Failed to update cluster: ' + (err.message || 'Unknown error'));
    }
  }, [selectedResource]);

  const renderMicroCard = (r: Resource) => (
    <div
      key={r.id}
      onClick={() => handleResourceClick(r)}
      className="bg-card hover:bg-accent border border-border/50 hover:border-border rounded-md p-2.5 cursor-pointer shadow-sm transition-all text-sm flex flex-col gap-1.5"
    >
      <div className="font-medium leading-snug line-clamp-2">{getDisplayTitle(r)}</div>
      <div className="flex items-center justify-between mt-0.5">
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground/70">
          {r.url ? <Globe className="h-3 w-3" /> : <FileText className="h-3 w-3" />}
          <span className="truncate max-w-[120px]">{r.hostname || 'Local'}</span>
        </div>
        {r.category && (
          <span className={`text-[10px] px-1.5 py-0.5 rounded-sm font-semibold uppercase ${categoryColors[r.category] || 'bg-muted text-muted-foreground'}`}>
            {r.category.substring(0, 4)}
          </span>
        )}
      </div>
    </div>
  );

  const BoardView = () => (
    <div className="flex-1 overflow-x-auto pb-4 pt-2">
      <div className="flex gap-4 h-full px-4 md:px-6 min-w-max">
        {/* Unmapped Column */}
        <div className="w-[320px] flex flex-col gap-3 bg-muted/30 rounded-xl p-3 border border-border/50">
          <div className="flex items-center justify-between px-1">
            <h3 className="font-semibold text-sm">Inbox / Unmapped</h3>
            <span className="text-xs font-mono bg-background px-1.5 py-0.5 rounded-md border">{unmapped.length}</span>
          </div>
          <div className="flex flex-col gap-2 overflow-y-auto pr-1 pb-2">
            {unmapped.map(renderMicroCard)}
            {unmapped.length === 0 && <p className="text-xs text-muted-foreground p-2 text-center italic">Inbox zero</p>}
          </div>
        </div>

        {/* Cluster Columns */}
        {initialClusters.map(c => (
          <div key={c.id} className="w-[320px] flex flex-col gap-3 bg-muted/10 rounded-xl p-3 border border-border/50">
            <div className="flex flex-col gap-0.5 px-1">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold text-sm truncate pr-2">{c.title}</h3>
                <span className="text-xs font-mono bg-background px-1.5 py-0.5 rounded-md border">{grouped[c.id]?.length || 0}</span>
              </div>
              {c.description && <p className="text-[10px] text-muted-foreground line-clamp-2 leading-tight mt-1">{c.description}</p>}
            </div>
            <div className="flex flex-col gap-2 overflow-y-auto pr-1 pb-2">
              {grouped[c.id]?.map(renderMicroCard)}
              {(!grouped[c.id] || grouped[c.id].length === 0) && <p className="text-xs text-muted-foreground p-2 text-center italic">Empty</p>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );

  const ListView = () => {
    // We will just render them expanded for simplicity, since standard search provides quick filtering
    return (
      <div className="flex-1 overflow-y-auto px-4 md:px-6 pb-8">
        <div className="max-w-6xl mx-auto flex flex-col gap-8 mt-4">
          
          {unmapped.length > 0 && (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2 border-b pb-2">
                <h3 className="font-semibold">Inbox / Unmapped</h3>
                <span className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded-md">{unmapped.length}</span>
              </div>
              <div className="flex flex-col border rounded-lg divide-y bg-card overflow-hidden">
                {unmapped.map(r => (
                  <div key={r.id} onClick={() => handleResourceClick(r)} className="flex items-center gap-4 px-4 py-3 hover:bg-muted/50 cursor-pointer text-sm transition-colors">
                    <div className="w-4 flex-shrink-0 opacity-50">{r.url ? <Globe className="h-4 w-4" /> : <FileText className="h-4 w-4" />}</div>
                    <div className="flex-1 font-medium truncate">{getDisplayTitle(r)}</div>
                    <div className="w-32 flex-shrink-0 text-muted-foreground/70 truncate">{r.hostname}</div>
                    <div className="w-24 flex-shrink-0">
                      {r.category && <span className={`text-[10px] px-2 py-0.5 rounded font-semibold uppercase ${categoryColors[r.category] || 'bg-muted text-muted-foreground'}`}>{r.category}</span>}
                    </div>
                    <div className="w-24 flex-shrink-0 text-right text-muted-foreground/50">{formatDate(r.created_at)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {initialClusters.map(c => {
            const items = grouped[c.id] || [];
            if (items.length === 0) return null;
            return (
              <div key={c.id} className="flex flex-col gap-2">
                <div className="flex items-end gap-3 border-b pb-2">
                  <h3 className="font-semibold">{c.title}</h3>
                  <span className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded-md">{items.length}</span>
                  {c.description && <span className="text-xs text-muted-foreground/60 hidden md:inline-block ml-2 truncate max-w-[400px]">— {c.description}</span>}
                </div>
                <div className="flex flex-col border rounded-lg divide-y bg-card overflow-hidden">
                  {items.map(r => (
                    <div key={r.id} onClick={() => handleResourceClick(r)} className="flex items-center gap-4 px-4 py-3 hover:bg-muted/50 cursor-pointer text-sm transition-colors">
                      <div className="w-4 flex-shrink-0 opacity-50">{r.url ? <Globe className="h-4 w-4" /> : <FileText className="h-4 w-4" />}</div>
                      <div className="flex-1 font-medium truncate">{getDisplayTitle(r)}</div>
                      <div className="w-32 flex-shrink-0 text-muted-foreground/70 truncate">{r.hostname}</div>
                      <div className="w-24 flex-shrink-0">
                        {r.category && <span className={`text-[10px] px-2 py-0.5 rounded font-semibold uppercase ${categoryColors[r.category] || 'bg-muted text-muted-foreground'}`}>{r.category}</span>}
                      </div>
                      <div className="w-24 flex-shrink-0 text-right text-muted-foreground/50">{formatDate(r.created_at)}</div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="flex flex-col h-[calc(100vh-7.5rem)] lg:h-screen">
      {/* Header Area */}
      <div className="flex flex-col gap-4 p-4 md:px-6 md:py-5 shrink-0 border-b bg-background/95 backdrop-blur z-10">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Knowledge Base</h1>
            <p className="text-sm text-muted-foreground/70 mt-0.5">
              Strategic clusters and vaulted resources
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="relative flex-1 max-w-md">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Filter resources..."
              className="pl-9 bg-muted/50 h-9 text-sm"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="flex items-center bg-muted/50 p-1 rounded-lg border">
            <button
              onClick={() => setView('board')}
              className={`p-1.5 rounded-md transition-colors ${view === 'board' ? 'bg-background shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
              title="Board View"
            >
              <Kanban className="h-4 w-4" />
            </button>
            <button
              onClick={() => setView('list')}
              className={`p-1.5 rounded-md transition-colors ${view === 'list' ? 'bg-background shadow-sm text-foreground' : 'text-muted-foreground hover:text-foreground'}`}
              title="List View"
            >
              <List className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>

      {/* Main Content Area */}
      {view === 'board' ? <BoardView /> : <ListView />}

      <ResourceDetailSheet
        resource={selectedResource}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        clusters={initialClusters}
        onClusterChange={handleClusterChange}
        relatedResources={relatedResources}
      />
    </div>
  );
}