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
  dimAlpha: number;
  dimEdgeAlpha: number;
  showParticles: boolean;
  useCurvedEdges: boolean;
  onNodeClick: (node: GraphNode) => void;
  onBackgroundClick: () => void;
  onDiagnostics?: (metrics: { layout: number; render: number; hover: number }) => void;
  onContextRestored?: () => void;
  enableEffects?: boolean;
  loading?: boolean;
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
  // Base 3D coordinates
  x: number;
  y: number;
  z: number;
  radialNorm: number; // 0 (center) to 1 (outer edge)
  degree: number;
  // Transient projection data
  projX: number;
  projY: number;
  projScale: number;
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

  const simEdges = edges
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
      centre.x  = 0;
      centre.y  = 0;
      centre.fx = 0;
      centre.fy = 0;
    }
  }

  const sim = d3
    .forceSimulation(simNodes)
    .force(
      'link',
      d3.forceLink<SimNode, typeof simEdges[0]>(simEdges)
        .id(d => d.id)
        .distance(50)
        .strength(0.4),
    )
    .force('charge', d3.forceManyBody().strength(-120))
    .force('centre', d3.forceCenter(0, 0))
    .force('collide', d3.forceCollide(20));

  sim.tick(500);
  sim.stop();

  // Find max radius to bound the sphere
  let maxR = 1;
  simNodes.forEach(n => {
    const r = Math.sqrt((n.x ?? 0)**2 + (n.y ?? 0)**2);
    if (r > maxR) maxR = r;
  });

  const layoutNodes: LayoutNode[] = simNodes.map((n, i) => {
    const nx = n.x ?? 0;
    const ny = n.y ?? 0;
    const r = Math.sqrt(nx*nx + ny*ny);
    
    // Inflate flat layout into a sphere. 
    // Outer nodes curve backwards or forwards.
    // Pseudo-random hemisphere assignment based on index to distribute evenly.
    const hemisphere = (i % 2 === 0) ? 1 : -1;
    // z = sqrt(R^2 - r^2) * hemisphere
    // We scale Z slightly down to make it an oblate spheroid (disc-like) for better reading
    let nz = Math.sqrt(Math.max(0, maxR*maxR - r*r)) * hemisphere * 0.7;
    
    // Core node stays exactly at 0,0,0
    if (n.id === centerId) nz = 0;

    return {
      id: n.id,
      label: n.label,
      type: n.type,
      x: nx,
      y: ny,
      z: nz,
      radialNorm: maxR > 0 ? (r / maxR) : 0,
      degree: degreeMap.get(n.id) ?? 0,
      projX: 0, projY: 0, projScale: 1
    };
  });

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
  loading = false,
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
    );
    lastMetrics.current.layout = Math.round(performance.now() - startLayout);

    // Only reset zoom/pan when centerNodeId actually changes (ego-focus recentering)
    // or when the node set changes (new graph). Do not reset on mode changes.
    viewTransformRef.current = { x: dimensions.width / 2, y: dimensions.height / 2, scale: 1 };
    mainContainerRef.current?.scale.set(1);
    mainContainerRef.current?.position.set(dimensions.width / 2, dimensions.height / 2);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setZoomVersion(v => v + 1);

    setLayoutData(result);
    setIsReady(true);
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
    viewTransformRef.current = { x: dimensions.width / 2, y: dimensions.height / 2, scale: 1 };
    mc.scale.set(1);
    mc.position.set(dimensions.width / 2, dimensions.height / 2);
    setZoomVersion(v => v + 1);
  }, [dimensions.width, dimensions.height]);

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
    mainContainer.sortableChildren = true;
    app.stage.addChild(mainContainer);
    mainContainerRef.current = mainContainer;

    // ── layers ────────────────────────────────────────────────────────────
    const backEdgesGraphics = new Graphics();
    backEdgesGraphics.zIndex = -1000;
    mainContainer.addChild(backEdgesGraphics);

    const frontEdgesGraphics = new Graphics();
    frontEdgesGraphics.zIndex = 1000;
    mainContainer.addChild(frontEdgesGraphics);

    const glowContainer = new Container();
    if (shouldRenderEffects)
      glowContainer.filters = [new BlurFilter({ strength: 8, quality: 2 })];
    glowContainer.eventMode = 'none';
    glowContainer.zIndex = -500;
    mainContainer.addChild(glowContainer);

    const particlesContainer = new Container();
    particlesContainer.eventMode = 'none';
    particlesContainer.zIndex = 1001;
    if (shouldRenderEffects && showParticles) mainContainer.addChild(particlesContainer);

    const nodesContainer = new Container();
    nodesContainer.sortableChildren = true;
    nodesContainer.zIndex = 0;
    mainContainer.addChild(nodesContainer);

    // ── scene data prep ───────────────────────────────────────────────────
    const useCurves = useCurvedEdges && currentViewMode !== 'overview';
    const isEgoFocus = currentViewMode === 'ego-focus';

    interface RenderNode {
      layoutNode: LayoutNode;
      circle: Graphics;
      glow?: Graphics;
      text?: Text;
      isCentre: boolean;
      isHovered: boolean;
      isKbFocused: boolean;
      radius: number;
    }
    const renderNodes: RenderNode[] = [];

    layoutNodes.forEach(n => {
      const colour = COLOR_MAP[n.type] ?? 0x52525b;
      const isCentre     = n.id === currentCentreId;
      const isHovered    = n.id === currentHoverId;
      const isKbFocused  = n.id === currentKbId;
      const isConnected  = connectedIds.has(n.id);
      const isNodeActive = !isFocused || isConnected;

      // ── node radius by role and mode ───────────────────────────────────
      let radius = 6;
      if (isCentre)    radius = 22;
      else if (isHovered || isKbFocused) radius = 13;
      else if (isFocused) {
        radius = isConnected ? 9 : 5;
      } else {
        radius = Math.min(9, 6 + Math.floor(n.degree / 4));
      }

      // ── glow ─────────────────────────────────────────────────────────
      let glow: Graphics | undefined;
      if (isNodeActive && shouldRenderEffects) {
        glow = new Graphics();
        glow.circle(0, 0, isCentre ? 36 : (isHovered || isKbFocused ? 18 : 13));
        glow.fill({
          color: colour,
          alpha: isCentre || isHovered ? 0.35 : (isFocused && isConnected ? 0.20 : 0.10),
        });
        glowContainer.addChild(glow);
      }

      // ── circle ───────────────────────────────────────────────────────
      const circle = new Graphics();
      
      const isPerson = n.type === 'person';
      const isConcept = n.type === 'concept' || n.type === 'emotional_state';
      const isProject = n.type === 'project';
      const isOrganization = n.type === 'organization';
      const isCluster = n.type === 'cluster';

      if (isPerson) {
        // Person: Solid nucleus with a soft inner glow/dot
        circle.circle(0, 0, radius);
        circle.fill({ color: colour });
        circle.circle(0, 0, radius * 0.4);
        circle.fill({ color: 0xffffff, alpha: 0.3 }); // Inner highlight
      } else if (isOrganization) {
        // Organization: Square/Diamond-like or just a thick robust ring
        circle.roundRect(-radius, -radius, radius * 2, radius * 2, radius * 0.3);
        circle.fill({ color: colour, alpha: 0.8 });
        circle.stroke({ width: 2, color: 0xffffff, alpha: 0.2 });
      } else if (isProject) {
        // Project: Hexagon or just a distinct geometric node
        circle.poly([
          0, -radius,
          radius * 0.866, -radius * 0.5,
          radius * 0.866, radius * 0.5,
          0, radius,
          -radius * 0.866, radius * 0.5,
          -radius * 0.866, -radius * 0.5,
        ]);
        circle.fill({ color: colour, alpha: 0.9 });
      } else if (isCluster) {
        // Cluster: Multiple concentric rings
        circle.circle(0, 0, radius);
        circle.stroke({ width: 1.5, color: colour, alpha: 0.8 });
        circle.circle(0, 0, radius * 0.6);
        circle.stroke({ width: 1, color: colour, alpha: 0.5 });
        circle.circle(0, 0, radius * 0.2);
        circle.fill({ color: colour, alpha: 0.9 });
      } else if (isConcept) {
        // Concept: Hollow orbital ring with a tiny core
        circle.circle(0, 0, radius);
        circle.fill({ color: colour, alpha: 0.1 });
        circle.stroke({ width: 1.5, color: colour, alpha: 0.8 });
        circle.circle(0, 0, radius * 0.2);
        circle.fill({ color: colour, alpha: 0.6 });
      } else {
        // Default (Task, Place, Memory, etc.)
        circle.circle(0, 0, radius * 0.8);
        circle.fill({ color: colour, alpha: 0.6 });
      }

      // Selection ring for keyboard/hover
      if (isKbFocused || isHovered) {
        circle.circle(0, 0, radius + 4);
        circle.stroke({ width: 1, color: 0xffffff, alpha: 0.5 });
      }

      circle.alpha  = isNodeActive ? 1.0 : dimAlpha;
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
        setKbFocusNodeId(null);
      });
      circle.on('pointerleave', () => setHoveredNodeId(null));
      nodesContainer.addChild(circle);

      // ── label ────────────────────────────────────────────────────────
      let text: Text | undefined;
      if (labelIdsToShow.has(n.id)) {
        const truncLen = scale < 0.8 ? 14 : 20;
        const labelStr =
          n.label.length > truncLen && !isHovered
            ? n.label.slice(0, truncLen) + '…'
            : n.label;

        let fontSize = 9;
        if (isCentre)               fontSize = 13;
        else if (isHovered || isKbFocused) fontSize = 10;
        else if (currentViewMode === 'overview' && n.degree > 4) fontSize = 10;

        text = new Text({
          text: labelStr,
          style: new TextStyle({
            fill:       isHovered || isKbFocused ? 0xffffff : 0xd4d4d8,
            fontSize,
            fontFamily: 'system-ui, sans-serif',
            fontWeight: isCentre ? 'bold' : 'normal',
          }),
        });
        text.anchor.set(0.5);
        text.alpha = isNodeActive ? 1.0 : Math.min(dimAlpha + 0.1, 0.4);
        text.eventMode = 'none';
        nodesContainer.addChild(text);
      }

      renderNodes.push({ layoutNode: n, circle, glow, text, isCentre, isHovered, isKbFocused, radius });
    });

    interface RenderEdge {
      layoutEdge: LayoutEdge;
      edgeClass: 'ambient' | 'related' | 'hero';
      relText?: Text;
    }
    const renderEdges: RenderEdge[] = [];

    layoutEdges.forEach(e => {
      let edgeClass: 'ambient' | 'related' | 'hero' = 'ambient';
      if (isFocused) {
        if (e.source.id === effectiveFocusId || e.target.id === effectiveFocusId) {
          edgeClass = 'hero';
        } else if (connectedIds.has(e.source.id) && connectedIds.has(e.target.id)) {
          edgeClass = 'related';
        }
      }

      let relText: Text | undefined;
      if (isEgoFocus && edgeClass === 'hero' && scale > 0.8) {
        relText = new Text({
          text: e.relationship.replace(/_/g, ' '),
          style: new TextStyle({
            fill: 0x71717a,
            fontSize: 7,
            fontFamily: 'system-ui, sans-serif',
            letterSpacing: 0.5,
          }),
        });
        relText.anchor.set(0.5);
        mainContainer.addChild(relText);
      }

      renderEdges.push({ layoutEdge: e, edgeClass, relText });
    });

    const edgeParticles: {
      sprite: Graphics; sx: number; sy: number; ex: number; ey: number;
      sz: number; ez: number;
      speed: number; offset: number;
    }[] = [];

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
          sx: e.source.x, sy: e.source.y, sz: e.source.z,
          ex: e.target.x, ey: e.target.y, ez: e.target.z,
          speed: 0.25 + Math.random() * 0.35,
          offset: Math.random(),
        });
      });
    }

    // ── ticker: 3D Projection & Rendering ─────────────────────────────────
    let tickerCb: ((time: unknown) => void) | null = null;
    
    tickerCb = () => {
      const time = performance.now() / 1000;
      
      // Gentle orbital rotation
      const yaw = time * 0.08;
      const pitch = Math.sin(time * 0.1) * 0.15;
      
      const cosY = Math.cos(yaw);
      const sinY = Math.sin(yaw);
      const cosP = Math.cos(pitch);
      const sinP = Math.sin(pitch);
      
      const FOV = 1000;

      // 1. Project nodes
      renderNodes.forEach(rn => {
        const n = rn.layoutNode;
        // Apply Yaw (around Y axis)
        const x1 = n.x * cosY - n.z * sinY;
        const z1 = n.z * cosY + n.x * sinY;
        // Apply Pitch (around X axis)
        const y2 = n.y * cosP - z1 * sinP;
        const z2 = z1 * cosP + n.y * sinP;
        
        // Perspective divide
        const safeZ2 = Math.max(-FOV + 10, z2);
        const scaleFact = FOV / (FOV + safeZ2);
        n.projX = x1 * scaleFact;
        n.projY = y2 * scaleFact;
        n.projScale = scaleFact;

        // Apply to sprites
        rn.circle.x = n.projX;
        rn.circle.y = n.projY;
        
        // Subtle breathing for active nodes
        const breathe = rn.isCentre ? 1 + Math.sin(time * 2.5) * 0.05 : 1;
        rn.circle.scale.set(scaleFact * breathe);
        rn.circle.zIndex = -z2; // PixiJS draws higher zIndex on top. Smaller z2 = closer = higher zIndex.

        // Alpha falloff for distant nodes
        if (rn.circle.alpha > dimAlpha) {
           // Majestic falloff: scaleFact (z-depth) + radialNorm (outer edges fade into darkness)
           const radialDim = 1.0 - (n.radialNorm * 0.65); 
           rn.circle.alpha = Math.max(dimAlpha, Math.min(1.0, scaleFact) * radialDim);
        }

        if (rn.glow) {
          rn.glow.x = n.projX;
          rn.glow.y = n.projY;
          rn.glow.scale.set(scaleFact * breathe);
          if (rn.isCentre) {
             rn.glow.alpha = 0.35 + Math.sin(time * 2.5) * 0.08;
          } else {
             // Glow also fades out on the periphery
             rn.glow.alpha = Math.max(0, rn.glow.alpha * (1.0 - n.radialNorm * 0.5));
          }
        }

        if (rn.text) {
          rn.text.x = n.projX;
          // Position label below the scaled node
          rn.text.y = n.projY + (rn.radius * scaleFact * breathe) + 9;
          rn.text.scale.set(Math.max(0.6, scaleFact));
          // Fade text on the periphery
          if (rn.text.alpha > dimAlpha) {
             rn.text.alpha = Math.max(dimAlpha, Math.min(1.0, scaleFact) * (1.0 - n.radialNorm * 0.5));
          }
        }
      });
      
      // Sort nodes for correct occlusion
      nodesContainer.sortChildren();

      // 2. Draw Edges
      backEdgesGraphics.clear();
      frontEdgesGraphics.clear();
      
      const resolvedDimEdgeAlpha = isFocused ? dimEdgeAlpha : 0.08;

      renderEdges.forEach(re => {
        const s = re.layoutEdge.source;
        const t = re.layoutEdge.target;
        
        // Average Z to determine back/front occlusion
        // Approximation: if both are behind origin, draw in back container.
        const zAvg = (s.projScale + t.projScale) / 2;
        const isBack = zAvg < 1.0; 
        const g = isBack ? backEdgesGraphics : frontEdgesGraphics;
        
        const rAvg = (s.radialNorm + t.radialNorm) / 2;
        const radialDim = 1.0 - (rAvg * 0.65);

        const color = re.edgeClass === 'hero' ? 0x71717a 
                  : re.edgeClass === 'related' ? 0x52525b 
                  : 0x3f3f46;
        const width = re.edgeClass === 'hero' ? 1.8 
                  : re.edgeClass === 'related' ? 1.2 
                  : 0.8;
        const baseAlpha = re.edgeClass === 'ambient' ? resolvedDimEdgeAlpha 
                  : re.edgeClass === 'related' ? 0.3 
                  : 0.65;
        
        // Majestic dimming for edges
        const alpha = Math.max(resolvedDimEdgeAlpha, baseAlpha * zAvg * radialDim);

        g.stroke({ width: width * zAvg, color, alpha });

        if (useCurves) {
          drawCurvedEdge(g, s.projX, s.projY, t.projX, t.projY, 14 * zAvg);
        } else {
          g.moveTo(s.projX, s.projY);
          g.lineTo(t.projX, t.projY);
        }

        if (re.relText) {
          const mx = (s.projX + t.projX) / 2;
          const my = (s.projY + t.projY) / 2;
          if (useCurves) {
            const dx = t.projX - s.projX;
            const dy = t.projY - s.projY;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const px = -dy / len;
            const py = dx / len;
            re.relText.x = mx + px * 14 * zAvg;
            re.relText.y = my + py * 14 * zAvg;
          } else {
            re.relText.x = mx;
            re.relText.y = my;
          }
          re.relText.scale.set(zAvg);
          // Label is front/back based on edge
          re.relText.zIndex = isBack ? -999 : 999;
        }
      });
      mainContainer.sortChildren();

      // 3. Particles
      edgeParticles.forEach(p => {
        if (!p.sprite.destroyed) {
          const progress = ((time * p.speed) + p.offset) % 1.0;
          
          // Interpolate 3D coordinates
          const x = p.sx + (p.ex - p.sx) * progress;
          const y = p.sy + (p.ey - p.sy) * progress;
          const z = p.sz + (p.ez - p.sz) * progress;
          
          // Project
          const x1 = x * cosY - z * sinY;
          const z1 = z * cosY + x * sinY;
          const y2 = y * cosP - z1 * sinP;
          const z2 = z1 * cosP + y * sinP;
          
          const safeZ2 = Math.max(-FOV + 10, z2);
          const scaleFact = FOV / (FOV + safeZ2);
          p.sprite.x = x1 * scaleFact;
          p.sprite.y = y2 * scaleFact;
          p.sprite.scale.set(scaleFact);
          p.sprite.zIndex = -z2;
        }
      });
      if (shouldRenderEffects && showParticles) {
        particlesContainer.sortChildren();
      }
    };
    app.ticker.add(tickerCb);

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
    <div className="relative w-full h-full">
      {(loading && nodesProp.length === 0) && (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-zinc-500 bg-zinc-950 z-20 pointer-events-none">
          <div className="h-6 w-6 rounded-full border-2 border-teal-500/20 border-t-teal-500 animate-spin mb-3" />
          <p className="text-xs uppercase tracking-widest font-semibold animate-pulse">Loading Knowledge Graph...</p>
        </div>
      )}
      <div
        ref={containerRef}
        className={`w-full h-full relative bg-zinc-950 overflow-hidden focus:outline-none focus-visible:ring-1 focus-visible:ring-zinc-600 focus-visible:ring-inset transition-opacity duration-300 ${
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
    </div>
  );
}
