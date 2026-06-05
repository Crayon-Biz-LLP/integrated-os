'use client';

import { useState, useMemo, useCallback } from 'react';
import type { Resource, ResourceCluster } from '@/lib/resources/types';
import { ResourceDetailSheet } from '@/components/resources/resource-detail-sheet';
import { updateResourceCluster, fetchResource, fetchRelatedResources } from '@/lib/resources/api';
import { Search, Globe, FileText, LayoutGrid, Maximize2, Inbox } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

const categoryColors: Record<string, string> = {
  TECHTOOL: 'text-blue-500 bg-blue-500/10',
  COMPETITOR: 'text-red-500 bg-red-500/10',
  LEADPOTENTIAL: 'text-green-500 bg-green-500/10',
  MARKETTREND: 'text-purple-500 bg-purple-500/10',
  ASHRAYA: 'text-amber-500 bg-amber-500/10',
  PERSONAL: 'text-emerald-500 bg-emerald-500/10',
};

function getDisplayTitle(resource: Resource): string {
  return resource.title || resource.hostname || resource.url || 'Untitled';
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
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
  
  // Selection States
  const [expandedClusterId, setExpandedClusterId] = useState<number | 'unmapped' | null>(null);
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

  // Determine span sizes for Bento Grid
  const getBentoSpan = (count: number) => {
    if (count >= 30) return 'col-span-1 md:col-span-2 row-span-2';
    if (count >= 15) return 'col-span-1 md:col-span-2 row-span-1';
    return 'col-span-1 md:col-span-1 row-span-1';
  };

  const activeCluster = useMemo(() => {
    if (expandedClusterId === 'unmapped') return { title: 'Inbox / Unmapped', items: unmapped, description: 'Resources waiting to be categorized.' };
    if (expandedClusterId !== null) {
      const c = initialClusters.find(c => c.id === expandedClusterId);
      return c ? { title: c.title, items: grouped[c.id] || [], description: c.description } : null;
    }
    return null;
  }, [expandedClusterId, unmapped, grouped, initialClusters]);

  const renderBentoBox = (id: number | 'unmapped', title: string, items: Resource[], description?: string | null) => {
    const spanClass = getBentoSpan(items.length);
    const isUnmapped = id === 'unmapped';

    return (
      <div
        key={id}
        onClick={() => setExpandedClusterId(id)}
        className={cn(
          "group relative flex flex-col bg-card border border-border/50 rounded-2xl p-5 cursor-pointer overflow-hidden transition-all duration-300 hover:shadow-lg hover:border-primary/30 hover:-translate-y-1",
          spanClass,
          isUnmapped ? "bg-muted/30 border-dashed" : ""
        )}
      >
        <div className="absolute top-4 right-4 opacity-0 group-hover:opacity-100 transition-opacity">
          <Maximize2 className="h-4 w-4 text-muted-foreground" />
        </div>
        
        <div className="flex items-center gap-3 mb-3">
          <div className={cn("p-2 rounded-xl flex-shrink-0", isUnmapped ? "bg-muted" : "bg-primary/10 text-primary")}>
            {isUnmapped ? <Inbox className="h-5 w-5" /> : <LayoutGrid className="h-5 w-5" />}
          </div>
          <div>
            <h3 className="font-bold text-base leading-tight tracking-tight line-clamp-1 pr-6">{title}</h3>
            <p className="text-xs font-mono text-muted-foreground mt-0.5">{items.length} resources</p>
          </div>
        </div>

        {description && (
          <p className="text-xs text-muted-foreground/80 line-clamp-2 mb-4 leading-relaxed">
            {description}
          </p>
        )}

        <div className="mt-auto flex flex-col gap-2 relative z-10">
          {items.slice(0, spanClass.includes('row-span-2') ? 5 : 3).map(r => (
            <div key={r.id} className="flex items-center gap-2 text-xs bg-background/50 border border-border/40 rounded-lg px-3 py-2 backdrop-blur-sm">
              <span className="truncate flex-1 font-medium text-muted-foreground group-hover:text-foreground transition-colors">
                {getDisplayTitle(r)}
              </span>
              {r.category && (
                <span className={cn("text-[9px] px-1.5 py-0.5 rounded font-bold uppercase shrink-0", categoryColors[r.category] || "text-muted-foreground bg-muted")}>
                  {r.category.substring(0,4)}
                </span>
              )}
            </div>
          ))}
          {items.length === 0 && (
            <div className="text-xs text-muted-foreground/50 italic py-2">Empty</div>
          )}
          {items.length > (spanClass.includes('row-span-2') ? 5 : 3) && (
            <div className="text-[10px] font-bold text-muted-foreground/50 uppercase tracking-widest text-center mt-1">
              + {items.length - (spanClass.includes('row-span-2') ? 5 : 3)} more
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="flex flex-col min-h-[calc(100vh-7.5rem)] lg:min-h-screen bg-muted/10 pb-12">
      {/* Header Area */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 p-6 shrink-0 border-b bg-background/95 backdrop-blur sticky top-0 z-20">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Knowledge Base</h1>
          <p className="text-sm text-muted-foreground/70 mt-1">
            Visual cluster mapping & resource management
          </p>
        </div>

        <div className="relative w-full sm:w-72">
          <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search across all clusters..."
            className="pl-9 bg-muted/50 border-border/50 rounded-xl"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {/* Bento Grid */}
      <div className="flex-1 max-w-7xl mx-auto w-full p-4 md:p-6 lg:p-8">
        <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-6 auto-rows-[220px]">
          {renderBentoBox('unmapped', 'Inbox / Unmapped', unmapped, 'Floating resources awaiting categorization.')}
          {initialClusters.map(c => renderBentoBox(c.id, c.title, grouped[c.id] || [], c.description))}
        </div>
      </div>

      {/* Expanded Cluster Modal */}
      <Dialog open={expandedClusterId !== null} onOpenChange={(open) => !open && setExpandedClusterId(null)}>
        <DialogContent className="max-w-4xl max-h-[85vh] h-[85vh] flex flex-col p-0 gap-0 overflow-hidden bg-background/95 backdrop-blur-xl border-border/50 shadow-2xl rounded-2xl">
          {activeCluster && (
            <>
              <DialogHeader className="p-6 pb-4 border-b shrink-0 bg-card/50">
                <div className="flex items-center gap-3">
                  <div className={cn("p-2 rounded-xl", expandedClusterId === 'unmapped' ? "bg-muted" : "bg-primary/10 text-primary")}>
                    {expandedClusterId === 'unmapped' ? <Inbox className="h-6 w-6" /> : <LayoutGrid className="h-6 w-6" />}
                  </div>
                  <div>
                    <DialogTitle className="text-2xl font-bold">{activeCluster.title}</DialogTitle>
                    <p className="text-sm text-muted-foreground mt-0.5">{activeCluster.items.length} resources</p>
                  </div>
                </div>
                {activeCluster.description && (
                  <p className="text-sm text-muted-foreground mt-3 max-w-2xl">{activeCluster.description}</p>
                )}
              </DialogHeader>
              
              <div className="flex-1 overflow-y-auto p-4 md:p-6 bg-muted/5">
                <div className="flex flex-col gap-2">
                  {activeCluster.items.map(r => (
                    <div 
                      key={r.id} 
                      onClick={() => handleResourceClick(r)}
                      className="group flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4 p-4 rounded-xl border border-border/40 bg-card hover:bg-accent/50 hover:border-border cursor-pointer transition-all hover:shadow-sm"
                    >
                      <div className="hidden sm:flex w-8 h-8 rounded-full bg-muted/50 items-center justify-center shrink-0 group-hover:bg-background transition-colors">
                        {r.url ? <Globe className="h-4 w-4 text-muted-foreground" /> : <FileText className="h-4 w-4 text-muted-foreground" />}
                      </div>
                      
                      <div className="flex-1 min-w-0 flex flex-col gap-1">
                        <div className="font-semibold text-base truncate pr-4">{getDisplayTitle(r)}</div>
                        <div className="flex items-center gap-3 text-xs text-muted-foreground">
                          <span className="truncate max-w-[200px] font-mono opacity-80">{r.hostname || 'Local Document'}</span>
                          <span className="hidden sm:inline-block border-l h-3 border-border/50"></span>
                          <span>{formatDate(r.created_at)}</span>
                        </div>
                      </div>

                      <div className="flex items-center gap-2 sm:w-48 shrink-0 sm:justify-end">
                        {r.category && (
                          <span className={cn("text-[10px] px-2 py-1 rounded-md font-bold uppercase tracking-wider", categoryColors[r.category] || "text-muted-foreground bg-muted")}>
                            {r.category}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                  {activeCluster.items.length === 0 && (
                    <div className="flex flex-col items-center justify-center h-48 text-muted-foreground">
                      <LayoutGrid className="h-12 w-12 opacity-20 mb-4" />
                      <p>No resources found in this view.</p>
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>

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