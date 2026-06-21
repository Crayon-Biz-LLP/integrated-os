'use client';

import { useRef, useEffect, useCallback } from 'react';
import { Clock, FileText, ChevronDown, ChevronRight, Hash } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import type { Episode } from '@/lib/memories/stream';

interface EpisodeStreamProps {
  episodes: Episode[];
  loading: boolean;
  expandedEpisodeId: string | null;
  expandedMemoryId: number | null;
  onToggleEpisode: (episode: Episode) => void;
  onMemoryClick: (memoryId: number) => void;
  onLoadMore: () => void;
}

function relativeTime(dateStr: string | null): string {
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

function stripMetadata(text: string | null): string {
  if (!text) return '';
  return text
    .replace(/\[.*?\]/g, '')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/__(.*?)__/g, '$1')
    .replace(/#\w+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function typeColor(type: string | undefined): string {
  switch (type) {
    case 'person': return 'bg-teal-500/20 text-teal-300 border-teal-700/40';
    case 'organization': return 'bg-purple-500/20 text-purple-300 border-purple-700/40';
    case 'project': return 'bg-blue-500/20 text-blue-300 border-blue-700/40';
    case 'place': return 'bg-amber-500/20 text-amber-300 border-amber-700/40';
    case 'cluster': return 'bg-pink-500/20 text-pink-300 border-pink-700/40';
    default: return 'bg-zinc-700/40 text-zinc-400 border-zinc-600/40';
  }
}

function EpisodeCard({
  episode,
  isExpanded,
  expandedMemoryId,
  onToggle,
  onMemoryClick,
}: {
  episode: Episode;
  isExpanded: boolean;
  expandedMemoryId: number | null;
  onToggle: () => void;
  onMemoryClick: (memoryId: number) => void;
}) {
  return (
    <div className="border-b border-zinc-800/50">
      <motion.button
        layout="position"
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
        onClick={onToggle}
        className="w-full text-left px-4 py-3 hover:bg-zinc-800/30 transition-colors"
      >
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              {isExpanded ? (
                <ChevronDown className="h-3.5 w-3.5 text-zinc-500 flex-shrink-0" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5 text-zinc-500 flex-shrink-0" />
              )}
              <h3 className="text-sm font-medium text-zinc-200 truncate">
                {episode.title}
              </h3>
            </div>

            <p className="text-xs text-zinc-400 mt-1.5 leading-relaxed line-clamp-2 pl-6">
              {episode.summary}
            </p>

            <div className="flex items-center gap-2 mt-2 pl-6 flex-wrap">
              {episode.entities.length > 0 && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  {episode.entities.slice(0, 4).map((e) => (
                    <span
                      key={e.id}
                      className={`text-[10px] px-1.5 py-0.5 rounded border ${typeColor(e.type)} truncate max-w-28`}
                    >
                      {e.label}
                    </span>
                  ))}
                  {episode.entities.length > 4 && (
                    <span className="text-[10px] text-zinc-500">
                      +{episode.entities.length - 4}
                    </span>
                  )}
                </div>
              )}

              {episode.count > 1 && (
                <span className="text-[10px] text-zinc-500 flex items-center gap-1 bg-zinc-800/60 px-1.5 py-0.5 rounded">
                  <Hash className="h-2.5 w-2.5" />
                  {episode.count}
                </span>
              )}

              <span className="text-[10px] text-zinc-500 flex items-center gap-1 ml-auto">
                <Clock className="h-3 w-3" />
                {relativeTime(episode.timestamp)}
              </span>
            </div>
          </div>
        </div>
      </motion.button>

      {/* Expanded raw memories */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="border-t border-zinc-800/30 mx-4" />
            {episode.memories.map((mem) => {
              const isMemSelected = mem.id === expandedMemoryId;
              const cleanContent = stripMetadata(mem.content);
              return (
                <button
                  key={mem.id}
                  onClick={(e) => {
                    e.stopPropagation();
                    onMemoryClick(mem.id);
                  }}
                  className={`w-full text-left px-8 py-2.5 transition-colors ${
                    isMemSelected
                      ? 'bg-teal-500/10 border-l-2 border-teal-500'
                      : 'hover:bg-zinc-800/20 border-l-2 border-transparent'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    {mem.memory_type && (
                      <span className="text-[10px] text-zinc-500 bg-zinc-800/60 rounded px-1.5 py-0.5 capitalize">
                        {mem.memory_type.replace('_', ' ')}
                      </span>
                    )}
                    <span className="text-[10px] text-zinc-600">
                      {relativeTime(mem.created_at)}
                    </span>
                  </div>
                  <p className="text-xs text-zinc-400 line-clamp-2 leading-relaxed">
                    {cleanContent.slice(0, 200)}
                    {cleanContent.length > 200 ? '...' : ''}
                  </p>
                </button>
              );
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function EpisodeStream({
  episodes,
  loading,
  expandedEpisodeId,
  expandedMemoryId,
  onToggleEpisode,
  onMemoryClick,
  onLoadMore,
}: EpisodeStreamProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el || loading) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 200) {
      onLoadMore();
    }
  }, [loading, onLoadMore]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.addEventListener('scroll', handleScroll);
    return () => el.removeEventListener('scroll', handleScroll);
  }, [handleScroll]);

  return (
    <div className="flex flex-col h-full bg-zinc-950 border-r border-zinc-800">
      <div className="px-4 py-3 border-b border-zinc-800">
        <h2 className="text-sm font-semibold text-zinc-100">Life Stream</h2>
        <p className="text-xs text-zinc-500 mt-0.5">Grouped memory episodes</p>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        {loading && episodes.length === 0 && (
          <div className="p-4 space-y-3">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="animate-pulse space-y-2">
                <div className="h-4 w-2/3 rounded bg-zinc-800" />
                <div className="h-3 w-full rounded bg-zinc-800/60" />
                <div className="h-3 w-1/3 rounded bg-zinc-800/40" />
              </div>
            ))}
          </div>
        )}

        {!loading && episodes.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-zinc-500 px-4">
            <FileText className="h-8 w-8 mb-2 opacity-40" />
            <p className="text-sm">No episodes yet</p>
          </div>
        )}

        <div className="relative">
          {episodes.map((ep) => (
            <EpisodeCard
              key={ep.id}
              episode={ep}
              isExpanded={ep.id === expandedEpisodeId}
              expandedMemoryId={expandedMemoryId}
              onToggle={() => onToggleEpisode(ep)}
              onMemoryClick={onMemoryClick}
            />
          ))}
        </div>

        {loading && episodes.length > 0 && (
          <div className="flex justify-center py-4">
            <div className="h-4 w-4 rounded-full border border-zinc-600 border-t-transparent animate-spin" />
          </div>
        )}
      </div>
    </div>
  );
}
