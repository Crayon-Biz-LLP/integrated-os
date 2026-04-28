'use client';

import { GraphNode, GraphEdge } from '@/lib/memories/types';
import { X } from 'lucide-react';

interface NodeFlyoutProps {
  node: GraphNode | null;
  edges: GraphEdge[];
  allNodes: GraphNode[];
  canonicalPages: { id: number; title: string }[];
  onClose: () => void;
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

export default function NodeFlyout({ node, edges, allNodes, canonicalPages, onClose }: NodeFlyoutProps) {
  if (!node) return null;

  const color = colorMap[node.type] || '#52525b';

  const connections = edges.filter(e => e.source_node_id === node.id || e.target_node_id === node.id);

  const canonicalPage = node.canonical_page_id
    ? canonicalPages.find(p => p.id === node.canonical_page_id)
    : null;

  return (
    <div className="fixed right-0 top-0 h-full w-80 z-50 bg-zinc-900 border-l border-zinc-800 translate-x-0 transition-transform duration-300">
      <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-800">
        <div className="flex items-center gap-2">
          <div className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
          <span className="text-xs text-zinc-400 capitalize">{node.type}</span>
        </div>
        <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300">
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="p-4">
        <h2 className="text-lg font-bold text-zinc-100">{node.label}</h2>

        <div className="mt-4 pt-4 border-t border-zinc-800">
          {canonicalPage && (
            <>
              <p className="text-xs text-zinc-500 uppercase tracking-wide mb-2">Linked Canonical Page</p>
              <span className="text-xs bg-teal-500/15 text-teal-300 border border-teal-500/30 px-2.5 py-1 rounded-full">
                {canonicalPage.title}
              </span>
            </>
          )}
        </div>

        <div className="mt-4 pt-4 border-t border-zinc-800">
          <p className="text-xs text-zinc-500 uppercase tracking-wide mb-2">Connections</p>
          {connections.length === 0 ? (
            <p className="text-sm text-zinc-500">No connections</p>
          ) : (
            <div className="space-y-1">
              {connections.map((edge) => {
                const isOutgoing = edge.source_node_id === node.id;
                const otherNodeId = isOutgoing ? edge.target_node_id : edge.source_node_id;
                const otherNode = allNodes.find(n => n.id === otherNodeId);
                if (!otherNode) return null;
                return (
                  <div key={edge.id} className="flex items-center gap-2 py-1.5 text-sm">
                    <span className="text-xs bg-zinc-800 px-2 py-0.5 rounded text-zinc-400">
                      {edge.relationship}
                    </span>
                    <span className="text-zinc-600">{isOutgoing ? '→' : '←'}</span>
                    <span className="text-zinc-300">{otherNode.label}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="mt-6 pt-4 border-t border-zinc-800">
          <p className="text-xs text-zinc-600">Node ID: {node.id}</p>
        </div>
      </div>
    </div>
  );
}
