'use client';

import dynamic from 'next/dynamic';
import { useEffect, useCallback, useRef } from 'react';
import {
  AlertCircle, ArrowLeft, User,
  PanelLeftClose, PanelLeft, PanelRightClose, PanelRight, Maximize2,
} from 'lucide-react';
import { useState } from 'react';

import { fetchNeighborhood, fetchEgoGraph, fetchEpisodes, resolveMemoryToEntity } from '@/lib/memories/stream';
import type { Episode } from '@/lib/memories/stream';
import type { GraphNode, GraphEdge } from '@/lib/memories/types';

import { useFocusContext } from '@/lib/memories/useFocusContext';
import { useNodeMemories } from '@/lib/memories/useNodeMemories';

const GraphFinder   = dynamic(() => import('@/components/memories/GraphFinder'),    { ssr: false });
const NeuralDisc      = dynamic(() => import('@/components/memories/NeuralDisc'),       { ssr: false });
const GraphInspector = dynamic(() => import('@/components/memories/GraphInspector'), { ssr: false });

// ── mode badge labels ─────────────────────────────────────────────────────────
const MODE_LABELS: Record<string, string> = {
  overview:    'Overview',
  'soft-focus': 'Exploring',
  'ego-focus':  'Deep focus',
  detail:      'Memory detail',
};

export default function MemoryGraphPage() {
  // ── episode / left-stream state ───────────────────────────────────────────
  const [episodes,         setEpisodes]         = useState<Episode[]>([]);
  const [episodesLoading,  setEpisodesLoading]  = useState(true);
  const [expandedEpisodeId, setExpandedEpisodeId] = useState<string | null>(null);
  const [expandedMemoryId,  setExpandedMemoryId]  = useState<number | null>(null);

  // ── graph data ────────────────────────────────────────────────────────────
  const [graphNodes,   setGraphNodes]   = useState<GraphNode[]>([]);
  const [graphEdges,   setGraphEdges]   = useState<GraphEdge[]>([]);
  const [, setGraphLoading] = useState(true);
  const [graphError,   setGraphError]   = useState<string | null>(null);
  // dannyId needs to drive render (toolbar visibility, isDannyCentered) so it lives in state.
  // The ref below shadows it for use inside callbacks without re-triggering effects.
  const [dannyId,    setDannyId]        = useState<string | null>(null);
  const dannyIdRef = useRef<string | null>(null);

  // ── focus context (state machine) ─────────────────────────────────────────
  const focus = useFocusContext();

  // ── memory panel data ─────────────────────────────────────────────────────
  const memories = useNodeMemories();

  // ── ui toggles ────────────────────────────────────────────────────────────
  const [streamCollapsed,  setStreamCollapsed]  = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [showDiagnostics,  setShowDiagnostics]  = useState(false);
  const [enableEffects,    setEnableEffects]     = useState(true);
  const [discKey,          setDiscKey]           = useState(0);

  // ── resizable panes & responsive ──────────────────────────────────────────
  const [leftWidth, setLeftWidth] = useState(320);
  const [rightWidth, setRightWidth] = useState(320);
  const [resizing, setResizing] = useState<'left' | 'right' | null>(null);

  // Auto-collapse on small screens
  useEffect(() => {
    if (typeof window !== 'undefined' && window.innerWidth < 1024) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStreamCollapsed(true);
      setInspectorCollapsed(true);
      // Reduce default widths on small screens
      if (window.innerWidth < 768) {
        setLeftWidth(window.innerWidth);
        setRightWidth(window.innerWidth);
      } else {
        setLeftWidth(280);
        setRightWidth(280);
      }
    }
  }, []);

  useEffect(() => {
    if (!resizing) return;
    const handleMove = (e: PointerEvent) => {
      if (resizing === 'left') {
        const newWidth = Math.max(240, Math.min(e.clientX, 600));
        setLeftWidth(newWidth);
      } else {
        const newWidth = Math.max(240, Math.min(window.innerWidth - e.clientX, 600));
        setRightWidth(newWidth);
      }
    };
    const handleUp = () => setResizing(null);

    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    return () => {
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
    };
  }, [resizing]);

  // ── diagnostics ───────────────────────────────────────────────────────────
  const [diagnostics, setDiagnostics] = useState({ fetch: 0, layout: 0, render: 0, hover: 0, total: 0 });

  // ── abort controllers & sequence guard ───────────────────────────────────
  const episodeAbortRef = useRef<AbortController | null>(null);
  const graphAbortRef   = useRef<AbortController | null>(null);
  const episodeLimitRef = useRef(40);
  const episodeFilterRef = useRef<string | null>(null);
  const sequenceRef = useRef(0);

  // ── data fetchers (unchanged from original page.tsx) ─────────────────────

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
    } catch (e: unknown) {
      if (e instanceof Error && (e.name === 'AbortError' || currentSeq !== sequenceRef.current)) return;
      setEpisodes([]);
    } finally {
      if (episodeAbortRef.current === abortController) setEpisodesLoading(false);
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
      const res = await fetchEpisodes(
        episodeFilterRef.current ?? undefined,
        episodeLimitRef.current,
        abortController.signal,
      );
      if (currentSeq !== sequenceRef.current) return;
      setEpisodes(prev => {
        const existing = new Set(prev.map(e => e.id));
        return [...prev, ...res.episodes.filter(e => !existing.has(e.id))];
      });
    } catch (e: unknown) {
      if (e instanceof Error && (e.name === 'AbortError' || currentSeq !== sequenceRef.current)) return;
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
      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime + d.layout + d.render }));
    } catch (e: unknown) {
      if (e instanceof Error && (e.name === 'AbortError' || currentSeq !== sequenceRef.current)) return;
      setGraphError(e instanceof Error ? e.message : 'Failed to load graph');
      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime }));
    } finally {
      if (graphAbortRef.current === abortController) setGraphLoading(false);
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
      const res = await fetchEgoGraph(2, 2500, abortController.signal);
      if (currentSeq !== sequenceRef.current) return;
      dannyIdRef.current = res.danny_id;
      setDannyId(res.danny_id);
      setGraphNodes(res.nodes || []);
      setGraphEdges(res.edges || []);
      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime + d.layout + d.render }));
    } catch (e: unknown) {
      if (e instanceof Error && (e.name === 'AbortError' || currentSeq !== sequenceRef.current)) return;
      setGraphError(e instanceof Error ? e.message : 'Failed to load graph');
      setGraphNodes([]);
      setGraphEdges([]);
      const fetchTime = Math.round(performance.now() - start);
      setDiagnostics(d => ({ ...d, fetch: fetchTime, total: fetchTime }));
    } finally {
      if (graphAbortRef.current === abortController) setGraphLoading(false);
    }
  }, []);

  // ── interaction handlers ──────────────────────────────────────────────────

  const returnToDanny = useCallback(() => {
    setExpandedEpisodeId(null);
    setExpandedMemoryId(null);
    memories.clear();
    focus.reset();
    if (dannyIdRef.current) {
      loadEgoGraph();
      loadEpisodes();
    } else {
      loadEgoGraph();
    }
  }, [focus, loadEgoGraph, loadEpisodes, memories]);

  /**
   * Graph node clicked — enters soft-focus (no recentering).
   * Also fetches memory stream for the panel.
   */
  const handleGraphNodeClick = useCallback(
    async (node: GraphNode) => {
      focus.clickNode(node.id);
      setInspectorCollapsed(false); // auto-expand on click
      // Soft-focus: load neighbourhood (which may change graph data slightly)
      // and memory stream in parallel; no centering on this node yet
      await Promise.all([
        loadNeighborhood(node.id),
        loadEpisodes(node.id),
      ]);
      memories.load(node.id);
    },
    [focus, loadNeighborhood, loadEpisodes, memories],
  );

  const handleGraphBackgroundClick = useCallback(() => {
    if (focus.state.focusedNodeId === dannyIdRef.current) return;
    returnToDanny();
  }, [focus.state.focusedNodeId, returnToDanny]);

  /**
   * Panel "Focus graph here" button — promote to ego-focus (recenters).
   */
  const handleEgoFocus = useCallback(
    (nodeId: string) => {
      focus.triggerEgoFocus(nodeId);
      setInspectorCollapsed(false);
      // Reload ego-graph centred on this node so layout pins it
      loadNeighborhood(nodeId);
    },
    [focus, loadNeighborhood],
  );

  const handlePanelClose = useCallback(() => {
    setInspectorCollapsed(true);
  }, []);

  // ── episode-stream interactions (unchanged) ───────────────────────────────

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
        focus.clickNode(entityId);
        setInspectorCollapsed(false);
        await loadNeighborhood(entityId);
        memories.load(entityId);
      }
    },
    [expandedEpisodeId, focus, loadNeighborhood, memories, returnToDanny],
  );

  const handleMemoryClick = useCallback(
    async (memoryId: number) => {
      setExpandedMemoryId(memoryId);
      const entityId = await resolveMemoryToEntity(memoryId);
      if (entityId) {
        focus.clickNode(entityId);
        setInspectorCollapsed(false);
        await loadNeighborhood(entityId);
        memories.load(entityId);
      }
    },
    [focus, loadNeighborhood, memories],
  );

  // ── stable callbacks for NeuralDisc ──────────────────────────────────────
  const handleDiagnostics = useCallback(
    (metrics: { layout: number; render: number; hover: number }) => {
      setDiagnostics(d => ({ ...d, ...metrics, total: d.fetch + metrics.layout + metrics.render }));
    },
    [],
  );
  const handleContextRestored = useCallback(() => setDiscKey(k => k + 1), []);

  // ── initial load ──────────────────────────────────────────────────────────
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadEgoGraph();
    loadEpisodes();
    return () => {
      if (graphAbortRef.current)   graphAbortRef.current.abort();
      if (episodeAbortRef.current) episodeAbortRef.current.abort();
    };
  }, [loadEgoGraph, loadEpisodes]);

  // ── derived ───────────────────────────────────────────────────────────────
  const { state: focusState, derived: focusDerived } = focus;

  // centreNodeId for NeuralDisc: only pass it when ego-focus should recenter
  const centreNodeForDisc = focusDerived.shouldRecenter
    ? focusState.focusedNodeId
    : (dannyId ?? null);       // ego default anchors Danny

  const isDannyCentered = focusState.focusedNodeId === dannyId
    || focusState.viewMode === 'overview';

  const focusedNode = graphNodes.find(n => n.id === focusState.focusedNodeId) ?? null;

  return (
    <div className={`flex h-[calc(100vh-3.5rem)] lg:h-[calc(100vh-4rem)] bg-zinc-950 ${resizing ? 'select-none' : ''}`}>
      {/* ── left: graph finder ───────────────────────────────────────────── */}
      {!streamCollapsed && (
        <div style={{ width: leftWidth }} className="flex-shrink-0 border-r border-zinc-800 transition-all duration-200">
          <GraphFinder
            episodes={episodes}
            loading={episodesLoading}
            allNodes={graphNodes}
            focusedNode={focusedNode}
            expandedEpisodeId={expandedEpisodeId}
            expandedMemoryId={expandedMemoryId}
            selectedNodeId={focusState.focusedNodeId}
            onToggleEpisode={handleEpisodeClick}
            onMemoryClick={handleMemoryClick}
            onLoadMore={loadMoreEpisodes}
            onNavigateNode={(nodeId) => {
              const targetNode = graphNodes.find(n => n.id === nodeId);
              if (targetNode) handleGraphNodeClick(targetNode);
            }}
          />
        </div>
      )}

      {/* ── left resizer ─────────────────────────────────────────────────── */}
      {!streamCollapsed && (
        <div
          className="w-1.5 cursor-col-resize bg-transparent hover:bg-teal-500/50 active:bg-teal-500/80 transition-colors z-30 flex-shrink-0"
          onPointerDown={(e) => { e.preventDefault(); setResizing('left'); }}
        />
      )}

      {/* ── centre: graph canvas ──────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col relative min-w-0" style={{ pointerEvents: resizing ? 'none' : 'auto' }}>
        {/* toolbar */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-zinc-800 bg-zinc-900/80 flex-shrink-0">
          {/* Danny / home button */}
          {!isDannyCentered && dannyId && (
            <button
              onClick={returnToDanny}
              className="text-xs flex items-center gap-1.5 text-teal-400 hover:text-teal-300 transition-colors px-2 py-1 rounded bg-teal-500/10 hover:bg-teal-500/20"
            >
              <User className="h-3 w-3" />
              Danny
            </button>
          )}

          {/* Mode indicator */}
          {focusState.viewMode !== 'overview' && (
            <span className="text-[10px] text-zinc-500 font-medium uppercase tracking-widest">
              {MODE_LABELS[focusState.viewMode]}
            </span>
          )}

          {/* Node + edge count */}
          {graphNodes.length > 0 && (
            <span className="text-xs text-zinc-600">
              {graphNodes.length} nodes · {graphEdges.length} edges
            </span>
          )}

          {/* Error */}
          {graphError && (
            <span className="text-xs text-red-400 flex items-center gap-1">
              <AlertCircle className="h-3 w-3" />
              {graphError}
            </span>
          )}

          <div className="flex-1" />

          {/* Ego-focus quick action (when in soft-focus) */}
          {(focusState.viewMode === 'soft-focus') && focusState.focusedNodeId && (
            <button
              onClick={() => handleEgoFocus(focusState.focusedNodeId!)}
              className="text-xs flex items-center gap-1 text-zinc-400 hover:text-zinc-200 px-2 py-1 rounded hover:bg-zinc-800 transition-colors"
              title="Recenter graph on focused node"
            >
              <Maximize2 className="h-3 w-3" />
              Deep focus
            </button>
          )}

          {/* Left Sidebar toggle */}
          <button
            onClick={() => setStreamCollapsed(c => !c)}
            className="text-xs flex items-center gap-1 text-zinc-500 hover:text-zinc-300 transition-colors px-1.5 py-1 rounded"
            title={streamCollapsed ? 'Show Finder' : 'Hide Finder'}
          >
            {streamCollapsed
              ? <PanelLeft className="h-3.5 w-3.5" />
              : <PanelLeftClose className="h-3.5 w-3.5" />}
          </button>

          {/* Right Sidebar toggle */}
          <button
            onClick={() => setInspectorCollapsed(c => !c)}
            className="text-xs flex items-center gap-1 text-zinc-500 hover:text-zinc-300 transition-colors px-1.5 py-1 rounded"
            title={inspectorCollapsed ? 'Show Inspector' : 'Hide Inspector'}
          >
            {inspectorCollapsed
              ? <PanelRight className="h-3.5 w-3.5" />
              : <PanelRightClose className="h-3.5 w-3.5" />}
          </button>

          {/* Dev diagnostics toggle */}
          <button
            onClick={() => setShowDiagnostics(d => !d)}
            className={`text-xs px-2 py-1 rounded transition-colors ${
              showDiagnostics ? 'bg-zinc-800 text-zinc-300' : 'text-zinc-700 hover:text-zinc-500'
            }`}
          >
            Dev
          </button>

          {showDiagnostics && (
            <div className="flex items-center gap-2 text-[10px] font-mono bg-zinc-950/50 px-2 py-1 rounded border border-zinc-800">
              <span className="text-blue-400">F {diagnostics.fetch}ms</span>
              <span className="text-zinc-700">|</span>
              <span className="text-purple-400">L {diagnostics.layout}ms</span>
              <span className="text-zinc-700">|</span>
              <span className="text-teal-400">R {diagnostics.render}ms</span>
              <span className="text-zinc-700">|</span>
              <span className="text-amber-400">H {diagnostics.hover}ms</span>
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
                  onClick={() => (window as unknown as { __crashPixi?: () => void }).__crashPixi?.()}
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

        {/* canvas area */}
        <div className="flex-1 relative min-h-0">
          <NeuralDisc
            key={discKey}
            nodes={graphNodes}
            edges={graphEdges}
            centerNodeId={centreNodeForDisc}
            viewMode={focusState.viewMode}
            dimAlpha={focusDerived.dimAlpha}
            dimEdgeAlpha={focusDerived.dimEdgeAlpha}
            showParticles={focusDerived.showParticles}
            useCurvedEdges={focusDerived.useCurvedEdges}
            onNodeClick={handleGraphNodeClick}
            onBackgroundClick={handleGraphBackgroundClick}
            onContextRestored={handleContextRestored}
            onDiagnostics={handleDiagnostics}
            enableEffects={enableEffects}
          />

          {/* Subtle bottom-left branding */}
          <span className="absolute bottom-3 left-4 text-[9px] text-zinc-800 select-none tracking-wider uppercase pointer-events-none">
            Rhodey OS · Knowledge Graph
          </span>
        </div>
      </div>

      {/* ── right resizer ────────────────────────────────────────────────── */}
      {!inspectorCollapsed && (
        <div
          className="w-1.5 cursor-col-resize bg-transparent hover:bg-teal-500/50 active:bg-teal-500/80 transition-colors z-30 flex-shrink-0"
          onPointerDown={(e) => { e.preventDefault(); setResizing('right'); }}
        />
      )}

      {/* ── right: graph inspector ────────────────────────────────────────── */}
      {!inspectorCollapsed && (
        <div style={{ width: rightWidth }} className="flex-shrink-0 border-l border-zinc-800 flex flex-col z-20 bg-zinc-950">
          <GraphInspector
          node={focusedNode}
          allNodes={graphNodes}
          allEdges={graphEdges}
          items={memories.state.items}
          loading={memories.state.loading}
          error={memories.state.error}
          onClose={handlePanelClose}
          onFocusNode={handleEgoFocus}
          onNavigateNode={(nodeId) => {
            const targetNode = graphNodes.find(n => n.id === nodeId);
            if (targetNode) handleGraphNodeClick(targetNode);
          }}
          />
        </div>
      )}
    </div>
  );
}
