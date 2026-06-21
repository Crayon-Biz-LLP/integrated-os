'use client';

import dynamic from 'next/dynamic';
import { useEffect, useState, useCallback, useRef } from 'react';
import { Loader2, AlertCircle, ArrowLeft, Info, User } from 'lucide-react';
import { fetchNeighborhood, fetchEgoGraph, fetchEpisodes, resolveMemoryToEntity } from '@/lib/memories/stream';
import type { Episode, NeighborhoodResponse } from '@/lib/memories/stream';
import type { GraphNode, GraphEdge } from '@/lib/memories/types';

const EpisodeStream = dynamic(() => import('@/components/memories/EpisodeStream'), { ssr: false });
const NeuralDisc = dynamic(() => import('@/components/memories/NeuralDisc'), { ssr: false });

export default function MemoryGraphPage() {
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [episodesLoading, setEpisodesLoading] = useState(true);
  const [expandedEpisodeId, setExpandedEpisodeId] = useState<string | null>(null);
  const [expandedMemoryId, setExpandedMemoryId] = useState<number | null>(null);

  const [graphNodes, setGraphNodes] = useState<GraphNode[]>([]);
  const [graphEdges, setGraphEdges] = useState<GraphEdge[]>([]);
  const [focusedNodeId, setFocusedNodeId] = useState<string | null>(null);
  const [graphLoading, setGraphLoading] = useState(true);
  const [graphError, setGraphError] = useState<string | null>(null);
  const dannyIdRef = useRef<string | null>(null);

  // Graphics context recovery
  const [discKey, setDiscKey] = useState(0);

  // Diagnostics
  const [diagnostics, setDiagnostics] = useState({ fetch: 0, layout: 0, render: 0, hover: 0, total: 0 });
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [enableEffects, setEnableEffects] = useState(true);

  const episodeLimitRef = useRef(40);

  // Abort controllers for in-flight requests
  const episodeAbortRef = useRef<AbortController | null>(null);
  const graphAbortRef = useRef<AbortController | null>(null);

  // Current filter node ID for episode stream
  const episodeFilterRef = useRef<string | null>(null);

  // Sequence guards against stale responses resolving out of order
  const sequenceRef = useRef(0);

  const loadEpisodes = useCallback(async (nodeId?: string) => {
    if (episodeAbortRef.current) episodeAbortRef.current.abort();
    const abortController = new AbortController();
    episodeAbortRef.current = abortController;
    const currentSeq = ++sequenceRef.current;

    episodeFilterRef.current = nodeId ?? null;
    setEpisodesLoading(true);

    try {
      const res = await fetchEpisodes(nodeId ?? undefined, episodeLimitRef.current, abortController.signal);
      if (currentSeq !== sequenceRef.current) return;
      setEpisodes(res.episodes);
    } catch (e: any) {
      if (e.name === 'AbortError' || currentSeq !== sequenceRef.current) return;
      setEpisodes([]);
    } finally {
      if (episodeAbortRef.current === abortController) {
        setEpisodesLoading(false);
      }
    }
  }, []);

  const loadMoreEpisodes = useCallback(async () => {
    if (episodeAbortRef.current) return;
    episodeLimitRef.current += 20;
    const abortController = new AbortController();
    episodeAbortRef.current = abortController;
    const currentSeq = sequenceRef.current;

    setEpisodesLoading(true);
    try {
      const res = await fetchEpisodes(episodeFilterRef.current ?? undefined, episodeLimitRef.current, abortController.signal);
      if (currentSeq !== sequenceRef.current) return;

      setEpisodes((prev) => {
        const existing = new Set(prev.map((e) => e.id));
        const newEps = res.episodes.filter((e) => !existing.has(e.id));
        return [...prev, ...newEps];
      });
    } catch (e: any) {
      if (e.name === 'AbortError' || currentSeq !== sequenceRef.current) return;
    } finally {
      if (episodeAbortRef.current === abortController) {
        episodeAbortRef.current = null;
        setEpisodesLoading(false);
      }
    }
  }, []);

  const loadNeighborhood = useCallback(async (nodeId: string) => {
    const currentSeq = ++sequenceRef.current;

    if (graphAbortRef.current) graphAbortRef.current.abort();
    const abortController = new AbortController();
    graphAbortRef.current = abortController;

    setGraphLoading(true);
    setGraphError(null);
    const start = performance.now();

    try {
      const res = await fetchNeighborhood(nodeId, abortController.signal);
      if (currentSeq !== sequenceRef.current) return;

      setGraphNodes(res.nodes || []);
      setGraphEdges(res.edges || []);
      setFocusedNodeId(res.center?.id ?? nodeId);

      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime + d.layout + d.render }));
    } catch (e: any) {
      if (e.name === 'AbortError' || currentSeq !== sequenceRef.current) return;
      setGraphError(e instanceof Error ? e.message : 'Failed to load graph');
      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime }));
    } finally {
      if (graphAbortRef.current === abortController) {
        setGraphLoading(false);
      }
    }
  }, []);

  const loadEgoGraph = useCallback(async () => {
    const currentSeq = ++sequenceRef.current;

    if (graphAbortRef.current) graphAbortRef.current.abort();
    const abortController = new AbortController();
    graphAbortRef.current = abortController;

    setGraphLoading(true);
    setGraphError(null);
    const start = performance.now();

    try {
      const res = await fetchEgoGraph(2, 80, abortController.signal);
      if (currentSeq !== sequenceRef.current) return;

      dannyIdRef.current = res.danny_id;
      setGraphNodes(res.nodes || []);
      setGraphEdges(res.edges || []);
      setFocusedNodeId(res.danny_id);

      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime + d.layout + d.render }));
    } catch (e: any) {
      if (e.name === 'AbortError' || currentSeq !== sequenceRef.current) return;
      setGraphError(e instanceof Error ? e.message : 'Failed to load graph');
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

  const handleEpisodeClick = useCallback(
    async (episode: Episode) => {
      if (expandedEpisodeId === episode.id) {
        setExpandedEpisodeId(null);
        setExpandedMemoryId(null);
        returnToDanny();
        return;
      }

      setExpandedEpisodeId(episode.id);
      setExpandedMemoryId(null);

      const entityId = episode.graph_node_ids[0];
      if (entityId) {
        setFocusedNodeId(entityId);
        await loadNeighborhood(entityId);
      }
    },
    [expandedEpisodeId, loadNeighborhood],
  );

  const handleMemoryClick = useCallback(
    async (memoryId: number) => {
      setExpandedMemoryId(memoryId);
      const entityId = await resolveMemoryToEntity(memoryId);
      if (entityId) {
        setFocusedNodeId(entityId);
        await loadNeighborhood(entityId);
      }
    },
    [loadNeighborhood],
  );

  const handleGraphNodeClick = useCallback(
    async (node: GraphNode) => {
      setFocusedNodeId(node.id);
      await loadNeighborhood(node.id);
      await loadEpisodes(node.id);
    },
    [loadNeighborhood, loadEpisodes],
  );

  const returnToDanny = useCallback(() => {
    if (!dannyIdRef.current) {
      loadEgoGraph();
      return;
    }

    setExpandedEpisodeId(null);
    setExpandedMemoryId(null);
    loadEgoGraph();
    loadEpisodes();
  }, [loadEgoGraph, loadEpisodes]);

  const handleGraphBackgroundClick = useCallback(() => {
    if (focusedNodeId === dannyIdRef.current) return;
    returnToDanny();
  }, [focusedNodeId, returnToDanny]);

  const isDannyCentered = focusedNodeId === dannyIdRef.current;

  useEffect(() => {
    loadEgoGraph();
    loadEpisodes();
    return () => {
      if (graphAbortRef.current) graphAbortRef.current.abort();
      if (episodeAbortRef.current) episodeAbortRef.current.abort();
    };
  }, [loadEgoGraph, loadEpisodes]);

  return (
    <div className="flex h-[calc(100vh-3.5rem)] lg:h-[calc(100vh-4rem)] bg-zinc-950">
      {/* Left: Context Stream (Episodes) */}
      <div className="w-96 flex-shrink-0 border-r border-zinc-800">
        <EpisodeStream
          episodes={episodes}
          loading={episodesLoading}
          expandedEpisodeId={expandedEpisodeId}
          expandedMemoryId={expandedMemoryId}
          onToggleEpisode={handleEpisodeClick}
          onMemoryClick={handleMemoryClick}
          onLoadMore={loadMoreEpisodes}
        />
      </div>

      {/* Right: Neural Knowledge Disc */}
      <div className="flex-1 flex flex-col relative">
        {/* Toolbar */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-zinc-800 bg-zinc-900/80 flex-shrink-0">
          {!isDannyCentered && dannyIdRef.current && (
            <button
              onClick={returnToDanny}
              className="text-xs flex items-center gap-1.5 text-teal-400 hover:text-teal-300 transition-colors px-2 py-1 rounded bg-teal-500/10 hover:bg-teal-500/20"
            >
              <User className="h-3 w-3" />
              Danny
            </button>
          )}

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
            <div className="absolute inset-0 flex items-center justify-center bg-zinc-950/80 z-10">
              <Loader2 className="h-6 w-6 animate-spin text-zinc-400" />
            </div>
          )}

          <NeuralDisc
            key={discKey}
            nodes={graphNodes}
            edges={graphEdges}
            centerNodeId={focusedNodeId}
            onNodeClick={handleGraphNodeClick}
            onBackgroundClick={handleGraphBackgroundClick}
            onContextRestored={() => setDiscKey(k => k + 1)}
            onDiagnostics={(metrics) => setDiagnostics(d => ({ ...d, ...metrics, total: d.fetch + metrics.layout + metrics.render }))}
            enableEffects={enableEffects}
          />
        </div>
      </div>
    </div>
  );
}
