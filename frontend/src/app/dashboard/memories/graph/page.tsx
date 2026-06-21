'use client';

import dynamic from 'next/dynamic';
import { useEffect, useState, useCallback, useRef } from 'react';
import { Loader2, AlertCircle, ArrowLeft, Info } from 'lucide-react';
import { fetchNeighborhood, fetchMemoryStream } from '@/lib/memories/stream';
import type { StreamItem, NeighborhoodResponse } from '@/lib/memories/stream';
import type { GraphNode, GraphEdge } from '@/lib/memories/types';

const LifeStream = dynamic(() => import('@/components/memories/LifeStream'), { ssr: false });
const NeuralDisc = dynamic(() => import('@/components/memories/NeuralDisc'), { ssr: false });

export default function MemoryGraphPage() {
  const [streamItems, setStreamItems] = useState<StreamItem[]>([]);
  const [streamLoading, setStreamLoading] = useState(true);
  const [focusedMemoryId, setFocusedMemoryId] = useState<number | null>(null);

  const [graphNodes, setGraphNodes] = useState<GraphNode[]>([]);
  const [graphEdges, setGraphEdges] = useState<GraphEdge[]>([]);
  const [focusedNodeId, setFocusedNodeId] = useState<number | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [graphInfo, setGraphInfo] = useState<string | null>(null);

  // Graphics context recovery
  const [discKey, setDiscKey] = useState(0);

  // Diagnostics
  const [diagnostics, setDiagnostics] = useState({ fetch: 0, layout: 0, render: 0, hover: 0, total: 0 });
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [enableEffects, setEnableEffects] = useState(true);

  const streamLimit = 20;

  // Abort controllers for in-flight requests
  const streamAbortRef = useRef<AbortController | null>(null);
  const graphAbortRef = useRef<AbortController | null>(null);
  
  // Sequence guards against stale responses resolving out of order
  const sequenceRef = useRef(0);

  const loadStream = useCallback(async (nodeId?: number, reqSeq?: number) => {
    if (streamAbortRef.current) streamAbortRef.current.abort();
    const abortController = new AbortController();
    streamAbortRef.current = abortController;
    const currentSeq = reqSeq ?? sequenceRef.current;

    setStreamLoading(true);
    try {
      const res = await fetchMemoryStream(nodeId, streamLimit, abortController.signal);
      if (currentSeq !== sequenceRef.current) return;
      setStreamItems(res.items);
    } catch (e: any) {
      if (e.name === 'AbortError' || currentSeq !== sequenceRef.current) return;
      setStreamItems([]);
    } finally {
      if (streamAbortRef.current === abortController) {
        setStreamLoading(false);
      }
    }
  }, []);

  const loadMoreStream = useCallback(async () => {
    if (streamAbortRef.current) return; // Prevent concurrent loads
    const abortController = new AbortController();
    streamAbortRef.current = abortController;
    const currentSeq = sequenceRef.current;

    setStreamLoading(true);
    try {
      const res = await fetchMemoryStream(undefined, streamLimit + 10, abortController.signal);
      if (currentSeq !== sequenceRef.current) return;
      
      setStreamItems((prev) => {
        const existing = new Set(prev.map((i) => i.id));
        const newItems = res.items.filter((i) => !existing.has(i.id));
        return [...prev, ...newItems];
      });
    } catch (e: any) {
      if (e.name === 'AbortError' || currentSeq !== sequenceRef.current) return;
    } finally {
      if (streamAbortRef.current === abortController) {
        streamAbortRef.current = null;
        setStreamLoading(false);
      }
    }
  }, []);

  const loadNeighborhood = useCallback(async (nodeId: number | null, memoryId: number | null) => {
    const currentSeq = ++sequenceRef.current;
    
    if (graphAbortRef.current) graphAbortRef.current.abort();
    const abortController = new AbortController();
    graphAbortRef.current = abortController;

    setGraphLoading(true);
    setGraphError(null);
    setGraphInfo(null);
    const start = performance.now();

    try {
      let res;
      if (nodeId !== null) {
        res = await fetchNeighborhood(nodeId, abortController.signal);
      } else if (memoryId !== null) {
        const fetchRes = await fetch(`/api/graph/neighborhood?memory_id=${memoryId}`, { signal: abortController.signal });
        if (fetchRes.status === 404) {
          throw new Error('ORPHAN_MEMORY');
        }
        if (!fetchRes.ok) throw new Error('Failed to fetch neighborhood');
        res = await fetchRes.json();
      } else {
        throw new Error('Must provide nodeId or memoryId');
      }

      if (currentSeq !== sequenceRef.current) return;

      setGraphNodes(res.nodes || []);
      setGraphEdges(res.edges || []);
      setFocusedNodeId(res.center?.id ?? null);
      
      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime + d.layout + d.render }));
    } catch (e: any) {
      if (e.name === 'AbortError' || currentSeq !== sequenceRef.current) return;
      
      if (e.message === 'ORPHAN_MEMORY') {
        setGraphInfo('This memory has no linked entities yet.');
      } else {
        setGraphError(e instanceof Error ? e.message : 'Failed to load graph');
      }
      
      setGraphNodes([]);
      setGraphEdges([]);
      setFocusedNodeId(null);
      
      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime }));
    } finally {
      if (graphAbortRef.current === abortController) {
        setGraphLoading(false);
      }
    }
  }, []);

  const handleSelectStreamItem = useCallback(
    async (item: StreamItem) => {
      setFocusedMemoryId(item.id);
      setFocusedNodeId(null); // Clear explicit node focus when focusing on a memory
      await loadNeighborhood(null, item.id);
    },
    [loadNeighborhood],
  );

  const handleGraphNodeClick = useCallback(
    async (node: GraphNode) => {
      setFocusedNodeId(node.id);
      await loadNeighborhood(node.id, null);
    },
    [loadNeighborhood],
  );

  const handleReset = useCallback(() => {
    // Only reset if we actually have something to reset
    if (focusedMemoryId === null && focusedNodeId === null) return;

    const currentSeq = ++sequenceRef.current;
    
    if (graphAbortRef.current) graphAbortRef.current.abort();
    if (streamAbortRef.current) streamAbortRef.current.abort();
    
    setFocusedMemoryId(null);
    setFocusedNodeId(null);
    setGraphNodes([]);
    setGraphEdges([]);
    setGraphError(null);
    setGraphInfo(null);
    
    loadStream(undefined, currentSeq);
  }, [focusedMemoryId, focusedNodeId, loadStream]);

  useEffect(() => {
    loadStream();
    return () => {
      if (graphAbortRef.current) graphAbortRef.current.abort();
      if (streamAbortRef.current) streamAbortRef.current.abort();
    };
  }, [loadStream]);

  return (
    <div className="flex h-[calc(100vh-3.5rem)] lg:h-[calc(100vh-4rem)] bg-zinc-950">
      {/* Left: Life Stream */}
      <div className="w-96 flex-shrink-0 border-r border-zinc-800">
        <LifeStream
          items={streamItems}
          loading={streamLoading}
          selectedItemId={focusedMemoryId}
          onSelectItem={handleSelectStreamItem}
          onLoadMore={loadMoreStream}
        />
      </div>

      {/* Right: Neural Knowledge Disc */}
      <div className="flex-1 flex flex-col relative">
        {/* Toolbar */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-zinc-800 bg-zinc-900/80 flex-shrink-0">
          {focusedNodeId && (
            <span className="text-xs text-zinc-400">
              {graphNodes.length} nodes &middot; {graphEdges.length} edges
            </span>
          )}

          {graphError && (
            <span className="text-xs text-red-400 flex items-center gap-1">
              <AlertCircle className="h-3 w-3" />
              {graphError}
            </span>
          )}

          {graphInfo && (
            <span className="text-xs text-zinc-400 flex items-center gap-1">
              <Info className="h-3 w-3" />
              {graphInfo}
            </span>
          )}

          <div className="flex-1" />

          {/* Diagnostics Panel Toggle */}
          <button
            onClick={() => setShowDiagnostics(d => !d)}
            className={`text-xs px-2 py-1 rounded transition-colors ${
              showDiagnostics ? 'bg-zinc-800 text-zinc-300' : 'text-zinc-600 hover:text-zinc-400'
            }`}
          >
            Dev
          </button>
          
          {showDiagnostics && (
            <div className="flex items-center gap-2 text-[10px] font-mono bg-zinc-950/50 px-2 py-1 rounded border border-zinc-800">
              <span className="text-blue-400">Fetch {diagnostics.fetch}ms</span>
              <span className="text-zinc-600">|</span>
              <span className="text-purple-400">Layout {diagnostics.layout}ms</span>
              <span className="text-zinc-600">|</span>
              <span className="text-teal-400">Render {diagnostics.render}ms</span>
              <span className="text-zinc-600">|</span>
              <span className="text-amber-400">Hover {diagnostics.hover}ms</span>
              <span className="text-zinc-600">|</span>
              <span className="text-zinc-300">Total {diagnostics.total}ms</span>
              
              <div className="w-px h-3 bg-zinc-800 mx-1" />
              
              <button
                onClick={() => setEnableEffects(e => !e)}
                className={`px-1.5 py-0.5 rounded border transition-colors ${
                  enableEffects 
                    ? 'bg-teal-900/30 text-teal-300 border-teal-800/50' 
                    : 'bg-zinc-800/50 text-zinc-500 border-zinc-700/50'
                }`}
              >
                FX {enableEffects ? 'ON' : 'OFF'}
              </button>

              {process.env.NODE_ENV === 'development' && (
                <button 
                  onClick={() => (window as any).__crashPixi?.()}
                  className="bg-red-900/50 text-red-300 px-1.5 py-0.5 rounded hover:bg-red-800/50 border border-red-800/50 ml-1"
                >
                  GPU Crash
                </button>
              )}
            </div>
          )}

          <a
            href="/dashboard/memories"
            className="text-sm text-zinc-500 hover:text-zinc-300 flex items-center gap-1 transition-colors ml-2"
          >
            <ArrowLeft className="h-4 w-4" />
            Memories
          </a>
        </div>

        {/* Canvas */}
        <div className="flex-1 relative">
          {graphLoading && (
            <div className="absolute inset-0 flex items-center justify-center bg-zinc-950/80 z-10 pointer-events-none">
              <Loader2 className="h-6 w-6 animate-spin text-zinc-400" />
            </div>
          )}

          <NeuralDisc
            key={discKey}
            nodes={graphNodes}
            edges={graphEdges}
            centerNodeId={focusedNodeId}
            onNodeClick={handleGraphNodeClick}
            onBackgroundClick={handleReset}
            onContextRestored={() => setDiscKey(k => k + 1)}
            onDiagnostics={(metrics) => setDiagnostics(d => ({ ...d, ...metrics, total: d.fetch + metrics.layout + metrics.render }))}
            enableEffects={enableEffects}
          />
        </div>
      </div>
    </div>
  );
}
