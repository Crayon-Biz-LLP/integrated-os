'use client';

import { useState } from 'react';
import { X, Brain, ChevronDown, ChevronRight, Clock, Layers, ExternalLink, AlertCircle } from 'lucide-react';
import type { GraphNode } from '@/lib/memories/types';
import type { StreamItem } from '@/lib/memories/stream';

// ── colour map (mirrors NeuralDisc and NodeFlyout) ──────────────────────────
const TYPE_COLOUR: Record<string, string> = {
  person:          '#3b82f6',
  organization:    '#14b8a6',
  project:         '#8b5cf6',
  cluster:         '#a855f7',
  task:            '#f59e0b',
  concept:         '#71717a',
  emotional_state: '#f43f5e',
};

const TYPE_LABEL: Record<string, string> = {
  person:          'Person',
  organization:    'Organisation',
  project:         'Project',
  cluster:         'Cluster',
  task:            'Task',
  concept:         'Concept',
  emotional_state: 'Emotion',
};

const MEMORY_TYPE_BADGE: Record<string, string> = {
  note:           'bg-zinc-800 text-zinc-400',
  canonical_page: 'bg-teal-900/40 text-teal-300',
  task:           'bg-amber-900/40 text-amber-300',
  meeting:        'bg-blue-900/40 text-blue-300',
  call:           'bg-violet-900/40 text-violet-300',
  email:          'bg-emerald-900/40 text-emerald-300',
};

// ── helpers ──────────────────────────────────────────────────────────────────
function relativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1)  return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7)  return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}

function cleanContent(text: string): string {
  return text
    .replace(/\[.*?\]/g, '')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/__(.*?)__/g, '$1')
    .replace(/#\w+/g, '')
    .replace(/https?:\/\/\S+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

// ── sub-components ────────────────────────────────────────────────────────────

function MemoryCard({ item }: { item: StreamItem }) {
  const [expanded, setExpanded] = useState(false);
  const content = cleanContent(item.content);
  const preview = content.length > 160 ? content.slice(0, 160) + '…' : content;
  const hasMore = content.length > 160;
  const typeCls = (item.memory_type && MEMORY_TYPE_BADGE[item.memory_type])
    || 'bg-zinc-800 text-zinc-400';

  return (
    <div className="group rounded-lg border border-zinc-800/60 bg-zinc-900/60 hover:bg-zinc-900 hover:border-zinc-700/60 transition-all duration-150">
      {/* card header */}
      <div className="flex items-start justify-between gap-2 px-3 pt-3 pb-2">
        <div className="flex items-center gap-2 min-w-0">
          {item.memory_type && (
            <span className={`flex-shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded ${typeCls} capitalize`}>
              {item.memory_type.replace(/_/g, ' ')}
            </span>
          )}
          <span className="text-[10px] text-zinc-600 flex items-center gap-1 flex-shrink-0">
            <Clock className="h-2.5 w-2.5" />
            {relativeTime(item.created_at)}
          </span>
        </div>
        {hasMore && (
          <button
            onClick={() => setExpanded(e => !e)}
            className="flex-shrink-0 text-zinc-600 hover:text-zinc-300 transition-colors"
            aria-label={expanded ? 'Collapse memory' : 'Expand memory'}
          >
            {expanded
              ? <ChevronDown className="h-3.5 w-3.5" />
              : <ChevronRight className="h-3.5 w-3.5" />}
          </button>
        )}
      </div>

      {/* card body */}
      <p className="px-3 pb-3 text-xs text-zinc-300 leading-relaxed">
        {expanded ? content : preview}
      </p>
    </div>
  );
}

function SkeletonCard() {
  return (
    <div className="rounded-lg border border-zinc-800/40 bg-zinc-900/40 p-3 space-y-2 animate-pulse">
      <div className="flex gap-2">
        <div className="h-4 w-16 rounded bg-zinc-800" />
        <div className="h-4 w-12 rounded bg-zinc-800/60" />
      </div>
      <div className="h-3 w-full rounded bg-zinc-800/60" />
      <div className="h-3 w-4/5 rounded bg-zinc-800/40" />
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export interface MemoryDetailPanelProps {
  /** Selected graph node. Panel hidden when null. */
  node: GraphNode | null;
  /** Memory stream items for this node */
  items: StreamItem[];
  /** Whether memory data is loading */
  loading: boolean;
  /** Error message if fetch failed */
  error: string | null;
  /** Total edge count for this node (passed from graph) */
  connectionCount?: number;
  /** Close / dismiss the panel */
  onClose: () => void;
  /** Promote to ego-focus (explicit deep focus) */
  onFocusNode: (nodeId: string) => void;
}

export default function MemoryDetailPanel({
  node,
  items,
  loading,
  error,
  connectionCount,
  onClose,
  onFocusNode,
}: MemoryDetailPanelProps) {
  if (!node) return null;

  const colour = TYPE_COLOUR[node.type] || '#52525b';
  const typeLabel = TYPE_LABEL[node.type] || node.type;

  return (
    <div
      className="flex flex-col h-full bg-zinc-950/95 border-l border-zinc-800/80 backdrop-blur-sm"
      role="complementary"
      aria-label={`Memory detail: ${node.label}`}
    >
      {/* ── header ─────────────────────────────────────────────────────────── */}
      <div
        className="flex-shrink-0 px-4 pt-4 pb-3 border-b border-zinc-800/60"
        style={{ borderTopColor: colour, borderTopWidth: 2 }}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            {/* type chip */}
            <div className="flex items-center gap-2 mb-2">
              <span
                className="inline-block h-2 w-2 rounded-full flex-shrink-0"
                style={{ backgroundColor: colour }}
              />
              <span className="text-[10px] font-medium uppercase tracking-widest text-zinc-500">
                {typeLabel}
              </span>
            </div>

            {/* node label */}
            <h2 className="text-base font-semibold text-zinc-100 leading-tight break-words">
              {node.label}
            </h2>

            {/* subtitle: canonical page link or connection count */}
            {connectionCount !== undefined && (
              <p className="mt-1 text-xs text-zinc-500">
                {connectionCount} connection{connectionCount !== 1 ? 's' : ''}
              </p>
            )}
          </div>

          {/* close button */}
          <button
            onClick={onClose}
            className="flex-shrink-0 p-1 -mr-1 text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 rounded transition-colors"
            aria-label="Close panel"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ego-focus CTA */}
        <button
          onClick={() => onFocusNode(node.id)}
          className="mt-3 w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded bg-zinc-800/60 hover:bg-zinc-800 border border-zinc-700/50 hover:border-zinc-600/50 text-xs text-zinc-400 hover:text-zinc-200 transition-all duration-150"
          title="Recenter graph on this node"
        >
          <Layers className="h-3 w-3" />
          Focus graph here
          <ExternalLink className="h-2.5 w-2.5 opacity-50 ml-1" />
        </button>
      </div>

      {/* ── memory stream ──────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto overscroll-contain">
        {/* section heading */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-zinc-800/40 sticky top-0 bg-zinc-950/90 backdrop-blur-sm z-10">
          <Brain className="h-3.5 w-3.5 text-zinc-500" />
          <span className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">
            Linked Memories
          </span>
          {!loading && items.length > 0 && (
            <span className="ml-auto text-[10px] text-zinc-600 tabular-nums">
              {items.length}
            </span>
          )}
        </div>

        <div className="p-3 space-y-2">
          {/* loading skeletons */}
          {loading && (
            <>
              <SkeletonCard />
              <SkeletonCard />
              <SkeletonCard />
            </>
          )}

          {/* error state */}
          {!loading && error && (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <AlertCircle className="h-6 w-6 text-zinc-600" />
              <p className="text-xs text-zinc-500">{error}</p>
            </div>
          )}

          {/* empty state */}
          {!loading && !error && items.length === 0 && (
            <div className="flex flex-col items-center gap-3 py-8 text-center px-4">
              <div
                className="h-10 w-10 rounded-full flex items-center justify-center"
                style={{ backgroundColor: `${colour}18` }}
              >
                <Brain className="h-5 w-5" style={{ color: colour }} />
              </div>
              <p className="text-xs text-zinc-500 max-w-[180px] leading-relaxed">
                No memories indexed for this node yet.
              </p>
            </div>
          )}

          {/* memory cards */}
          {!loading && !error && items.map((item) => (
            <MemoryCard key={item.id} item={item} />
          ))}
        </div>
      </div>

      {/* ── footer branding ────────────────────────────────────────────────── */}
      <div className="flex-shrink-0 px-4 py-2 border-t border-zinc-800/40">
        <p className="text-[9px] text-zinc-700 select-none tracking-wider uppercase">
          Rhodey OS · Knowledge Graph
        </p>
      </div>
    </div>
  );
}
