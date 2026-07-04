'use client';

import { useState, useMemo, useCallback } from 'react';
import type { Resource, ResourceCluster } from '@/lib/resources/types';
import { updateResourceCluster, fetchResource, fetchRelatedResources, dismissResource } from '@/lib/resources/api';
import { Search, Globe, FileText, LayoutGrid, Maximize2, Inbox, ExternalLink, ChevronLeft, List, XCircle } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent } from '@/components/ui/dialog';
import { Separator } from '@/components/ui/separator';
import { Button } from '@/components/ui/button';
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
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid');
  
  // Selection States
  const [expandedClusterId, setExpandedClusterId] = useState<number | 'unmapped' | null>(null);
  const [selectedResource, setSelectedResource] = useState<Resource | null>(null);
  const [relatedResources, setRelatedResources] = useState<Resource[]>([]);

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

  const handleDismiss = useCallback(async (resourceId: number) => {
    try {
      await dismissResource(resourceId);
      setResources(prev => prev.filter(r => r.id !== resourceId));
      if (selectedResource?.id === resourceId) {
        setSelectedResource(null);
      }
    } catch (err: any) {
      console.error('Failed to dismiss resource:', err);
      alert('Failed to dismiss resource: ' + (err.message || 'Unknown error'));
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

        <div className="flex items-center gap-3 w-full sm:w-auto">
          <div className="relative w-full sm:w-72">
            <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search across all clusters..."
              className="pl-9 bg-muted/50 border-border/50 rounded-xl"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div className="flex bg-muted/50 rounded-lg p-1 shrink-0 border border-border/50">
            <button
              onClick={() => setViewMode('grid')}
              className={cn("p-1.5 rounded-md transition-colors", viewMode === 'grid' ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground")}
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={cn("p-1.5 rounded-md transition-colors", viewMode === 'list' ? "bg-background shadow-sm" : "text-muted-foreground hover:text-foreground")}
            >
              <List className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>

      {viewMode === 'grid' ? (
        <div className="flex-1 max-w-7xl mx-auto w-full p-4 md:p-6 lg:p-8">
          <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-6 auto-rows-[220px]">
            {renderBentoBox('unmapped', 'Inbox / Unmapped', unmapped, 'Floating resources awaiting categorization.')}
            {initialClusters.map(c => renderBentoBox(c.id, c.title, grouped[c.id] || [], c.description))}
          </div>
        </div>
      ) : (
        <div className="flex-1 max-w-7xl mx-auto w-full p-4 md:p-6 lg:p-8">
          <div className="bg-card border border-border/50 rounded-xl overflow-hidden shadow-sm">
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <thead className="text-xs text-muted-foreground uppercase bg-muted/30 border-b border-border/50">
                  <tr>
                    <th className="px-4 py-3 font-medium">Title</th>
                    <th className="px-4 py-3 font-medium">Hostname</th>
                    <th className="px-4 py-3 font-medium">Category</th>
                    <th className="px-4 py-3 font-medium">Cluster</th>
                    <th className="px-4 py-3 font-medium">Date</th>
                    <th className="px-4 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/30">
                  {filteredResources.map(r => (
                    <tr key={r.id} className="hover:bg-muted/10 transition-colors">
                      <td className="px-4 py-3 max-w-[300px]">
                        <div className="flex items-center gap-2">
                          <div className="flex w-6 h-6 rounded-full items-center justify-center shrink-0 bg-muted/50 text-muted-foreground">
                            {r.url ? <Globe className="h-3 w-3" /> : <FileText className="h-3 w-3" />}
                          </div>
                          <a 
                            href={r.url || '#'} 
                            target="_blank" 
                            rel="noopener noreferrer"
                            className={cn("font-medium truncate hover:underline", !r.url && "pointer-events-none")}
                          >
                            {getDisplayTitle(r)}
                          </a>
                        </div>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-muted-foreground truncate max-w-[150px]">
                        {r.hostname || 'Local'}
                      </td>
                      <td className="px-4 py-3">
                        {r.category ? (
                          <span className={cn("text-[10px] px-2 py-1 rounded font-bold uppercase shrink-0 whitespace-nowrap", categoryColors[r.category] || "text-muted-foreground bg-muted")}>
                            {r.category.substring(0,4)}
                          </span>
                        ) : <span className="text-muted-foreground/50">-</span>}
                      </td>
                      <td className="px-4 py-3">
                        <select
                          value={r.cluster_id ? String(r.cluster_id) : 'unmapped'}
                          onChange={(e) => handleClusterChange(r.id, e.target.value === 'unmapped' ? null : Number(e.target.value))}
                          className="w-full max-w-[150px] rounded-md border border-border/50 bg-transparent text-xs px-2 py-1 focus:outline-none focus:ring-1 focus:ring-primary/20 text-foreground cursor-pointer hover:bg-muted/40"
                        >
                          <option value="unmapped">Unmapped</option>
                          {initialClusters.map((m) => (
                            <option key={m.id} value={String(m.id)}>
                              {m.title}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                        {formatDate(r.created_at)}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button 
                          variant="ghost" 
                          size="sm" 
                          onClick={() => handleDismiss(r.id)}
                          className="h-7 px-2 text-muted-foreground hover:text-red-500 hover:bg-red-500/10"
                        >
                          <XCircle className="w-3.5 h-3.5 mr-1" />
                          Dismiss
                        </Button>
                      </td>
                    </tr>
                  ))}
                  {filteredResources.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-4 py-12 text-center text-muted-foreground">
                        No resources found.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Expanded Cluster Modal (Split Pane) */}
      <Dialog 
        open={expandedClusterId !== null} 
        onOpenChange={(open) => {
          if (!open) {
            setExpandedClusterId(null);
            setSelectedResource(null);
          }
        }}
      >
        <DialogContent 
          showCloseButton={false}
          className="max-w-[95vw] sm:max-w-[95vw] md:max-w-5xl lg:max-w-7xl w-[95vw] max-h-[90vh] h-[90vh] flex flex-col p-0 gap-0 overflow-hidden bg-background/95 backdrop-blur-2xl border-border/50 shadow-2xl rounded-2xl"
        >
          {activeCluster && (
            <div className="flex h-full w-full overflow-hidden">
              
              {/* Left Pane: Resource List */}
              <div className={cn(
                "flex-col h-full border-r border-border/50 bg-card/30 w-full md:w-2/5 md:min-w-[380px] md:max-w-[450px]",
                selectedResource ? "hidden md:flex" : "flex"
              )}>
                {/* Header */}
                <div className="p-5 md:p-6 pb-4 border-b shrink-0 bg-card/50">
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-3">
                      <div className={cn("p-2 rounded-xl", expandedClusterId === 'unmapped' ? "bg-muted" : "bg-primary/10 text-primary")}>
                        {expandedClusterId === 'unmapped' ? <Inbox className="h-5 w-5" /> : <LayoutGrid className="h-5 w-5" />}
                      </div>
                      <div>
                        <h2 className="text-xl font-bold leading-tight">{activeCluster.title}</h2>
                        <p className="text-xs text-muted-foreground mt-0.5 font-mono">{activeCluster.items.length} items</p>
                      </div>
                    </div>
                    {/* Custom mobile close button to close dialog since we disabled the default one */}
                    <button 
                      onClick={() => setExpandedClusterId(null)}
                      className="md:hidden p-2 bg-muted/50 rounded-full text-muted-foreground hover:bg-muted"
                    >
                      <Search className="h-4 w-4 rotate-45" /> {/* Just using as a close cross visually or we can use X */}
                    </button>
                  </div>
                  {activeCluster.description && (
                    <p className="text-sm text-muted-foreground line-clamp-2">{activeCluster.description}</p>
                  )}
                </div>

                {/* List */}
                <div className="flex-1 overflow-y-auto p-3 md:p-4 bg-muted/5">
                  <div className="flex flex-col gap-1.5">
                    {activeCluster.items.map(r => {
                      const isSelected = selectedResource?.id === r.id;
                      return (
                        <div 
                          key={r.id} 
                          onClick={() => handleResourceClick(r)}
                          className={cn(
                            "group flex items-center gap-3 p-3 rounded-xl border cursor-pointer transition-all",
                            isSelected 
                              ? "bg-primary/5 border-primary/30 shadow-sm" 
                              : "border-transparent hover:bg-accent/50 hover:border-border/40"
                          )}
                        >
                          <div className={cn(
                            "flex w-8 h-8 rounded-full items-center justify-center shrink-0 transition-colors",
                            isSelected ? "bg-primary/10 text-primary" : "bg-muted/50 text-muted-foreground group-hover:bg-background"
                          )}>
                            {r.url ? <Globe className="h-4 w-4" /> : <FileText className="h-4 w-4" />}
                          </div>
                          
                          <div className="flex-1 min-w-0 flex flex-col gap-0.5">
                            <div className={cn(
                              "font-semibold text-sm truncate",
                              isSelected ? "text-primary" : "text-foreground"
                            )}>
                              {getDisplayTitle(r)}
                            </div>
                            <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                              <span className="truncate max-w-[120px] font-mono opacity-80">{r.hostname || 'Local'}</span>
                              {r.category && (
                                <>
                                  <span className="border-l h-2 border-border/50"></span>
                                  <span className="font-bold uppercase tracking-wider opacity-70">{r.category.substring(0,4)}</span>
                                </>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                    {activeCluster.items.length === 0 && (
                      <div className="flex flex-col items-center justify-center h-48 text-muted-foreground text-sm">
                        <LayoutGrid className="h-10 w-10 opacity-20 mb-3" />
                        <p>Inbox zero.</p>
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Right Pane: Detail View */}
              <div className={cn(
                "flex-col h-full bg-background overflow-y-auto flex-1 relative",
                selectedResource ? "flex" : "hidden md:flex"
              )}>
                {selectedResource ? (
                  <div className="flex flex-col p-6 md:p-10 max-w-3xl mx-auto w-full animate-in fade-in slide-in-from-right-4 duration-300">
                    <div className="flex items-center justify-between mb-6">
                      <button 
                        onClick={() => setSelectedResource(null)} 
                        className="md:hidden flex items-center w-fit text-sm font-medium text-muted-foreground hover:text-foreground transition-colors bg-muted/50 px-3 py-1.5 rounded-full"
                      >
                        <ChevronLeft className="w-4 h-4 mr-1" /> Back
                      </button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleDismiss(selectedResource.id)}
                        className="ml-auto text-muted-foreground hover:text-red-500 hover:bg-red-500/10"
                      >
                        <XCircle className="w-4 h-4 mr-2 md:mr-2" />
                        <span className="hidden md:inline">Dismiss Resource</span>
                        <span className="md:hidden">Dismiss</span>
                      </Button>
                    </div>

                    <div className="flex items-center gap-3 mb-6">
                      {selectedResource.category && (
                        <span className={cn("text-xs px-2.5 py-1 rounded-md font-bold uppercase tracking-wider", categoryColors[selectedResource.category] || "text-muted-foreground bg-muted")}>
                          {selectedResource.category}
                        </span>
                      )}
                      <span className="text-xs text-muted-foreground/50 font-mono ml-auto">
                        Added {formatDate(selectedResource.created_at)}
                      </span>
                    </div>

                    <h2 className="text-2xl md:text-3xl font-bold leading-tight tracking-tight mb-6">
                      {selectedResource.title || selectedResource.hostname || 'Untitled'}
                    </h2>

                    {selectedResource.url && (
                      <a
                        href={selectedResource.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="group flex items-center gap-2 text-sm text-primary/80 hover:text-primary font-mono mb-8 bg-primary/5 hover:bg-primary/10 w-fit px-3 py-2 rounded-lg transition-colors"
                      >
                        <Globe className="h-4 w-4" />
                        {selectedResource.hostname || selectedResource.url}
                        <ExternalLink className="h-3 w-3 opacity-50 group-hover:opacity-100 transition-opacity" />
                      </a>
                    )}

                    <div className="space-y-8">
                      {selectedResource.strategic_note && (
                        <div>
                          <h4 className="text-xs font-bold uppercase tracking-widest text-muted-foreground mb-3">Strategic Context</h4>
                          <p className="text-base text-foreground/90 leading-relaxed border-l-2 border-primary/30 pl-4 italic">
                            "{selectedResource.strategic_note}"
                          </p>
                        </div>
                      )}

                      {selectedResource.summary && (
                        <div>
                          <h4 className="text-xs font-bold uppercase tracking-widest text-muted-foreground mb-3">Summary</h4>
                          <p className="text-sm text-muted-foreground leading-relaxed">
                            {selectedResource.summary}
                          </p>
                        </div>
                      )}

                      <Separator className="my-8 opacity-50" />

                      <div>
                        <h4 className="text-xs font-bold uppercase tracking-widest text-muted-foreground mb-3">Cluster Assignment</h4>
                        <select
                          value={selectedResource.cluster_id ? String(selectedResource.cluster_id) : 'unmapped'}
                          onChange={(e) => handleClusterChange(selectedResource.id, e.target.value === 'unmapped' ? null : Number(e.target.value))}
                          className="w-full md:w-80 rounded-xl border border-border/50 bg-muted/20 text-sm px-4 py-3 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground cursor-pointer hover:bg-muted/40"
                        >
                          <option value="unmapped">Inbox / Unmapped</option>
                          {initialClusters.map((m) => (
                            <option key={m.id} value={String(m.id)}>
                              {m.title}
                            </option>
                          ))}
                        </select>
                      </div>

                      {relatedResources.length > 0 && (
                        <div className="pt-4">
                          <h4 className="text-xs font-bold uppercase tracking-widest text-muted-foreground mb-4">Related in this Cluster ({relatedResources.length})</h4>
                          <div className="flex flex-col gap-2">
                            {relatedResources.slice(0, 5).map((r) => (
                              <div key={r.id} className="flex items-center gap-3 p-3 rounded-lg border border-border/30 bg-muted/10">
                                <div className="flex-1 font-medium text-sm truncate">{getDisplayTitle(r)}</div>
                                {r.category && (
                                  <span className={cn("text-[9px] px-1.5 py-0.5 rounded uppercase font-bold shrink-0", categoryColors[r.category] || "bg-muted")}>
                                    {r.category.substring(0,4)}
                                  </span>
                                )}
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col items-center justify-center h-full text-muted-foreground opacity-50 p-6 text-center">
                    <FileText className="h-16 w-16 mb-6 opacity-20" />
                    <p className="text-lg font-medium">Select a resource</p>
                    <p className="text-sm mt-2 max-w-sm">Click any resource in the list to view its strategic context, summary, and related cluster items.</p>
                  </div>
                )}
              </div>

              {/* Desktop Close Button (absolute over the right pane) */}
              <button 
                onClick={() => setExpandedClusterId(null)}
                className="hidden md:flex absolute top-6 right-6 p-2.5 bg-muted/50 hover:bg-muted rounded-full text-muted-foreground transition-colors z-50"
              >
                <Search className="h-4 w-4 rotate-45" /> {/* Poor man's X icon since we didn't import X */}
              </button>

            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}