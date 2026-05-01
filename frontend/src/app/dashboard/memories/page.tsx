'use client';

import dynamic from 'next/dynamic';
import { useEffect, useState, useCallback, Suspense, useRef } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BookOpen, Loader2, AlertCircle, FileText, Search, X, GitFork, Network } from 'lucide-react';
import { cn } from '@/lib/utils';
import { fetchPagesList, fetchPageById, fetchNodesByPageId, fetchEdgesByPageId } from '@/lib/memories/api';
import { CanonicalPage, CanonicalPageListItem, GraphNode, GraphEdge } from '@/lib/memories/types';

const EgoGraph = dynamic(() => import('@/components/memories/EgoGraph'), { ssr: false });

function formatRelativeTime(dateStr: string | null): string {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString();
}

function SidebarSkeleton() {
  return (
    <div className="space-y-1 p-2">
      {[...Array(5)].map((_, i) => (
        <div key={i} className="h-12 rounded-lg bg-muted/50 animate-pulse" />
      ))}
    </div>
  );
}

function ContentSkeleton() {
  return (
    <div className="p-6 space-y-4">
      <div className="h-8 w-64 rounded bg-muted/50 animate-pulse" />
      <div className="flex gap-2">
        <div className="h-5 w-20 rounded bg-muted/50 animate-pulse" />
        <div className="h-5 w-32 rounded bg-muted/50 animate-pulse" />
      </div>
      <div className="space-y-2 mt-6">
        {[...Array(8)].map((_, i) => (
          <div key={i} className="h-4 w-full rounded bg-muted/50 animate-pulse" />
        ))}
      </div>
    </div>
  );
}

function MemoriesContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [pages, setPages] = useState<CanonicalPageListItem[]>([]);
  const [pagesLoading, setPagesLoading] = useState(true);
  const [pagesError, setPagesError] = useState<string | null>(null);

  const [selectedPage, setSelectedPage] = useState<CanonicalPage | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);

  const selectedId = searchParams.get('page');
  const hasAutoSelected = useRef(false);

  const [searchQuery, setSearchQuery] = useState('');
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);

  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [nodesLoading, setNodesLoading] = useState(false);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [graphOpen, setGraphOpen] = useState(false);

  const categories = Array.from(
    new Set(pages.map(p => p.category).filter((c): c is string => !!c))
  );

  const filtered = pages
    .filter(p => selectedCategory === null || p.category === selectedCategory)
    .filter(p => p.title.toLowerCase().includes(searchQuery.toLowerCase()));

  const loadPages = useCallback(async () => {
    setPagesLoading(true);
    setPagesError(null);
    try {
      const data = await fetchPagesList();
      setPages(data);
      if (data.length > 0 && !hasAutoSelected.current) {
        hasAutoSelected.current = true;
        router.replace(`/dashboard/memories?page=${data[0].id}`);
      }
    } catch (e: unknown) {
      setPagesError(e instanceof Error ? e.message : 'Failed to load pages');
    } finally {
      setPagesLoading(false);
    }
  }, [router]);

  const loadPageContent = useCallback(async (id: number) => {
    setContentLoading(true);
    setContentError(null);
    setNodes([]);
    setEdges([]);
    setNodesLoading(true);
    try {
      const data = await fetchPageById(id);
      setSelectedPage(data);
      try {
        const nodesData = await fetchNodesByPageId(id);
        setNodes(nodesData);
        try {
          const edgesData = await fetchEdgesByPageId(id);
          setEdges(edgesData);
        } catch {
          setEdges([]);
        }
      } catch {
        setNodes([]);
        setEdges([]);
      } finally {
        setNodesLoading(false);
      }
    } catch (e: unknown) {
      setContentError(e instanceof Error ? e.message : 'Failed to load page');
      setSelectedPage(null);
      setNodesLoading(false);
    } finally {
      setContentLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPages();
  }, [loadPages]);

  useEffect(() => {
    if (selectedId) {
      loadPageContent(Number(selectedId));
    }
  }, [selectedId, loadPageContent]);

  const handleSelectPage = (id: number) => {
    router.push(`/dashboard/memories?page=${id}`);
  };

  return (
    <>
      <div className="flex h-[calc(100vh-3.5rem)] lg:h-[calc(100vh-4rem)]">
        {/* Left Sidebar */}
        <aside className="hidden md:flex w-72 flex-col border-r border-border/60 bg-muted/20 backdrop-blur-sm">
          <div className="flex items-center gap-2 border-b border-border/60 px-4 py-3 bg-background/60">
            <BookOpen className="h-4 w-4 text-primary" />
            <h2 className="text-sm font-semibold tracking-tight">Memories</h2>
            <span className="ml-auto text-xs bg-primary/10 text-primary px-2 py-0.5 rounded-full font-semibold tabular-nums">
              {filtered.length}
            </span>
            <a href="/dashboard/memories/graph" className="text-xs text-muted-foreground/60 hover:text-primary flex items-center gap-1 transition-colors duration-150">
              <Network className="h-3 w-3" />
              Graph View
            </a>
          </div>

          <div className="px-3 py-2">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/70" />
              <input
                type="text"
                placeholder="Search memories..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full rounded-lg border border-border/60 bg-background py-2 pl-8 pr-3 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-150"
              />
            </div>
          </div>

          {categories.length > 0 && (
            <div className="flex gap-1.5 overflow-x-auto px-3 pb-2 scrollbar-none">
              <button
                onClick={() => setSelectedCategory(null)}
                className={`text-xs whitespace-nowrap rounded-full px-2.5 py-1 ${
                  selectedCategory === null
                    ? 'bg-accent text-foreground'
                    : 'bg-transparent text-muted-foreground/70 hover:text-foreground/80'
                }`}
              >
                All
              </button>
              {categories.map((cat) => (
                <button
                  key={cat}
                  onClick={() => setSelectedCategory(cat)}
                  className={`text-xs whitespace-nowrap rounded-full px-2.5 py-1 ${
                    selectedCategory === cat
                      ? 'bg-accent text-foreground'
                      : 'bg-transparent text-muted-foreground/70 hover:text-foreground/80'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          )}

          <div className="flex-1 overflow-y-auto">
            {pagesLoading && <SidebarSkeleton />}
            {pagesError && (
              <div className="p-4 text-sm text-red-400 flex items-center gap-2">
                <AlertCircle className="h-4 w-4" />
                {pagesError}
              </div>
            )}
            {!pagesLoading && !pagesError && pages.length === 0 && (
              <div className="p-4 text-sm text-muted-foreground text-center">
                No memories yet
              </div>
            )}
            {!pagesLoading && !pagesError && (
              <>
                {filtered.length === 0 && searchQuery && (
                  <div className="p-4 text-sm text-muted-foreground/70 text-center">No results</div>
                )}
                <div className="space-y-0.5 p-2">
                  {filtered.map((page) => {
                    const isActive = String(page.id) === selectedId;
                    return (
                      <button
                        key={page.id}
                        onClick={() => handleSelectPage(page.id)}
                         className={cn(
                          'w-full text-left text-sm transition-colors',
                          isActive
                            ? 'bg-primary/10 text-foreground border-l-2 border-primary pl-[10px] rounded-r-lg transition-all duration-150'
                            : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground pl-3 rounded-lg transition-all duration-150'
                        )}
                      >
                        <div className="font-medium truncate">{page.title}</div>
                         <div className="flex items-center gap-2 mt-0.5 text-xs text-muted-foreground/50 font-mono">
                          {page.source_count != null && (
                            <span>{page.source_count} sources</span>
                          )}
                          {page.source_count != null && page.updated_at && (
                            <span>·</span>
                          )}
                          <span>{formatRelativeTime(page.updated_at)}</span>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </>
            )}
          </div>
         </aside>

         {/* Main Panel */}
         <main className="flex-1 overflow-y-auto bg-background min-w-0">
           {contentLoading && <ContentSkeleton />}
           {contentError && (
             <div className="p-6 text-sm text-red-400 flex items-center gap-2">
               <AlertCircle className="h-4 w-4" />
               {contentError}
             </div>
           )}
           {!contentLoading && !contentError && selectedPage && (
             <div className="p-6 max-w-3xl">
                <h1 className="text-2xl font-bold tracking-tight">{selectedPage.title}</h1>
               <div className="flex items-center gap-3 mt-3 flex-wrap">
                 {selectedPage.source_count != null && (
                    <span className="inline-flex items-center gap-1 text-xs bg-primary/10 text-primary px-2.5 py-1 rounded-full font-medium border border-primary/20">
                      <FileText className="h-3 w-3" />
                      {selectedPage.source_count} sources
                    </span>
                 )}
                  {selectedPage.category && (
                    <span className="text-xs bg-muted text-muted-foreground/80 px-2.5 py-1 rounded-full border border-border/60 font-medium">
                      {selectedPage.category}
                    </span>
                  )}
                  <span className="text-xs bg-muted text-muted-foreground/80 px-2.5 py-1 rounded-full border border-border/60">
                    {selectedPage.is_sparse ? 'Sparse' : 'Full'}
                  </span>
                  {selectedPage.last_synth_at && (
                    <span className="text-xs text-muted-foreground/50 font-mono">
                      Synthed {new Date(selectedPage.last_synth_at).toLocaleDateString('en-GB', {
                        day: 'numeric', month: 'short', year: 'numeric',
                        hour: '2-digit', minute: '2-digit'
                      })}
                    </span>
                  )}
                  {selectedPage.project_id && (
                    <span className="text-xs text-muted-foreground/50 font-mono">
                      Project #{selectedPage.project_id}
                    </span>
                  )}
                 {nodes.length > 0 && (
                   <button
                     onClick={() => setGraphOpen(!graphOpen)}
                      className="text-xs text-muted-foreground hover:text-primary flex items-center gap-1 border border-border rounded-md px-2.5 py-1 ml-2 hover:border-primary/40 transition-all duration-150"
                   >
                     <GitFork className="h-3 w-3" />
                     {graphOpen ? 'Hide Graph' : 'Show Graph'}
                   </button>
                 )}
               </div>
               {(nodes.length > 0 || nodesLoading) && (
                 <div className="mt-4">
                    <span className="section-label">Linked Entities</span>
                   <div className="flex flex-wrap gap-1.5 mt-2">
                     {nodesLoading ? (
                       [...Array(3)].map((_, i) => (
                         <div key={i} className="h-5 w-16 rounded-full bg-muted animate-pulse" />
                       ))
                     ) : (
                       nodes.map((node) => {
                          const colorClasses: Record<string, string> = {
                            person: 'bg-blue-500/10 text-blue-600 border border-blue-500/20',
                            organization: 'bg-primary/10 text-primary border border-primary/20',
                            project: 'bg-violet-500/10 text-violet-600 border border-violet-500/20',
                            mission: 'bg-purple-500/10 text-purple-600 border border-purple-500/20',
                            task: 'bg-amber-500/10 text-amber-600 border border-amber-500/20',
                            concept: 'bg-muted text-muted-foreground border border-border',
                            emotional_state: 'bg-rose-500/10 text-rose-600 border border-rose-500/20',
                          };
                          const cls = colorClasses[node.type] || 'bg-muted text-muted-foreground border border-border';
                         return (
                           <span
                             key={node.id}
                             className={`text-xs px-2.5 py-0.5 rounded-full font-medium ${cls}`}
                           >
                             {node.label}
                           </span>
                         );
                       })
                     )}
                   </div>
                 </div>
               )}
                <div className="mt-6 border-t border-border/40 pt-6">
                {selectedPage.content ? (
                   <div className="prose prose-sm max-w-none prose-headings:font-semibold prose-headings:tracking-tight prose-h2:text-base prose-h3:text-sm prose-p:text-muted-foreground prose-p:leading-relaxed prose-strong:text-foreground prose-li:text-muted-foreground prose-code:text-primary prose-code:bg-primary/8 prose-code:rounded prose-code:px-1">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {selectedPage.content}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="text-sm text-muted-foreground italic py-8 text-center">
                    No summary available yet
                  </div>
                )}
              </div>
            </div>
          )}
          {!contentLoading && !contentError && !selectedPage && !pagesLoading && (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <div className="text-center">
                <BookOpen className="h-12 w-12 mx-auto mb-3 text-muted-foreground" />
                <p>Select a memory from the sidebar</p>
              </div>
            </div>
          )}
         </main>
         {graphOpen && nodes.length > 0 && (
            <aside className="hidden lg:flex w-80 flex-col border-l border-border/60 bg-muted/20">
              <div className="flex items-center justify-between px-4 py-3 border-b border-border/60 bg-background/60">
                <span className="text-sm font-semibold">Entity Graph</span>
                <button onClick={() => setGraphOpen(false)} className="text-muted-foreground/60 hover:text-primary">
                 <X className="h-4 w-4" />
               </button>
             </div>
             <div className="flex-1 flex items-start justify-center pt-4 overflow-hidden">
               <EgoGraph nodes={nodes} edges={edges} width={300} height={320} />
             </div>
              <div className="px-4 pb-4 text-xs text-muted-foreground/50 text-center font-mono">
               {nodes.length} entities · {edges.length} relationships
             </div>
           </aside>
         )}
       </div>
    </>
  );
}

export default function MemoriesPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-muted-foreground">Loading...</div>}>
      <MemoriesContent />
    </Suspense>
  );
}
