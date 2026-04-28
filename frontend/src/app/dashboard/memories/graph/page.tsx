'use client';

import dynamic from 'next/dynamic';
import { useEffect, useState, useCallback } from 'react';
import { Loader2, AlertCircle, Network, ArrowLeft } from 'lucide-react';
import { cn } from '@/lib/utils';
import { fetchAllNodes, fetchAllEdges, fetchPagesList } from '@/lib/memories/api';
import { GraphNode, GraphEdge, CanonicalPageListItem } from '@/lib/memories/types';

const FullGraph = dynamic(() => import('@/components/memories/FullGraph'), { ssr: false });
const NodeFlyout = dynamic(() => import('@/components/memories/NodeFlyout'), { ssr: false });

export default function GraphPage() {
  const [allNodes, setAllNodes] = useState<GraphNode[]>([]);
  const [allEdges, setAllEdges] = useState<GraphEdge[]>([]);
  const [canonicalPages, setCanonicalPages] = useState<{ id: number; title: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);

  const [typeFilter, setTypeFilter] = useState<string[]>([]);
  const [relFilter, setRelFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState<string>('');

  const types = Array.from(new Set(allNodes.map(n => n.type)));
  const relationships = Array.from(new Set(allEdges.map(e => e.relationship)));

  const filteredNodes = allNodes.filter(n => {
    const matchType = typeFilter.length === 0 || typeFilter.includes(n.type);
    const matchSearch = n.label.toLowerCase().includes(searchQuery.toLowerCase());
    return matchType && matchSearch;
  });

  const filteredNodeIds = new Set(filteredNodes.map(n => n.id));
  const filteredEdges = allEdges.filter(e =>
    filteredNodeIds.has(e.source_node_id) &&
    filteredNodeIds.has(e.target_node_id) &&
    (relFilter === '' || e.relationship === relFilter)
  );

  const toggleType = (type: string) => {
    setTypeFilter(prev =>
      prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type]
    );
  };

  const handleNodeClick = useCallback((node: GraphNode | null) => {
    setSelectedNode(node);
  }, []);

  useEffect(() => {
    setLoading(true);
    Promise.all([fetchAllNodes(), fetchAllEdges(), fetchPagesList()])
      .then(([nodes, edges, pages]) => {
        setAllNodes(nodes);
        setAllEdges(edges);
        setCanonicalPages(pages.map(p => ({ id: p.id, title: p.title })));
        setError(null);
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : 'Failed to load graph data');
      })
      .finally(() => {
        setLoading(false);
      });
  }, []);

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] lg:h-[calc(100vh-4rem)]">
      {/* Toolbar */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-zinc-800 bg-zinc-900/80 flex-wrap">
        {/* Search */}
        <div className="relative">
          <input
            type="text"
            placeholder="Search nodes..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-48 rounded-lg border border-zinc-700 bg-zinc-800/60 py-1.5 pl-3 pr-3 text-sm text-zinc-100 placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-zinc-600"
          />
        </div>

        {/* Type filters */}
        <button
          onClick={() => setTypeFilter([])}
          className={`text-xs whitespace-nowrap rounded-full px-2.5 py-1 border ${
            typeFilter.length === 0
              ? 'bg-zinc-700 text-zinc-100 border-zinc-600'
              : 'bg-transparent text-zinc-500 border-zinc-700 hover:text-zinc-300'
          }`}
        >
          All Types
        </button>
        {types.map((type) => (
          <button
            key={type}
            onClick={() => toggleType(type)}
            className={`text-xs whitespace-nowrap rounded-full px-2.5 py-1 border flex items-center gap-1 ${
              typeFilter.includes(type)
                ? 'bg-zinc-700 text-zinc-100 border-zinc-600'
                : 'bg-transparent text-zinc-500 border-zinc-700 hover:text-zinc-300'
            }`}
          >
            <div
              className="h-2 w-2 rounded-full"
              style={{ backgroundColor: colorMap[type] || '#52525b' }}
            />
            {type}
          </button>
        ))}

        {/* Relationship filter */}
        <select
          value={relFilter}
          onChange={(e) => setRelFilter(e.target.value)}
          className="bg-zinc-800 border border-zinc-700 text-sm text-zinc-300 rounded-md px-2.5 py-1.5"
        >
          <option value="">All relationships</option>
          {relationships.map((rel) => (
            <option key={rel} value={rel}>{rel}</option>
          ))}
        </select>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Counts */}
        <span className="text-xs text-zinc-400">{filteredNodes.length} nodes</span>
        <span className="text-xs text-zinc-400">{filteredEdges.length} edges</span>

        {/* Back link */}
        <a href="/dashboard/memories" className="text-sm text-zinc-400 hover:text-zinc-200 flex items-center gap-1">
          <ArrowLeft className="h-4 w-4" />
          Memories
        </a>
      </div>

      {/* Graph canvas */}
      <div className="flex-1 relative bg-zinc-950">
        {loading && (
          <div className="flex items-center justify-center h-full">
            <Loader2 className="h-8 w-8 animate-spin text-zinc-400" />
          </div>
        )}
        {error && (
          <div className="flex items-center justify-center h-full">
            <div className="text-sm text-red-400 flex items-center gap-2">
              <AlertCircle className="h-4 w-4" />
              {error}
            </div>
          </div>
        )}
        {!loading && !error && filteredNodes.length === 0 && (
          <div className="flex items-center justify-center h-full text-zinc-500">
            No nodes match your filters
          </div>
        )}
        {!loading && !error && filteredNodes.length > 0 && (
          <FullGraph
            nodes={filteredNodes}
            edges={filteredEdges}
            onNodeClick={handleNodeClick}
          />
        )}
        <NodeFlyout
          node={selectedNode}
          edges={allEdges}
          allNodes={allNodes}
          canonicalPages={canonicalPages}
          onClose={() => setSelectedNode(null)}
        />
      </div>
    </div>
  );
}

const colorMap: Record<string, string> = {
  person: '#3b82f6',
  organization: '#14b8a6',
  project: '#8b5cf6',
  mission: '#a855f7',
  task: '#f59e0b',
  concept: '#71717a',
  emotional_state: '#f43f5e',
};
