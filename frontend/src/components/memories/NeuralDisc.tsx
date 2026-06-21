'use client';

import { useRef, useEffect, useState, useCallback } from 'react';
import * as d3 from 'd3';
import { Application, Graphics, Text, TextStyle, Container, BlurFilter } from 'pixi.js';
import { GraphNode, GraphEdge } from '@/lib/memories/types';

interface NeuralDiscProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  centerNodeId: string | null;
  onNodeClick: (node: GraphNode) => void;
  onBackgroundClick: () => void;
  onDiagnostics?: (metrics: { layout: number; render: number; hover: number }) => void;
  onContextRestored?: () => void;
  enableEffects?: boolean;
}

const colorMap: Record<string, number> = {
  person: 0x3b82f6,
  organization: 0x14b8a6,
  project: 0x8b5cf6,
  cluster: 0xa855f7,
  task: 0xf59e0b,
  concept: 0x71717a,
  emotional_state: 0xf43f5e,
};

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

function computeLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  centerId: string | null,
  width: number,
  height: number,
): { layoutNodes: LayoutNode[]; layoutEdges: LayoutEdge[] } {
  if (nodes.length === 0) return { layoutNodes: [], layoutEdges: [] };

  const simNodes: SimNode[] = nodes.map((n) => ({
    id: n.id,
    label: n.label,
    type: n.type,
  }));

  const simEdges: SimEdge[] = edges
    .filter(
      (e) =>
        nodes.some((n) => n.id === e.source_node_id) &&
        nodes.some((n) => n.id === e.target_node_id),
    )
    .map((e) => ({
      id: e.id,
      relationship: e.relationship,
      source: simNodes.find((n) => n.id === e.source_node_id)!,
      target: simNodes.find((n) => n.id === e.target_node_id)!,
    }));

  if (centerId !== null) {
    const center = simNodes.find((n) => n.id === centerId);
    if (center) {
      center.x = width / 2;
      center.y = height / 2;
      center.fx = width / 2;
      center.fy = height / 2;
    }
  }

  const sim = d3
    .forceSimulation(simNodes)
    .force(
      'link',
      d3.forceLink<SimNode, SimEdge>(simEdges).id((d) => d.id).distance(120).strength(0.6),
    )
    .force('charge', d3.forceManyBody().strength(-300))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide(30));

  sim.tick(300);
  sim.stop();

  const layoutNodes: LayoutNode[] = simNodes.map((n) => ({
    id: n.id,
    label: n.label,
    type: n.type,
    x: n.x ?? width / 2,
    y: n.y ?? height / 2,
  }));

  const nodeMap = new Map<string, LayoutNode>(layoutNodes.map((n) => [n.id, n]));
  const layoutEdges: LayoutEdge[] = simEdges
    .filter((e) => nodeMap.has(e.source.id) && nodeMap.has(e.target.id))
    .map((e) => ({
      id: e.id,
      relationship: e.relationship,
      source: nodeMap.get(e.source.id)!,
      target: nodeMap.get(e.target.id)!,
    }));

  return { layoutNodes, layoutEdges };
}

export default function NeuralDisc({
  nodes: nodesProp,
  edges: edgesProp,
  centerNodeId,
  onNodeClick,
  onBackgroundClick,
  onDiagnostics,
  onContextRestored,
  enableEffects = true,
}: NeuralDiscProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const appRef = useRef<Application | null>(null);
  const mainContainerRef = useRef<Container | null>(null);
  const [dimensions, setDimensions] = useState({ width: 600, height: 600 });
  const [contextLost, setContextLost] = useState(false);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false);
  const [zoomVersion, setZoomVersion] = useState(0);

  // ---- Stable refs for callbacks ----
  const onNodeClickRef = useRef(onNodeClick);
  onNodeClickRef.current = onNodeClick;
  const onBackgroundClickRef = useRef(onBackgroundClick);
  onBackgroundClickRef.current = onBackgroundClick;
  const onDiagnosticsRef = useRef(onDiagnostics);
  onDiagnosticsRef.current = onDiagnostics;
  const onContextRestoredRef = useRef(onContextRestored);
  onContextRestoredRef.current = onContextRestored;
  const nodesRef = useRef(nodesProp);
  nodesRef.current = nodesProp;
  const edgesRef = useRef(edgesProp);
  edgesRef.current = edgesProp;
  const centerNodeIdRef = useRef(centerNodeId);
  centerNodeIdRef.current = centerNodeId;

  // ---- Zoom / Pan transform (persisted across scene rebuilds, reset on new layout) ----
  const viewTransformRef = useRef({ x: 0, y: 0, scale: 1 });

  // ---- Debug counters ----
  const renderCountRef = useRef(0);
  renderCountRef.current += 1;
  const layoutCountRef = useRef(0);
  const sceneBuildCountRef = useRef(0);
  const diagCallCountRef = useRef(0);
  const lastLogRef = useRef(performance.now());

  // --- Track Reduced Motion Live ---
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    setPrefersReducedMotion(mediaQuery.matches);
    const handler = (e: MediaQueryListEvent) => setPrefersReducedMotion(e.matches);
    mediaQuery.addEventListener('change', handler);
    return () => mediaQuery.removeEventListener('change', handler);
  }, []);

  const [layoutData, setLayoutData] = useState<{ layoutNodes: LayoutNode[]; layoutEdges: LayoutEdge[] }>({
    layoutNodes: [],
    layoutEdges: [],
  });
  const lastMetrics = useRef({ layout: 0, render: 0, hover: 0 });
  const prevHoveredNodeId = useRef<string | null>(null);

  // Expose GPU crash for dev testing
  useEffect(() => {
    if (process.env.NODE_ENV !== 'development') return;
    (window as any).__crashPixi = () => {
      if (appRef.current) {
        const renderer: any = appRef.current.renderer;
        const gl = renderer.gl || renderer.context?.gl;
        if (gl) gl.getExtension('WEBGL_lose_context')?.loseContext();
      }
    };
    return () => { if (process.env.NODE_ENV === 'development') delete (window as any).__crashPixi; };
  }, []);

  // --- init PixiJS once ---
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let destroyed = false;
    const app = new Application();

    const handleContextLost = (e: Event) => {
      e.preventDefault();
      setContextLost(true);
      console.warn("WebGL context lost");
    };
    const handleContextRestored = () => {
      console.log("WebGL context restored");
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
        if (destroyed) { app.destroy(true, { children: true, texture: true, textureSource: true }); return; }
        app.canvas.style.display = 'block';
        app.canvas.addEventListener('webglcontextlost', handleContextLost);
        app.canvas.addEventListener('webglcontextrestored', handleContextRestored);
        container.appendChild(app.canvas);
        appRef.current = app;
      } catch (err) {
        console.error("PixiJS initialization failed:", err);
        setContextLost(true);
      }
    })();

    return () => {
      destroyed = true;
      if (appRef.current) {
        appRef.current.canvas.removeEventListener('webglcontextlost', handleContextLost);
        appRef.current.canvas.removeEventListener('webglcontextrestored', handleContextRestored);
        if (appRef.current.canvas.parentNode) appRef.current.canvas.parentNode.removeChild(appRef.current.canvas);
        appRef.current.destroy(true, { children: true, texture: true, textureSource: true });
        appRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- resize observer ---
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver((entries) => {
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

  // --- Compute Layout (Heavy) — resets zoom/pan on new data ---
  useEffect(() => {
    if (nodesProp.length === 0 || dimensions.width === 0) return;

    layoutCountRef.current += 1;
    if (process.env.NODE_ENV === 'development') {
      console.log(`[NeuralDisc] layout #${layoutCountRef.current} — ${nodesProp.length} nodes, ${edgesProp.length} edges`);
    }

    const startLayout = performance.now();
    const result = computeLayout(nodesProp, edgesProp, centerNodeId, dimensions.width, dimensions.height);
    lastMetrics.current.layout = Math.round(performance.now() - startLayout);

    // Reset zoom/pan on new graph data so the full graph is visible
    viewTransformRef.current = { x: 0, y: 0, scale: 1 };
    mainContainerRef.current?.scale.set(1);
    mainContainerRef.current?.position.set(0, 0);
    setZoomVersion(v => v + 1);

    setLayoutData(result);
  }, [nodesProp, edgesProp, centerNodeId, dimensions.width, dimensions.height]);

  // --- Zoom helpers (called from UI buttons) ---
  const zoomTo = useCallback((factor: number) => {
    const mc = mainContainerRef.current;
    if (!mc) return;
    const newScale = Math.max(0.1, Math.min(5, viewTransformRef.current.scale * factor));
    const cx = dimensions.width / 2;
    const cy = dimensions.height / 2;
    const worldX = (cx - viewTransformRef.current.x) / viewTransformRef.current.scale;
    const worldY = (cy - viewTransformRef.current.y) / viewTransformRef.current.scale;
    viewTransformRef.current.scale = newScale;
    viewTransformRef.current.x = cx - worldX * newScale;
    viewTransformRef.current.y = cy - worldY * newScale;
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

  // --- Render graph scene — uses refs for callbacks, mainContainer for zoom/pan ---
  useEffect(() => {
    const app = appRef.current;
    if (!app || layoutData.layoutNodes.length === 0 || contextLost) return;

    sceneBuildCountRef.current += 1;

    const startRender = performance.now();
    const { layoutNodes, layoutEdges } = layoutData;
    const currentCenterId = centerNodeIdRef.current;
    const currentHoverId = hoveredNodeId;
    const isHoverPass = prevHoveredNodeId.current !== currentHoverId;
    prevHoveredNodeId.current = currentHoverId;

    const shouldRenderEffects = enableEffects && !prefersReducedMotion;
    const isHovering = currentHoverId !== null;
    const connectedIds = new Set<string>();
    if (isHovering) {
      connectedIds.add(currentHoverId);
      layoutEdges.forEach(e => {
        if (e.source.id === currentHoverId) connectedIds.add(e.target.id);
        if (e.target.id === currentHoverId) connectedIds.add(e.source.id);
      });
    }

    if (process.env.NODE_ENV === 'development') {
      const now = performance.now();
      if (now - lastLogRef.current > 5000) {
        console.log(`[NeuralDisc] scene #${sceneBuildCountRef.current}, renders=${renderCountRef.current}, layouts=${layoutCountRef.current}, diagCalls=${diagCallCountRef.current}, hover=${isHoverPass}, nodes=${layoutNodes.length}, edges=${layoutEdges.length}`);
        lastLogRef.current = now;
      }
    }

    // ---- Main container: single child of stage, holds all visual layers, supports zoom/pan ----
    app.stage.removeChildren();

    const mainContainer = new Container();
    mainContainer.eventMode = 'static';
    mainContainer.cursor = 'grab';
    const tx = viewTransformRef.current;
    mainContainer.scale.set(tx.scale);
    mainContainer.position.set(tx.x, tx.y);
    app.stage.addChild(mainContainer);
    mainContainerRef.current = mainContainer;

    // ---- Edge layers ----
    const edgesContainer = new Container();
    edgesContainer.eventMode = 'none';
    mainContainer.addChild(edgesContainer);

    const edgeGraphics = new Graphics();
    edgesContainer.addChild(edgeGraphics);
    layoutEdges.forEach((e) => {
      if (!isHovering || e.source.id === currentHoverId || e.target.id === currentHoverId) return;
      edgeGraphics.moveTo(e.source.x, e.source.y);
      edgeGraphics.lineTo(e.target.x, e.target.y);
    });
    edgeGraphics.stroke({ width: 1.2, color: 0x3f3f46, alpha: 0.2 });

    const activeEdgeGraphics = new Graphics();
    edgesContainer.addChild(activeEdgeGraphics);
    layoutEdges.forEach((e) => {
      if (isHovering && e.source.id !== currentHoverId && e.target.id !== currentHoverId) return;
      activeEdgeGraphics.moveTo(e.source.x, e.source.y);
      activeEdgeGraphics.lineTo(e.target.x, e.target.y);
    });
    activeEdgeGraphics.stroke({ width: isHovering ? 2.0 : 1.2, color: isHovering ? 0x71717a : 0x3f3f46, alpha: isHovering ? 0.9 : 0.6 });

    // ---- Glow layer ----
    const glowContainer = new Container();
    if (shouldRenderEffects) glowContainer.filters = [new BlurFilter({ strength: 8, quality: 2 })];
    glowContainer.eventMode = 'none';
    mainContainer.addChild(glowContainer);

    // ---- Nodes layer ----
    const nodesContainer = new Container();
    mainContainer.addChild(nodesContainer);

    // ---- Particle layer ----
    const particlesContainer = new Container();
    particlesContainer.eventMode = 'none';
    if (shouldRenderEffects) mainContainer.addChild(particlesContainer);

    let tickerCb: ((time: any) => void) | null = null;
    let centerCircleGraphics: Graphics | null = null;
    let centerGlowGraphics: Graphics | null = null;
    const edgeParticles: { sprite: Graphics; sx: number; sy: number; ex: number; ey: number; speed: number; offset: number }[] = [];

    if (shouldRenderEffects) {
      layoutEdges.forEach((e) => {
        const isEdgeActive = isHovering
          ? (e.source.id === currentHoverId || e.target.id === currentHoverId)
          : (e.source.id === currentCenterId || e.target.id === currentCenterId);
        if (!isEdgeActive) return;
        const p = new Graphics();
        p.circle(0, 0, 1.5);
        p.fill({ color: 0xd4d4d8, alpha: 0.9 });
        particlesContainer.addChild(p);
        edgeParticles.push({ sprite: p, sx: e.source.x, sy: e.source.y, ex: e.target.x, ey: e.target.y, speed: 0.3 + Math.random() * 0.4, offset: Math.random() });
      });
    }

    layoutNodes.forEach((n) => {
      const color = colorMap[n.type] ?? 0x52525b;
      const isCenter = n.id === currentCenterId;
      const isNodeActive = !isHovering || connectedIds.has(n.id);
      const isDirectHover = n.id === currentHoverId;

      if (isNodeActive && shouldRenderEffects) {
        const glow = new Graphics();
        glow.circle(0, 0, isCenter ? 20 : (isDirectHover ? 18 : 14));
        glow.fill({ color, alpha: isCenter || isDirectHover ? 0.4 : 0.15 });
        glow.x = n.x;
        glow.y = n.y;
        glowContainer.addChild(glow);
        if (isCenter) centerGlowGraphics = glow;
      }

      const circle = new Graphics();
      circle.circle(0, 0, isCenter ? 14 : (isDirectHover ? 12 : 10));
      circle.fill({ color });
      circle.stroke({ width: isCenter ? 2.5 : 1.5, color: 0x18181b });
      circle.x = n.x;
      circle.y = n.y;
      circle.alpha = isNodeActive ? 1.0 : 0.2;
      if (isCenter) centerCircleGraphics = circle;
      circle.eventMode = 'static';
      circle.cursor = 'pointer';

      const nodeId = n.id;
      circle.on('pointerdown', (e) => {
        e.stopPropagation();
        const clicked = nodesRef.current.find((nd) => nd.id === nodeId);
        if (clicked) onNodeClickRef.current(clicked);
      });
      circle.on('pointerenter', () => setHoveredNodeId(nodeId));
      circle.on('pointerleave', () => setHoveredNodeId(null));
      nodesContainer.addChild(circle);

      if (isCenter || isDirectHover || (isHovering && isNodeActive)) {
        const label = n.label.length > 16 && !isDirectHover ? n.label.slice(0, 16) + '...' : n.label;
        const text = new Text({
          text: label,
          style: new TextStyle({ fill: isDirectHover ? 0xffffff : 0xd4d4d8, fontSize: isDirectHover ? 10 : 9, fontFamily: 'system-ui, sans-serif' }),
        });
        text.anchor.set(0.5);
        text.x = n.x;
        text.y = n.y + (isCenter ? 22 : (isDirectHover ? 20 : 18));
        text.alpha = isNodeActive ? 1.0 : 0.2;
        text.eventMode = 'none';
        nodesContainer.addChild(text);
      }
    });

    if (shouldRenderEffects && (centerCircleGraphics || edgeParticles.length > 0)) {
      tickerCb = () => {
        const time = performance.now() / 1000;
        if (centerCircleGraphics && !centerCircleGraphics.destroyed) {
          const breathe = 1 + Math.sin(time * 3) * 0.06;
          centerCircleGraphics.scale.set(breathe);
          if (centerGlowGraphics && !centerGlowGraphics.destroyed) {
            centerGlowGraphics.scale.set(breathe);
            centerGlowGraphics.alpha = 0.4 + Math.sin(time * 3) * 0.1;
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

    // ---- Wheel zoom (zoom toward mouse cursor) ----
    mainContainer.on('wheel', (e) => {
      e.preventDefault();
      const oldScale = viewTransformRef.current.scale;
      const newScale = Math.max(0.1, Math.min(5, oldScale * (e.deltaY > 0 ? 0.9 : 1.1)));
      const worldX = (e.global.x - viewTransformRef.current.x) / oldScale;
      const worldY = (e.global.y - viewTransformRef.current.y) / oldScale;
      viewTransformRef.current.scale = newScale;
      viewTransformRef.current.x = e.global.x - worldX * newScale;
      viewTransformRef.current.y = e.global.y - worldY * newScale;
      mainContainer.scale.set(newScale);
      mainContainer.position.set(viewTransformRef.current.x, viewTransformRef.current.y);
      setZoomVersion(v => v + 1);
    });

    // ---- Drag-to-pan with click/drag detection ----
    let isDragging = false;
    let dragStart = { x: 0, y: 0 };
    let dragStartTx = { x: 0, y: 0 };

    mainContainer.on('pointerdown', (e) => {
      if (e.target !== mainContainer) return;
      isDragging = true;
      mainContainer.cursor = 'grabbing';
      dragStart = { x: e.global.x, y: e.global.y };
      dragStartTx = { ...viewTransformRef.current };
      e.stopPropagation();
    });

    const onPointerMove = (e: any) => {
      if (!isDragging) return;
      const dx = e.global.x - dragStart.x;
      const dy = e.global.y - dragStart.y;
      if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
      viewTransformRef.current.x = dragStartTx.x + dx;
      viewTransformRef.current.y = dragStartTx.y + dy;
      mainContainer.position.set(viewTransformRef.current.x, viewTransformRef.current.y);
    };

    const onPointerUp = (e: any) => {
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

    const endRender = performance.now();
    const renderTime = Math.round(endRender - startRender);
    if (isHoverPass) { lastMetrics.current.hover = renderTime; }
    else { lastMetrics.current.render = renderTime; }

    diagCallCountRef.current += 1;
    onDiagnosticsRef.current?.({ ...lastMetrics.current });

    return () => {
      if (tickerCb && app && app.ticker) {
        try { app.ticker.remove(tickerCb); } catch (e) {}
      }
    };
    // Deps: only scene-triggering state. Callbacks accessed through stable refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutData, hoveredNodeId, contextLost, enableEffects, prefersReducedMotion]);

  const zoomPercent = Math.round(viewTransformRef.current.scale * 100);

  if (contextLost) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-zinc-500 bg-zinc-950 p-6 text-center">
        <div className="h-12 w-12 rounded-full border border-red-500/30 flex items-center justify-center mb-3 text-red-400 bg-red-500/10">!</div>
        <p className="text-sm font-medium text-zinc-300">Graphics Context Lost</p>
        <p className="text-xs text-zinc-500 mt-1 max-w-sm">Please refresh to restore.</p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="w-full h-full relative bg-zinc-950 overflow-hidden">
      {/* Zoom controls */}
      <div className="absolute bottom-4 right-4 flex flex-col items-center gap-0.5 z-20 select-none">
        <button
          onClick={() => zoomTo(1.3)}
          className="w-7 h-7 flex items-center justify-center text-xs text-zinc-400 bg-zinc-900/80 border border-zinc-700/50 rounded-t hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
          title="Zoom in"
        >+</button>
        <div className="w-7 py-0.5 text-[10px] text-center text-zinc-500 bg-zinc-900/80 border-x border-zinc-700/50 font-mono">
          {zoomPercent}%
        </div>
        <button
          onClick={() => zoomTo(1 / 1.3)}
          className="w-7 h-7 flex items-center justify-center text-xs text-zinc-400 bg-zinc-900/80 border border-zinc-700/50 hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
          title="Zoom out"
        >−</button>
        <button
          onClick={resetView}
          className="w-7 h-7 flex items-center justify-center text-[10px] text-zinc-500 bg-zinc-900/80 border border-zinc-700/50 border-t-0 rounded-b hover:bg-zinc-800 hover:text-zinc-200 transition-colors"
          title="Reset view"
        >Fit</button>
      </div>
    </div>
  );
}
