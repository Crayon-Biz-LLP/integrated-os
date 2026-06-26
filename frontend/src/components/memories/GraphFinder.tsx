'use client';

import { useRef, useEffect, useCallback, useState, useMemo } from 'react';
import { Clock, FileText, ChevronDown, ChevronRight, Hash, Search, Database, Link2, Link2Off, ArrowDownAZ, ArrowUpAZ, Component, BarChart2 } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import type { Episode } from '@/lib/memories/stream';
import type { GraphNode } from '@/lib/memories/types';

interface GraphFinderProps {
  episodes: Episode[];
  loading: boolean;
  allNodes?: GraphNode[];
  focusedNode?: GraphNode | null;
  expandedEpisodeId: string | null;
  expandedMemoryId: number | null;
  selectedNodeId: string | null;
  onToggleEpisode: (episode: Episode) => void;
  onMemoryClick: (memoryId: number) => void;
  onLoadMore: () => void;
  onNavigateNode?: (nodeId: string) => void;
  graphLoading?: boolean;
  entityTypeFilter?: string | null;
  onFilterByType?: (type: string | null) => void;
  graphLinked?: boolean;
  onToggleGraphLinked?: () => void;
}

type TabType = 'all' | 'people' | 'organizations' | 'projects' | 'concepts' | 'clusters' | 'tasks';

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

const TYPE_COLOUR: Record<string, string> = {
  person:          '#3b82f6',
  organization:    '#14b8a6',
  project:         '#8b5cf6',
  cluster:         '#a855f7',
  task:            '#f59e0b',
  concept:         '#71717a',
  emotional_state: '#f43f5e',
};

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
  isSelected,
  onToggle,
  onMemoryClick,
}: {
  episode: Episode;
  isExpanded: boolean;
  expandedMemoryId: number | null;
  isSelected: boolean;
  onToggle: () => void;
  onMemoryClick: (memoryId: number) => void;
}) {
  return (
    <div className={`border-b border-zinc-800/50 transition-colors ${isSelected ? 'bg-zinc-800/20' : ''}`}>
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

export default function GraphFinder({
  episodes,
  loading,
  allNodes = [],
  focusedNode,
  expandedEpisodeId,
  expandedMemoryId,
  selectedNodeId,
  onToggleEpisode,
  onMemoryClick,
  onLoadMore,
  onNavigateNode,
  graphLoading,
  entityTypeFilter,
  onFilterByType,
  graphLinked,
  onToggleGraphLinked,
}: GraphFinderProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [activeTab, setActiveTab] = useState<TabType>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [showEntityExplorer, setShowEntityExplorer] = useState(true);
  const [sortMode, setSortMode] = useState<'alpha-asc' | 'alpha-desc' | 'type' | 'connections'>('alpha-asc');

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

  // Derived stats
  const typeCounts = useMemo(() => {
    const counts = { person: 0, organization: 0, project: 0, concept: 0, other: 0 };
    allNodes.forEach(n => {
      if (n.type === 'person') counts.person++;
      else if (n.type === 'organization') counts.organization++;
      else if (n.type === 'project') counts.project++;
      else if (n.type === 'concept' || n.type === 'emotional_state') counts.concept++;
      else counts.other++;
    });
    return counts;
  }, [allNodes]);

  const filteredEpisodes = useMemo(() => {
    return episodes.filter(ep => {
      // Filter by search
      if (searchQuery) {
        const q = searchQuery.toLowerCase();
        const matchesTitle = ep.title?.toLowerCase().includes(q);
        const matchesSummary = ep.summary?.toLowerCase().includes(q);
        const matchesEntity = ep.entities.some(e => e.label.toLowerCase().includes(q));
        if (!matchesTitle && !matchesSummary && !matchesEntity) return false;
      }
      
      // Filter by tab
      if (activeTab === 'all') return true;
      
      return ep.entities.some(e => {
        if (activeTab === 'people') return e.type === 'person';
        if (activeTab === 'organizations') return e.type === 'organization';
        if (activeTab === 'projects') return e.type === 'project';
        if (activeTab === 'concepts') return e.type === 'concept' || e.type === 'emotional_state';
        if (activeTab === 'clusters') return e.type === 'cluster';
        if (activeTab === 'tasks') return e.type === 'task';
        return false;
      });
    });
  }, [episodes, activeTab, searchQuery]);

  return (
    <div className="flex flex-col h-full bg-zinc-950 border-r border-zinc-800">
      
      {/* ── top section: search, breadcrumb, stats ───────────────────────────── */}
      <div className="px-4 pt-3 pb-2 border-b border-zinc-800/80 bg-zinc-950/80 backdrop-blur-md z-10 shrink-0">
        
        {/* Breadcrumb */}
        <div className="flex items-center gap-1.5 mb-3 text-[10px] uppercase tracking-widest text-zinc-500 font-semibold h-4">
          <Database className="h-3 w-3" />
          <span>Graph</span>
          {focusedNode && (
            <>
              <ChevronRight className="h-2.5 w-2.5 opacity-50" />
              <span className="text-zinc-300 truncate max-w-[150px]">{focusedNode.label}</span>
            </>
          )}
        </div>

        <div className="relative mb-3">
          <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-zinc-500" />
          <input 
            type="text" 
            placeholder="Search graph..."
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            className="w-full bg-zinc-900 border border-zinc-800/80 rounded-md pl-8 pr-3 py-1.5 text-xs text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-700 transition-colors"
          />
        </div>
        
        {/* Stats Bar */}
        <div className="flex items-center justify-between mb-3 px-1">
          <div className="flex items-center gap-2 text-[9px] text-zinc-500 uppercase tracking-widest font-semibold">
            <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-blue-500/80"></span>{typeCounts.person}</span>
            <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-teal-500/80"></span>{typeCounts.organization}</span>
            <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-purple-500/80"></span>{typeCounts.project}</span>
            <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-zinc-500/80"></span>{typeCounts.concept}</span>
          </div>
          <button 
            onClick={() => setShowEntityExplorer(s => !s)}
            className="text-[9px] uppercase tracking-widest font-semibold text-zinc-400 hover:text-zinc-200 transition-colors bg-zinc-800/50 px-1.5 py-0.5 rounded border border-zinc-700/50"
          >
            {showEntityExplorer ? 'Hide Entities' : 'All Entities'}
          </button>
        </div>

        <div className="flex gap-1 overflow-x-auto pb-1 scrollbar-hide">
          {(['all', 'people', 'organizations', 'projects', 'concepts', 'clusters', 'tasks'] as TabType[]).map(tab => {
            const isActive = activeTab === tab;
            const isFilteredInGraph = showEntityExplorer && graphLinked && isActive && tab !== 'all';
            return (
              <button
                key={tab}
                onClick={() => {
                  setActiveTab(tab);
                  if (showEntityExplorer && onFilterByType && graphLinked) {
                    if (tab === 'all') onFilterByType(null);
                    else if (tab === 'people') onFilterByType('person');
                    else if (tab === 'organizations') onFilterByType('organization');
                    else if (tab === 'projects') onFilterByType('project');
                    else if (tab === 'concepts') onFilterByType('concept');
                    else if (tab === 'clusters') onFilterByType('cluster');
                    else if (tab === 'tasks') onFilterByType('task');
                  }
                }}
                className={`text-[10px] uppercase tracking-wider font-semibold px-2 py-1 rounded transition-colors whitespace-nowrap border ${
                  isActive 
                    ? isFilteredInGraph
                      ? 'bg-zinc-800 text-teal-400 border-teal-500/30'
                      : 'bg-zinc-800 text-zinc-200 border-zinc-700' 
                    : 'text-zinc-500 border-transparent hover:text-zinc-300 hover:bg-zinc-800/50'
                }`}
              >
                {tab}
              </button>
            );
          })}
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        {showEntityExplorer ? (
          /* ── Entity Explorer Mode ───────────────────────────────────────────── */
          <div className="p-3">
            <div className="flex items-center justify-between mb-3 pl-1 pr-0.5">
              <div className="text-xs text-zinc-500">Every entity in view</div>
              <div className="flex items-center gap-1.5">
                {/* Sort control */}
                <select 
                  value={sortMode}
                  onChange={(e) => setSortMode(e.target.value as any)}
                  className="bg-zinc-900 border border-zinc-800 text-[10px] text-zinc-400 rounded px-1.5 py-0.5 outline-none focus:border-zinc-700"
                >
                  <option value="alpha-asc">A–Z</option>
                  <option value="alpha-desc">Z–A</option>
                  <option value="type">By Type</option>
                </select>

                {/* Graph Link Toggle */}
                {onToggleGraphLinked && (
                  <button
                    onClick={onToggleGraphLinked}
                    className={`flex items-center justify-center p-1 rounded border transition-colors ${
                      graphLinked 
                        ? 'bg-teal-500/10 border-teal-500/30 text-teal-400 hover:bg-teal-500/20' 
                        : 'bg-zinc-800/50 border-zinc-700/50 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300'
                    }`}
                    title={graphLinked ? 'Filter linked to graph' : 'Filter only affects list'}
                  >
                    {graphLinked ? <Link2 className="h-3.5 w-3.5" /> : <Link2Off className="h-3.5 w-3.5" />}
                  </button>
                )}
              </div>
            </div>
            
            {graphLoading && allNodes.length === 0 ? (
              <div className="flex flex-wrap gap-1.5 px-1">
                {[...Array(12)].map((_, i) => (
                  <div key={i} className="h-6 w-24 rounded bg-zinc-900/50 border border-zinc-800/50 animate-pulse" />
                ))}
              </div>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {allNodes
                  .filter(n => searchQuery ? n.label.toLowerCase().includes(searchQuery.toLowerCase()) : true)
                  .filter(n => {
                    if (activeTab === 'all') return true;
                    if (activeTab === 'people') return n.type === 'person';
                    if (activeTab === 'organizations') return n.type === 'organization';
                    if (activeTab === 'projects') return n.type === 'project';
                    if (activeTab === 'concepts') return n.type === 'concept' || n.type === 'emotional_state';
                    if (activeTab === 'clusters') return n.type === 'cluster';
                    if (activeTab === 'tasks') return n.type === 'task';
                    return true;
                  })
                  .sort((a, b) => {
                    if (sortMode === 'alpha-asc') return a.label.localeCompare(b.label);
                    if (sortMode === 'alpha-desc') return b.label.localeCompare(a.label);
                    if (sortMode === 'type') {
                      if (a.type !== b.type) return a.type.localeCompare(b.type);
                      return a.label.localeCompare(b.label);
                    }
                    return 0;
                  })
                  .map(n => (
                  <button
                    key={n.id}
                    onClick={() => onNavigateNode?.(n.id)}
                    className={`flex items-center gap-1.5 px-2 py-1 rounded text-[10px] border transition-colors ${
                      selectedNodeId === n.id 
                        ? 'bg-zinc-800 border-zinc-600 text-zinc-100' 
                        : 'bg-zinc-900 border-zinc-800/80 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200'
                    }`}
                  >
                    <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: TYPE_COLOUR[n.type] || '#52525b' }} />
                    <span className="truncate max-w-[140px]">{n.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          /* ── Episode Stream Mode ───────────────────────────────────────────── */
          <>
            {loading && filteredEpisodes.length === 0 && (
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
          {filteredEpisodes.map((ep) => (
            <EpisodeCard
              key={ep.id}
              episode={ep}
              isExpanded={ep.id === expandedEpisodeId}
              expandedMemoryId={expandedMemoryId}
              isSelected={selectedNodeId !== null && ep.graph_node_ids.includes(selectedNodeId)}
              onToggle={() => onToggleEpisode(ep)}
              onMemoryClick={onMemoryClick}
            />
          ))}
        </div>

        {loading && filteredEpisodes.length > 0 && (
          <div className="flex justify-center py-4">
            <div className="h-4 w-4 rounded-full border border-zinc-600 border-t-transparent animate-spin" />
          </div>
        )}
          </>
        )}
      </div>
    </div>
  );
}
