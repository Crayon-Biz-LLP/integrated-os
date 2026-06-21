'use client';

import { useRef, useEffect, useCallback } from 'react';
import { Clock, FileText } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import type { StreamItem } from '@/lib/memories/stream';

interface LifeStreamProps {
  items: StreamItem[];
  loading: boolean;
  selectedItemId: number | null;
  onSelectItem: (item: StreamItem) => void;
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

export default function LifeStream({
  items,
  loading,
  selectedItemId,
  onSelectItem,
  onLoadMore,
}: LifeStreamProps) {
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
        <p className="text-xs text-zinc-500 mt-0.5">Chronological memory narrative</p>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        {loading && items.length === 0 && (
          <div className="p-4 space-y-3">
            {[...Array(5)].map((_, i) => (
              <div key={i} className="animate-pulse space-y-2">
                <div className="h-4 w-3/4 rounded bg-zinc-800" />
                <div className="h-3 w-1/2 rounded bg-zinc-800/60" />
                <div className="h-12 w-full rounded bg-zinc-800/40" />
              </div>
            ))}
          </div>
        )}

        {!loading && items.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full text-zinc-500 px-4">
            <FileText className="h-8 w-8 mb-2 opacity-40" />
            <p className="text-sm">No memories yet</p>
          </div>
        )}

        <div className="divide-y divide-zinc-800/50 relative">
          <AnimatePresence mode="popLayout">
            {items.map((item) => {
              const isSelected = item.id === selectedItemId;
              const cleanContent = stripMetadata(item.content);

              return (
                <motion.button
                  layout="position"
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  transition={{ duration: 0.2 }}
                  key={item.id}
                  onClick={() => onSelectItem(item)}
                  className={`w-full text-left px-4 py-3 transition-colors ${
                    isSelected
                      ? 'bg-zinc-800/50 border-l-2 border-teal-500'
                      : 'hover:bg-zinc-800/30 border-l-2 border-transparent'
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="text-sm font-medium text-zinc-200 line-clamp-1">
                      {item.title}
                    </h3>
                    <span className="text-[10px] text-zinc-500 whitespace-nowrap mt-0.5 flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      {relativeTime(item.updated_at)}
                    </span>
                  </div>

                  {item.category && (
                    <span className="inline-block text-[10px] text-zinc-500 bg-zinc-800/60 rounded px-1.5 py-0.5 mt-1">
                      {item.category}
                    </span>
                  )}

                  {cleanContent && (
                    <p className="text-xs text-zinc-400 mt-1.5 line-clamp-3 leading-relaxed">
                      {cleanContent.slice(0, 280)}
                      {cleanContent.length > 280 ? '...' : ''}
                    </p>
                  )}
                </motion.button>
              );
            })}
          </AnimatePresence>
        </div>

        {loading && items.length > 0 && (
          <div className="flex justify-center py-4">
            <div className="h-4 w-4 rounded-full border border-zinc-600 border-t-transparent animate-spin" />
          </div>
        )}
      </div>
    </div>
  );
}
