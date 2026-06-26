'use client';

/**
 * NeuralDisc — PixiJS WebGL graph renderer for Rhodey OS.
 *
 * Rendering modes (driven by `viewMode` prop):
 *   overview   – ambient field; ranked anchor labels (≤8); straight edges;
 *                no particles; no recentering
 *   soft-focus – dim non-connected; neighbourhood labels (ranked ≤16);
 *                curved edges; no particles; no recentering
 *   ego-focus  – dim non-connected; neighbourhood labels (ranked ≤20);
 *                curved edges; particles on active edges; recenters on
 *                centreNodeId (triggered by parent)
 *   detail     – identical to soft-focus visually; parent controls recenter
 *
 * Keyboard nav (lightweight):
 *   Tab    → focuses the canvas wrapper
 *   ↑↓←→  → moves the virtual focus cursor through nodes (by proximity)
 *   Enter  → triggers onNodeClick (same as pointer click)
 *   Space  → same as Enter
 *   Escape → triggers onBackgroundClick
 */

import { useRef, useEffect, useLayoutEffect, useState, useCallback } from 'react';
import * as d3 from 'd3';
import { Application, Graphics, Text, TextStyle, Container, BlurFilter } from 'pixi.js';
import type { GraphNode, GraphEdge } from '@/lib/memories/types';
import type { ViewMode } from '@/lib/memories/useFocusContext';

// ── types ────────────────────────────────────────────────────────────────────

interface NeuralDiscProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  centerNodeId: string | null;
  viewMode: ViewMode;
  /** Whether to dim non-connected nodes/edges */
  dimAlpha: number;
  /** Alpha for non-connected edges */
  dimEdgeAlpha: number;
  /** Whether particles should render */
  showParticles: boolean;
  /** Whether to use curved edges */
  useCurvedEdges: boolean;
  onNodeClick: (node: GraphNode) => void;
  onBackgroundClick: () => void;
  onDiagnostics?: (metrics: { layout: number; render: number; hover: number }) => void;
  onContextRestored?: () => void;
  enableEffects?: boolean;
}

interface SimNode extends d3.SimulationNodeDatum {
  id: string;
  label: string;
  type: string;
}

interface LayoutNode {
  id: string;
  label: string;
  type: string;
  x: number;
  y: number;
  /** Number of edges connected to this node in the current view */
  degree: number;
}

interface SimEdge {
  id: string;
  relationship: string;
  source: SimNode;
  target: SimNode;
}

interface LayoutEdge {
  id: string;
  relationship: string;
  source: LayoutNode;
  target: LayoutNode;
}

// ── constants ────────────────────────────────────────────────────────────────

const COLOR_MAP: Record<string, number> = {
  person:          0x3b82f6,
  organization:    0x14b8a6,
  project:         0x8b5cf6,
  cluster:         0xa855f7,
  task:            0xf59e0b,
  concept:         0x71717a,
  emotional_state: 0xf43f5e,
};

/** Max labels per zoom level × mode. Prevents label explosion in dense graphs. */
const LABEL_CAPS: Record<ViewMode, Record<'far' | 'mid' | 'near' | 'close', number>> = {
  overview:   { far: 3, mid: 5,  near: 8,  close: 12 },
  'soft-focus': { far: 3, mid: 8,  near: 16, close: 24 },
  'ego-focus':  { far: 3, mid: 10, near: 20, close: 32 },
  detail:     { far: 3, mid: 8,  near: 16, close: 24 },
};

function zoomBand(scale: number): 'far' | 'mid' | 'near' | 'close' {
  if (scale < 0.45) return 'far';
  if (scale < 0.8)  return 'mid';
  if (scale < 1.8)  return 'near';
  return 'close';
}

// ── layout ────────────────────────────────────────────────────────────────────

function computeLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  centerId: string | null,
  width: number,
  height: number,
): { layoutNodes: LayoutNode[]; layoutEdges: LayoutEdge[] } {
  if (nodes.length === 0) return { layoutNodes: [], layoutEdges: [] };

  // Degree map
  const degreeMap = new Map<string, number>();
  nodes.forEach(n => degreeMap.set(n.id, 0));
  edges.forEach(e => {
    degreeMap.set(e.source_node_id, (degreeMap.get(e.source_node_id) ?? 0) + 1);
    degreeMap.set(e.target_node_id, (degreeMap.get(e.target_node_id) ?? 0) + 1);
  });

  const simNodes: SimNode[] = nodes.map(n => ({
    id: n.id,
    label: n.label,
    type: n.type,
  }));

  const simEdges: SimEdge[] = edges
    .filter(
      e =>
        nodes.some(n => n.id === e.source_node_id) &&
        nodes.some(n => n.id === e.target_node_id),
    )
    .map(e => ({
      id: e.id,
      relationship: e.relationship,
      source: simNodes.find(n => n.id === e.source_node_id)!,
      target: simNodes.find(n => n.id === e.target_node_id)!,
    }));

  if (centerId !== null) {
    const centre = simNodes.find(n => n.id === centerId);
    if (centre) {
      centre.x  = width / 2;
      centre.y  = height / 2;
      centre.fx = width / 2;
      centre.fy = height / 2;
    }
  }

  const sim = d3
    .forceSimulation(simNodes)
    .force(
      'link',
      d3.forceLink<SimNode, SimEdge>(simEdges)
        .id(d => d.id)
        .distance(120)
        .strength(0.6),
    )
    .force('charge', d3.forceManyBody().strength(-300))
    .force('centre', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide(30));

  sim.tick(300);
  sim.stop();

  const layoutNodes: LayoutNode[] = simNodes.map(n => ({
    id: n.id,
    label: n.label,
    type: n.type,
    x: n.x ?? width / 2,
    y: n.y ?? height / 2,
    degree: degreeMap.get(n.id) ?? 0,
  }));

  const nodeMap = new Map<string, LayoutNode>(layoutNodes.map(n => [n.id, n]));
  const layoutEdges: LayoutEdge[] = simEdges
    .filter(e => nodeMap.has(e.source.id) && nodeMap.has(e.target.id))
    .map(e => ({
      id: e.id,
      relationship: e.relationship,
      source: nodeMap.get(e.source.id)!,
      target: nodeMap.get(e.target.id)!,
    }));

  return { layoutNodes, layoutEdges };
}

// ── label selection ───────────────────────────────────────────────────────────

/**
 * Returns the set of node IDs that should display a label.
 * Ranked by degree; honours per-mode zoom caps; always shows focused/centre node.
 */
function selectLabelIds(
  layoutNodes: LayoutNode[],
  layoutEdges: LayoutEdge[],
  centreId: string | null,
  hoverId: string | null,
  viewMode: ViewMode,
  scale: number,
  connectedIds: Set<string>,
): Set<string> {
  const band = zoomBand(scale);
  const cap  = LABEL_CAPS[viewMode][band];

  // In focus modes, neighbourhood nodes are prioritised over global rank
  const isFocused = viewMode === 'soft-focus' || viewMode === 'ego-focus' || viewMode === 'detail';

  // Sort all nodes by priority descending
  const sorted = [...layoutNodes].sort((a, b) => {
    // 1. Centre always first
    const aCentre = a.id === centreId ? 1 : 0;
    const bCentre = b.id === centreId ? 1 : 0;
    if (aCentre !== bCentre) return bCentre - aCentre;

    // 2. In focus mode: connected neighbours before others
    if (isFocused) {
      const aConn = connectedIds.has(a.id) ? 1 : 0;
      const bConn = connectedIds.has(b.id) ? 1 : 0;
      if (aConn !== bConn) return bConn - aConn;
    }

    // 3. Degree score (higher = more important)
    return b.degree - a.degree;
  });

  const ids = new Set<string>();

  // Always include hovered (transient, no cap)
  if (hoverId) ids.add(hoverId);

  for (const n of sorted) {
    if (ids.size >= cap) break;
    ids.add(n.id);
  }

  // Hovered node's connected neighbours get labels in focused band regardless
  if (hoverId && (band === 'near' || band === 'close')) {
    layoutEdges.forEach(e => {
      if (e.source.id === hoverId && ids.size < cap + 4) ids.add(e.target.id);
      if (e.target.id === hoverId && ids.size < cap + 4) ids.add(e.source.id);
    });
  }

  return ids;
}

// ── edge curve helper ─────────────────────────────────────────────────────────
/**
 * Draws a curved quadratic bezier edge.
 * The control point is offset perpendicular to the midpoint by `curvature` px.
 */
function drawCurvedEdge(
  g: Graphics,
  sx: number, sy: number,
  tx: number, ty: number,
  curvature = 18,
) {
  const mx = (sx + tx) / 2;
  const my = (sy + ty) / 2;
  const dx = tx - sx;
  const dy = ty - sy;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  // Perpendicular unit vector
  const px = -dy / len;
  const py =  dx / len;
  const cx = mx + px * curvature;
  const cy = my + py * curvature;
  g.moveTo(sx, sy);
  g.quadraticCurveTo(cx, cy, tx, ty);
}

  // ── component ─────────────────────────────────────────────────────────────────

export default function NeuralDisc({
  nodes: nodesProp,
  edges: edgesProp,
  centerNodeId,
  viewMode,
  dimAlpha,
  dimEdgeAlpha,
  showParticles,
  useCurvedEdges,
  onNodeClick,
  onBackgroundClick,
  onDiagnostics,
  onContextRestored,
  enableEffects = true,
}: NeuralDiscProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const appRef       = useRef<Application | null>(null);
  const mainContainerRef = useRef<Container | null>(null);

  const [dimensions,           setDimensions]           = useState({ width: 600, height: 600 });
  const [contextLost,          setContextLost]           = useState(false);
  const [isReady,              setIsReady]               = useState(false);
  const [hoveredNodeId,        setHoveredNodeId]         = useState<string | null>(null);
  const [prefersReducedMotion, setPrefersReducedMotion]  = useState(false);
  const [, setZoomVersion] = useState(0);
  const [layoutData, setLayoutData] = useState<{
    layoutNodes: LayoutNode[];
    layoutEdges: LayoutEdge[];
  }>({ layoutNodes: [], layoutEdges: [] });

  // ── keyboard virtual focus ────────────────────────────────────────────────
  const [kbFocusNodeId, setKbFocusNodeId] = useState<string | null>(null);

  // ── stable refs ───────────────────────────────────────────────────────────
  const onNodeClickRef       = useRef(onNodeClick);
  const onBackgroundClickRef = useRef(onBackgroundClick);
  const onDiagnosticsRef     = useRef(onDiagnostics);
  const onContextRestoredRef = useRef(onContextRestored);
  const nodesRef             = useRef(nodesProp);
  const edgesRef             = useRef(edgesProp);
  const centerNodeIdRef      = useRef(centerNodeId);
  const viewModeRef          = useRef(viewMode);

  const viewTransformRef = useRef({ x: 0, y: 0, scale: 1 });
  const renderCountRef   = useRef(0);
  const layoutCountRef   = useRef(0);
  const sceneBuildCountRef = useRef(0);
  const diagCallCountRef = useRef(0);
  const lastLogRef       = useRef(0);
  const lastMetrics      = useRef({ layout: 0, render: 0, hover: 0 });
  const prevHoveredNodeId = useRef<string | null>(null);

  // Layout stored in ref for keyboard nav lookups (always current)
  const layoutDataRef = useRef(layoutData);

  useLayoutEffect(() => {
    onNodeClickRef.current       = onNodeClick;
    onBackgroundClickRef.current = onBackgroundClick;
    onDiagnosticsRef.current     = onDiagnostics;
    onContextRestoredRef.current = onContextRestored;
    nodesRef.current             = nodesProp;
    edgesRef.current             = edgesProp;
    centerNodeIdRef.current      = centerNodeId;
    viewModeRef.current          = viewMode;
    renderCountRef.current      += 1;
  });

  useEffect(() => {
    layoutDataRef.current = layoutData;
  }, [layoutData]);

  // ── reduced-motion ────────────────────────────────────────────────────────
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPrefersReducedMotion(mq.matches);
    const handler = (e: MediaQueryListEvent) => setPrefersReducedMotion(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);

  // ── dev GPU crash helper ──────────────────────────────────────────────────
  useEffect(() => {
    if (process.env.NODE_ENV !== 'development') return;
    (window as unknown as Record<string, unknown>).__crashPixi = () => {
      const renderer = (appRef.current?.renderer as unknown as { gl?: WebGLRenderingContext; context?: { gl: WebGLRenderingContext } });
      const gl = renderer?.gl || renderer?.context?.gl;
      if (gl) (gl.getExtension('WEBGL_lose_context') as { loseContext?: () => void })?.loseContext?.();
    };
    return () => {
      if (process.env.NODE_ENV === 'development')
        delete (window as unknown as Record<string, unknown>).__crashPixi;
    };
  }, []);

  // ── init PixiJS (once) ────────────────────────────────────────────────────
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let destroyed = false;
    const app = new Application();

    const handleContextLost = (e: Event) => {
      e.preventDefault();
      setContextLost(true);
    };
    const handleContextRestored = () => {
      onContextRestoredRef.current?.();
    };

    (async () => {
      try {
        await app.init({
          background: '#09090b',
          width: dimensions.width,
          height: dimensions.height,
          antialias: true,
          resolution: Math.min(window.devicePixelRatio || 1, 2),
          autoDensity: true,
        });
        if (destroyed) {
          app.destroy(true, { children: true, texture: true, textureSource: true });
          return;
        }
        app.canvas.style.display = 'block';
        app.canvas.addEventListener('webglcontextlost', handleContextLost);
        app.canvas.addEventListener('webglcontextrestored', handleContextRestored);
        container.appendChild(app.canvas);
        appRef.current = app;
      } catch {
        setContextLost(true);
      }
    })();

    return () => {
      destroyed = true;
      if (appRef.current) {
        appRef.current.canvas.removeEventListener('webglcontextlost', handleContextLost);
        appRef.current.canvas.removeEventListener('webglcontextrestored', handleContextRestored);
        if (appRef.current.canvas.parentNode)
          appRef.current.canvas.parentNode.removeChild(appRef.current.canvas);
        appRef.current.destroy(true, { children: true, texture: true, textureSource: true });
        appRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── resize observer ───────────────────────────────────────────────────────
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(entries => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        const w = Math.floor(width);
        const h = Math.floor(height);
        setDimensions({ width: w, height: h });
        appRef.current?.renderer.resize(w, h);
      }
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // ── compute layout (only on graph data / dimension change) ────────────────
  // NOTE: viewMode, dimAlpha etc. do NOT trigger layout recomputation.
  //       Hover state does NOT trigger layout recomputation.
  //       Only nodesProp, edgesProp, centerNodeId (when recentering is wanted),
  //       and dimensions do.
  useEffect(() => {
    if (nodesProp.length === 0 || dimensions.width === 0) return;

    layoutCountRef.current += 1;

    const startLayout = performance.now();
    // For soft-focus, we do NOT pin centreNode (no recentering).
    // For ego-focus (shouldRecenter=true), parent passes centerNodeId.
    // Here we pass it through; the layout pins it.
    const result = computeLayout(
      nodesProp,
      edgesProp,
      centerNodeId,
      dimensions.width,
      dimensions.height,
    );
    lastMetrics.current.layout = Math.round(performance.now() - startLayout);

    // Only reset zoom/pan when centerNodeId actually changes (ego-focus recentering)
    // or when the node set changes (new graph). Do not reset on mode changes.
    viewTransformRef.current = { x: 0, y: 0, scale: 1 };
    mainContainerRef.current?.scale.set(1);
    mainContainerRef.current?.position.set(0, 0);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setZoomVersion(v => v + 1);

    setLayoutData(result);
    // Add a slight delay for smoother visual entrance
    requestAnimationFrame(() => {
      setIsReady(true);
    });
  }, [nodesProp, edgesProp, centerNodeId, dimensions.width, dimensions.height]);

  // ── zoom helpers ──────────────────────────────────────────────────────────
  const zoomTo = useCallback((factor: number) => {
    const mc = mainContainerRef.current;
    if (!mc) return;
    const newScale = Math.max(0.1, Math.min(5, viewTransformRef.current.scale * factor));
    const cx = dimensions.width  / 2;
    const cy = dimensions.height / 2;
    const worldX = (cx - viewTransformRef.current.x) / viewTransformRef.current.scale;
    const worldY = (cy - viewTransformRef.current.y) / viewTransformRef.current.scale;
    viewTransformRef.current.scale = newScale;
    viewTransformRef.current.x     = cx - worldX * newScale;
    viewTransformRef.current.y     = cy - worldY * newScale;
    mc.scale.set(newScale);
    mc.position.set(viewTransformRef.current.x, viewTransformRef.current.y);
    setZoomVersion(v => v + 1);
  }, [dimensions.width, dimensions.height]);

  const resetView = useCallback(() => {
    const mc = mainContainerRef.current;
    if (!mc) return;
    viewTransformRef.current = { x: 0, y: 0, scale: 1 };
    mc.scale.set(1);
    mc.position.set(0, 0);
    setZoomVersion(v => v + 1);
  }, []);

  // ── keyboard virtual cursor ───────────────────────────────────────────────
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const { layoutNodes } = layoutDataRef.current;
      if (layoutNodes.length === 0) return;

      if (e.key === 'Escape') {
        e.preventDefault();
        setKbFocusNodeId(null);
        onBackgroundClickRef.current();
        return;
      }

      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        const id = kbFocusNodeId ?? centerNodeIdRef.current ?? layoutNodes[0]?.id;
        if (!id) return;
        const node = nodesRef.current.find(n => n.id === id);
        if (node) onNodeClickRef.current(node);
        return;
      }

      // Arrow keys: move virtual cursor to nearest neighbour
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault();

      const currentId = kbFocusNodeId ?? centerNodeIdRef.current ?? layoutNodes[0]?.id;
      const current   = layoutNodes.find(n => n.id === currentId);
      if (!current) {
        setKbFocusNodeId(layoutNodes[0]?.id ?? null);
        return;
      }

      // Find candidates: prefer connected neighbours for a natural traversal
      const { layoutEdges } = layoutDataRef.current;
      const connectedNeighbourIds = new Set<string>();
      layoutEdges.forEach(edge => {
        if (edge.source.id === currentId) connectedNeighbourIds.add(edge.target.id);
        if (edge.target.id === currentId) connectedNeighbourIds.add(edge.source.id);
      });

      const dirFilter: (n: LayoutNode) => boolean = (() => {
        switch (e.key) {
          case 'ArrowRight': return n => n.x > current.x + 5;
          case 'ArrowLeft':  return n => n.x < current.x - 5;
          case 'ArrowDown':  return n => n.y > current.y + 5;
          case 'ArrowUp':    return n => n.y < current.y - 5;
          default: return () => false;
        }
      })();

      const candidates = layoutNodes
        .filter(n => n.id !== currentId && dirFilter(n))
        .sort((a, b) => {
          // Prefer connected neighbours
          const aPref = connectedNeighbourIds.has(a.id) ? -10000 : 0;
          const bPref = connectedNeighbourIds.has(b.id) ? -10000 : 0;
          const distA = Math.hypot(a.x - current.x, a.y - current.y) + aPref;
          const distB = Math.hypot(b.x - current.x, b.y - current.y) + bPref;
          return distA - distB;
        });

      if (candidates.length > 0) {
        setKbFocusNodeId(candidates[0].id);
      }
    },
    [kbFocusNodeId],
  );

  // ── render graph scene ────────────────────────────────────────────────────
  useEffect(() => {
    const app = appRef.current;
    if (!app || layoutData.layoutNodes.length === 0 || contextLost) return;

    sceneBuildCountRef.current += 1;

    const startRender          = performance.now();
    const { layoutNodes, layoutEdges } = layoutData;
    const currentCentreId      = centerNodeIdRef.current;
    const currentHoverId       = hoveredNodeId;
    const currentKbId          = kbFocusNodeId;
    const currentViewMode      = viewModeRef.current;
    const isHoverPass          = prevHoveredNodeId.current !== currentHoverId;
    prevHoveredNodeId.current  = currentHoverId;

    const shouldRenderEffects = enableEffects && !prefersReducedMotion;
    const isFocused = currentViewMode !== 'overview';

    // ── connected IDs (for dimming and edge highlighting) ──────────────────
    const connectedIds = new Set<string>();
    const effectiveFocusId = currentHoverId ?? currentCentreId;
    if (isFocused && effectiveFocusId) {
      connectedIds.add(effectiveFocusId);
      layoutEdges.forEach(e => {
        if (e.source.id === effectiveFocusId) connectedIds.add(e.target.id);
        if (e.target.id === effectiveFocusId) connectedIds.add(e.source.id);
      });
    }

    // ── which labels to show ──────────────────────────────────────────────
    const scale       = viewTransformRef.current.scale;
    const labelIdsToShow = selectLabelIds(
      layoutNodes,
      layoutEdges,
      currentCentreId,
      currentHoverId ?? currentKbId,
      currentViewMode,
      scale,
      connectedIds,
    );

    // ── dev logging ───────────────────────────────────────────────────────
    if (process.env.NODE_ENV === 'development') {
      const now = performance.now();
      if (now - lastLogRef.current > 5000) {
        console.log(
          `[NeuralDisc] scene #${sceneBuildCountRef.current} mode=${currentViewMode}` +
          ` hover=${isHoverPass} nodes=${layoutNodes.length} labels=${labelIdsToShow.size}`,
        );
        lastLogRef.current = now;
      }
    }

    // ── build PixiJS scene ────────────────────────────────────────────────
    app.stage.removeChildren();

    const mainContainer = new Container();
    mainContainer.eventMode = 'static';
    mainContainer.cursor = 'grab';
    const tx = viewTransformRef.current;
    mainContainer.scale.set(tx.scale);
    mainContainer.position.set(tx.x, tx.y);
    app.stage.addChild(mainContainer);
    mainContainerRef.current = mainContainer;

    // ── edge layer ────────────────────────────────────────────────────────
    const edgesContainer = new Container();
    edgesContainer.eventMode = 'none';
    mainContainer.addChild(edgesContainer);

    const ambientEdgeGraphics = new Graphics();
    const relatedEdgeGraphics = new Graphics();
    const heroEdgeGraphics    = new Graphics();
    edgesContainer.addChild(ambientEdgeGraphics);
    edgesContainer.addChild(relatedEdgeGraphics);
    edgesContainer.addChild(heroEdgeGraphics);

    const useCurves = useCurvedEdges && currentViewMode !== 'overview';
    const isEgoFocus = currentViewMode === 'ego-focus';

    layoutEdges.forEach(e => {
      let edgeClass: 'ambient' | 'related' | 'hero' = 'ambient';

      if (isFocused) {
        if (e.source.id === effectiveFocusId || e.target.id === effectiveFocusId) {
          edgeClass = 'hero';
        } else if (connectedIds.has(e.source.id) && connectedIds.has(e.target.id)) {
          edgeClass = 'related';
        }
      } else {
        // Overview mode: all edges are ambient
        edgeClass = 'ambient';
      }

      const g = edgeClass === 'hero' ? heroEdgeGraphics 
              : edgeClass === 'related' ? relatedEdgeGraphics 
              : ambientEdgeGraphics;

      if (useCurves) {
        drawCurvedEdge(g, e.source.x, e.source.y, e.target.x, e.target.y, 14);
      } else {
        g.moveTo(e.source.x, e.source.y);
        g.lineTo(e.target.x, e.target.y);
      }

      // Add relationship labels on hero edges in ego-focus mode
      if (isEgoFocus && edgeClass === 'hero') {
        const mx = (e.source.x + e.target.x) / 2;
        const my = (e.source.y + e.target.y) / 2;
        // Subtle relation label
        const relText = new Text({
          text: e.relationship.replace(/_/g, ' '),
          style: new TextStyle({
            fill: 0x71717a,
            fontSize: 7,
            fontFamily: 'system-ui, sans-serif',
            letterSpacing: 0.5,
          }),
        });
        relText.anchor.set(0.5);
        
        if (useCurves) {
          const dx = e.target.x - e.source.x;
          const dy = e.target.y - e.source.y;
          const len = Math.sqrt(dx * dx + dy * dy) || 1;
          const px = -dy / len;
          const py = dx / len;
          relText.x = mx + px * 14;
          relText.y = my + py * 14;
        } else {
          relText.x = mx;
          relText.y = my;
        }

        // Only add text if it's not too cluttered (e.g. limit by zoom or just let them overlap)
        if (scale > 0.8) {
          edgesContainer.addChild(relText);
        }
      }
    });

    // Ambient edge style
    const resolvedDimEdgeAlpha = isFocused ? dimEdgeAlpha : 0.08;
    ambientEdgeGraphics.stroke({ width: 0.8, color: 0x3f3f46, alpha: resolvedDimEdgeAlpha });

    // Related edge style
    relatedEdgeGraphics.stroke({ width: 1.2, color: 0x52525b, alpha: 0.3 });

    // Hero edge style
    heroEdgeGraphics.stroke({ width: 1.8, color: 0x71717a, alpha: 0.65 });

    // ── glow layer ────────────────────────────────────────────────────────
    const glowContainer = new Container();
    if (shouldRenderEffects)
      glowContainer.filters = [new BlurFilter({ strength: 8, quality: 2 })];
    glowContainer.eventMode = 'none';
    mainContainer.addChild(glowContainer);

    // ── particle layer ────────────────────────────────────────────────────
    const particlesContainer = new Container();
    particlesContainer.eventMode = 'none';
    // Only add particle container when particles are enabled
    if (shouldRenderEffects && showParticles) mainContainer.addChild(particlesContainer);

    let tickerCb: ((time: unknown) => void) | null = null;
    let centreCircleGraphics: Graphics | null = null;
    let centreGlowGraphics:   Graphics | null = null;
    const edgeParticles: {
      sprite: Graphics; sx: number; sy: number; ex: number; ey: number;
      speed: number; offset: number;
    }[] = [];

    // ── particle seeding (ego-focus only) ─────────────────────────────────
    if (shouldRenderEffects && showParticles) {
      layoutEdges.forEach(e => {
        const isEdgeActive =
          e.source.id === currentCentreId || e.target.id === currentCentreId;
        if (!isEdgeActive) return;
        const p = new Graphics();
        p.circle(0, 0, 1.5);
        p.fill({ color: 0xd4d4d8, alpha: 0.85 });
        particlesContainer.addChild(p);
        edgeParticles.push({
          sprite: p,
          sx: e.source.x, sy: e.source.y,
          ex: e.target.x, ey: e.target.y,
          speed: 0.25 + Math.random() * 0.35,
          offset: Math.random(),
        });
      });
    }

    // ── nodes layer ───────────────────────────────────────────────────────
    const nodesContainer = new Container();
    mainContainer.addChild(nodesContainer);

    layoutNodes.forEach(n => {
      const colour = COLOR_MAP[n.type] ?? 0x52525b;
      const isCentre     = n.id === currentCentreId;
      const isHovered    = n.id === currentHoverId;
      const isKbFocused  = n.id === currentKbId;
      const isConnected  = connectedIds.has(n.id);
      // In overview: no dimming; in focus modes: dim non-connected
      const isNodeActive = !isFocused || isConnected;

      // ── node radius by role and mode ───────────────────────────────────
      let radius = 8; // overview default
      if (isCentre)    radius = 15;
      else if (isHovered || isKbFocused) radius = 13;
      else if (isFocused) {
        radius = isConnected ? 11 : 7;
      } else {
        // overview: scale by degree (higher-degree nodes are slightly bigger)
        radius = Math.min(11, 7 + Math.floor(n.degree / 3));
      }

      // ── glow ─────────────────────────────────────────────────────────
      if (isNodeActive && shouldRenderEffects) {
        const glow = new Graphics();
        glow.circle(0, 0, isCentre ? 22 : (isHovered || isKbFocused ? 18 : 13));
        glow.fill({
          color: colour,
          alpha: isCentre || isHovered ? 0.35 : (isFocused && isConnected ? 0.20 : 0.10),
        });
        glow.x = n.x;
        glow.y = n.y;
        glowContainer.addChild(glow);
        if (isCentre) centreGlowGraphics = glow;
      }

      // ── circle ───────────────────────────────────────────────────────
      const circle = new Graphics();
      const isConcept = n.type === 'concept' || n.type === 'emotional_state';

      circle.circle(0, 0, radius);
      
      if (isConcept) {
        // Concept nodes are hollow with a colored stroke
        circle.fill({ color: colour, alpha: 0.15 });
        circle.stroke({
          width: isCentre ? 2.5 : 1.5,
          color: colour,
          alpha: isNodeActive ? (isKbFocused ? 1.0 : 0.9) : dimAlpha,
        });
      } else {
        // Standard entity nodes are solid
        circle.fill({ color: colour });
        circle.stroke({
          width: isCentre ? 2.5 : 1.5,
          color: isKbFocused ? 0xffffff : 0x18181b,
          alpha: isKbFocused ? 0.9 : 1.0,
        });
      }

      circle.x      = n.x;
      circle.y      = n.y;
      circle.alpha  = isNodeActive ? 1.0 : dimAlpha;
      if (isCentre) centreCircleGraphics = circle;

      circle.eventMode = 'static';
      circle.cursor    = 'pointer';

      const nodeId = n.id;
      circle.on('pointerdown', e => {
        e.stopPropagation();
        const clicked = nodesRef.current.find(nd => nd.id === nodeId);
        if (clicked) onNodeClickRef.current(clicked);
      });
      circle.on('pointerenter', () => {
        setHoveredNodeId(nodeId);
        setKbFocusNodeId(null); // pointer takes over from keyboard
      });
      circle.on('pointerleave', () => setHoveredNodeId(null));
      nodesContainer.addChild(circle);

      // ── label ────────────────────────────────────────────────────────
      if (labelIdsToShow.has(n.id)) {
        const truncLen = scale < 0.8 ? 14 : 20;
        const labelStr =
          n.label.length > truncLen && !isHovered
            ? n.label.slice(0, truncLen) + '…'
            : n.label;

        // Bigger font for high-degree overview anchors
        let fontSize = 9;
        if (isCentre)               fontSize = 11;
        else if (isHovered || isKbFocused) fontSize = 10;
        else if (currentViewMode === 'overview' && n.degree > 4) fontSize = 10;

        const text = new Text({
          text: labelStr,
          style: new TextStyle({
            fill:       isHovered || isKbFocused ? 0xffffff : 0xd4d4d8,
            fontSize,
            fontFamily: 'system-ui, sans-serif',
            fontWeight: isCentre ? 'bold' : 'normal',
          }),
        });
        text.anchor.set(0.5);
        text.x     = n.x;
        text.y     = n.y + radius + 9;
        text.alpha = isNodeActive ? 1.0 : Math.min(dimAlpha + 0.1, 0.4);
        text.eventMode = 'none';
        nodesContainer.addChild(text);
      }
    });

    // ── ticker: breathe + particles ───────────────────────────────────────
    if (shouldRenderEffects && (centreCircleGraphics || edgeParticles.length > 0)) {
      tickerCb = () => {
        const time = performance.now() / 1000;
        if (centreCircleGraphics && !centreCircleGraphics.destroyed) {
          const breathe = 1 + Math.sin(time * 2.5) * 0.05; // gentle, 0.4 Hz
          centreCircleGraphics.scale.set(breathe);
          if (centreGlowGraphics && !centreGlowGraphics.destroyed) {
            centreGlowGraphics.scale.set(breathe);
            centreGlowGraphics.alpha = 0.35 + Math.sin(time * 2.5) * 0.08;
          }
        }
        edgeParticles.forEach(p => {
          if (!p.sprite.destroyed) {
            const progress = ((time * p.speed) + p.offset) % 1.0;
            p.sprite.x = p.sx + (p.ex - p.sx) * progress;
            p.sprite.y = p.sy + (p.ey - p.sy) * progress;
          }
        });
      };
      app.ticker.add(tickerCb);
    }

    // ── drag-to-pan (with background-click detection) ─────────────────────
    let isDragging  = false;
    let dragStart   = { x: 0, y: 0 };
    let dragStartTx = { x: 0, y: 0 };

    mainContainer.on('pointerdown', e => {
      if (e.target !== mainContainer) return;
      isDragging  = true;
      mainContainer.cursor = 'grabbing';
      dragStart   = { x: e.global.x, y: e.global.y };
      dragStartTx = { ...viewTransformRef.current };
      e.stopPropagation();
    });

    const onPointerMove = (e: { global: { x: number; y: number } }) => {
      if (!isDragging) return;
      const dx = e.global.x - dragStart.x;
      const dy = e.global.y - dragStart.y;
      if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
      viewTransformRef.current.x = dragStartTx.x + dx;
      viewTransformRef.current.y = dragStartTx.y + dy;
      mainContainer.position.set(viewTransformRef.current.x, viewTransformRef.current.y);
    };

    const onPointerUp = (e: { global: { x: number; y: number } }) => {
      if (!isDragging) return;
      isDragging = false;
      mainContainer.cursor = 'grab';
      const dx = e.global.x - dragStart.x;
      const dy = e.global.y - dragStart.y;
      if (Math.abs(dx) < 5 && Math.abs(dy) < 5) {
        onBackgroundClickRef.current();
      }
    };

    mainContainer.on('pointermove', onPointerMove);
    mainContainer.on('pointerup', onPointerUp);
    mainContainer.on('pointerupoutside', () => {
      isDragging = false;
      mainContainer.cursor = 'grab';
    });

    // ── wheel zoom ────────────────────────────────────────────────────────
    mainContainer.on('wheel', e => {
      e.preventDefault();
      const oldScale = viewTransformRef.current.scale;
      const newScale = Math.max(0.1, Math.min(5, oldScale * (e.deltaY > 0 ? 0.9 : 1.1)));
      const worldX = (e.global.x - viewTransformRef.current.x) / oldScale;
      const worldY = (e.global.y - viewTransformRef.current.y) / oldScale;
      viewTransformRef.current.scale = newScale;
      viewTransformRef.current.x     = e.global.x - worldX * newScale;
      viewTransformRef.current.y     = e.global.y - worldY * newScale;
      mainContainer.scale.set(newScale);
      mainContainer.position.set(viewTransformRef.current.x, viewTransformRef.current.y);
      setZoomVersion(v => v + 1);
    });

    // ── metrics ───────────────────────────────────────────────────────────
    const renderTime = Math.round(performance.now() - startRender);
    if (isHoverPass) lastMetrics.current.hover  = renderTime;
    else             lastMetrics.current.render = renderTime;
    diagCallCountRef.current += 1;
    onDiagnosticsRef.current?.({ ...lastMetrics.current });

    return () => {
      if (tickerCb && app && app.ticker) {
        try { app.ticker.remove(tickerCb); } catch { /* ignore */ }
      }
    };
    // Only scene-triggering state. All callbacks accessed through stable refs.
  }, [
    layoutData, hoveredNodeId, kbFocusNodeId,
    contextLost, enableEffects, prefersReducedMotion,
    viewMode, dimAlpha, dimEdgeAlpha, showParticles, useCurvedEdges,
  ]);

  // ── context-loss fallback ─────────────────────────────────────────────────
  if (contextLost) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-zinc-500 bg-zinc-950 p-6 text-center">
        <div className="h-12 w-12 rounded-full border border-red-500/30 flex items-center justify-center mb-3 text-red-400 bg-red-500/10">!</div>
        <p className="text-sm font-medium text-zinc-300">Graphics Context Lost</p>
        <p className="text-xs text-zinc-500 mt-1 max-w-sm">Please refresh to restore the graph.</p>
      </div>
    );
  }

  // eslint-disable-next-line react-hooks/refs
  const zoomPercent = Math.round(viewTransformRef.current.scale * 100);

  return (
    <div
      ref={containerRef}
      className={`w-full h-full relative bg-zinc-950 overflow-hidden focus:outline-none focus-visible:ring-1 focus-visible:ring-zinc-600 focus-visible:ring-inset transition-opacity duration-1000 ${
        isReady ? 'opacity-100' : 'opacity-0'
      }`}
      tabIndex={0}
      role="application"
      aria-label="Rhodey OS Knowledge Graph. Use arrow keys to navigate nodes, Enter to focus, Escape to return to overview."
      onKeyDown={handleKeyDown}
    >
      {/* Zoom controls */}
      <div className="absolute bottom-4 right-4 flex flex-col items-center gap-0.5 z-20 select-none">
        <button
          onClick={() => zoomTo(1.3)}
          className="w-7 h-7 flex items-center justify-center text-xs text-zinc-400 bg-zinc-900/80 border border-zinc-700/50 rounded-t hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
          tabIndex={-1}
          title="Zoom in"
        >+</button>
        <div className="w-7 py-0.5 text-[10px] text-center text-zinc-500 bg-zinc-900/80 border-x border-zinc-700/50 font-mono">
          {zoomPercent}%
        </div>
        <button
          onClick={() => zoomTo(1 / 1.3)}
          className="w-7 h-7 flex items-center justify-center text-xs text-zinc-400 bg-zinc-900/80 border border-zinc-700/50 hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
          tabIndex={-1}
          title="Zoom out"
        >−</button>
        <button
          onClick={resetView}
          className="w-7 h-7 flex items-center justify-center text-[10px] text-zinc-500 bg-zinc-900/80 border border-zinc-700/50 border-t-0 rounded-b hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
          tabIndex={-1}
          title="Reset view"
        >Fit</button>
      </div>
    </div>
  );
}
