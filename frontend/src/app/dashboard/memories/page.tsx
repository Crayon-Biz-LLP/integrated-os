'use client';

import { useEffect, useState, useCallback, Suspense, useRef } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BookOpen, Loader2, AlertCircle, FileText, Search } from 'lucide-react';
import { cn } from '@/lib/utils';
import { fetchPagesList, fetchPageById } from '@/lib/memories/api';
import { CanonicalPage, CanonicalPageListItem } from '@/lib/memories/types';

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
        <div key={i} className="h-12 rounded-lg bg-zinc-800/50 animate-pulse" />
      ))}
    </div>
  );
}

function ContentSkeleton() {
  return (
    <div className="p-6 space-y-4">
      <div className="h-8 w-64 rounded bg-zinc-800/50 animate-pulse" />
      <div className="flex gap-2">
        <div className="h-5 w-20 rounded bg-zinc-800/50 animate-pulse" />
        <div className="h-5 w-32 rounded bg-zinc-800/50 animate-pulse" />
      </div>
      <div className="space-y-2 mt-6">
        {[...Array(8)].map((_, i) => (
          <div key={i} className="h-4 w-full rounded bg-zinc-800/50 animate-pulse" />
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
    try {
      const data = await fetchPageById(id);
      setSelectedPage(data);
    } catch (e: unknown) {
      setContentError(e instanceof Error ? e.message : 'Failed to load page');
      setSelectedPage(null);
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
      <style jsx global>{`
        .markdown-body h2 {
          font-size: 1.25rem;
          font-weight: 600;
          margin-top: 1.5rem;
          margin-bottom: 0.75rem;
          padding-bottom: 0.5rem;
          border-bottom: 1px solid rgb(63, 63, 70);
        }
        .markdown-body h3 {
          font-size: 1.1rem;
          font-weight: 600;
          margin-top: 1.25rem;
          margin-bottom: 0.5rem;
        }
        .markdown-body ul, .markdown-body ol {
          padding-left: 1.5rem;
          margin: 0.75rem 0;
        }
        .markdown-body ul { list-style-type: disc; }
        .markdown-body ol { list-style-type: decimal; }
        .markdown-body li { margin: 0.25rem 0; }
        .markdown-body strong { font-weight: 600; color: rgb(244, 244, 245); }
        .markdown-body p { margin: 0.75rem 0; }
        .markdown-body code {
          background: rgb(39, 39, 42);
          padding: 0.125rem 0.375rem;
          border-radius: 0.25rem;
          font-size: 0.875rem;
        }
      `}</style>
      <div className="flex h-[calc(100vh-3.5rem)] lg:h-[calc(100vh-4rem)]">
        {/* Left Sidebar */}
        <aside className="hidden md:flex w-72 flex-col border-r border-zinc-800 bg-zinc-900/50">
          <div className="flex items-center gap-2 border-b border-zinc-800 px-4 py-3">
            <BookOpen className="h-4 w-4 text-muted-foreground" />
            <h2 className="text-sm font-semibold">Memories</h2>
            <span className="ml-auto text-xs text-muted-foreground">{filtered.length}</span>
          </div>

          <div className="px-3 py-2">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
              <input
                type="text"
                placeholder="Search memories..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800/60 py-2 pl-8 pr-3 text-sm text-zinc-100 placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-zinc-600"
              />
            </div>
          </div>

          {categories.length > 0 && (
            <div className="flex gap-1.5 overflow-x-auto px-3 pb-2 scrollbar-none">
              <button
                onClick={() => setSelectedCategory(null)}
                className={`text-xs whitespace-nowrap rounded-full px-2.5 py-1 ${
                  selectedCategory === null
                    ? 'bg-zinc-700 text-zinc-100'
                    : 'bg-transparent text-zinc-500 hover:text-zinc-300'
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
                      ? 'bg-zinc-700 text-zinc-100'
                      : 'bg-transparent text-zinc-500 hover:text-zinc-300'
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
                  <div className="p-4 text-sm text-zinc-500 text-center">No results</div>
                )}
                <div className="space-y-0.5 p-2">
                  {filtered.map((page) => {
                    const isActive = String(page.id) === selectedId;
                    return (
                      <button
                        key={page.id}
                        onClick={() => handleSelectPage(page.id)}
                        className={cn(
                          'w-full text-left px-3 py-2.5 rounded-lg text-sm transition-colors',
                          isActive
                            ? 'bg-accent text-accent-foreground'
                            : 'text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-100'
                        )}
                      >
                        <div className="font-medium truncate">{page.title}</div>
                        <div className="flex items-center gap-2 mt-0.5 text-xs text-zinc-500">
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
        <main className="flex-1 overflow-y-auto bg-background">
          {contentLoading && <ContentSkeleton />}
          {contentError && (
            <div className="p-6 text-sm text-red-400 flex items-center gap-2">
              <AlertCircle className="h-4 w-4" />
              {contentError}
            </div>
          )}
          {!contentLoading && !contentError && selectedPage && (
            <div className="p-6 max-w-3xl">
              <h1 className="text-2xl font-bold">{selectedPage.title}</h1>
              <div className="flex items-center gap-3 mt-3 flex-wrap">
                {selectedPage.source_count != null && (
                  <span className="inline-flex items-center gap-1 text-xs bg-zinc-800 text-zinc-300 px-2.5 py-1 rounded-full">
                    <FileText className="h-3 w-3" />
                    {selectedPage.source_count} sources
                  </span>
                )}
                {selectedPage.category && (
                  <span className="text-xs bg-zinc-800 text-zinc-400 px-2.5 py-1 rounded-full">
                    {selectedPage.category}
                  </span>
                )}
                <span className="text-xs bg-zinc-800 text-zinc-400 px-2.5 py-1 rounded-full">
                  {selectedPage.is_sparse ? 'Sparse' : 'Full'}
                </span>
                {selectedPage.last_synth_at && (
                  <span className="text-xs text-muted-foreground">
                    Synthed {new Date(selectedPage.last_synth_at).toLocaleDateString('en-GB', {
                      day: 'numeric', month: 'short', year: 'numeric',
                      hour: '2-digit', minute: '2-digit'
                    })}
                  </span>
                )}
                {selectedPage.project_id && (
                  <span className="text-xs text-muted-foreground">
                    Project #{selectedPage.project_id}
                  </span>
                )}
              </div>
              <div className="mt-6 border-t border-zinc-800 pt-6">
                {selectedPage.content ? (
                  <div className="text-sm leading-relaxed text-zinc-300 markdown-body">
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
                <BookOpen className="h-12 w-12 mx-auto mb-3 text-zinc-700" />
                <p>Select a memory from the sidebar</p>
              </div>
            </div>
          )}
        </main>
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
