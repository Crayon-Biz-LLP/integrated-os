'use client';

import { useRef, useEffect, useState } from 'react';
import * as d3 from 'd3';
import { Application, Graphics, Text, TextStyle, Container, BlurFilter } from 'pixi.js';
import { GraphNode, GraphEdge } from '@/lib/memories/types';

interface NeuralDiscProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  centerNodeId: number | null;
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
  id: number;
  label: string;
  type: string;
}

interface LayoutNode {
  id: number;
  label: string;
  type: string;
  x: number;
  y: number;
}

interface SimEdge {
  id: number;
  relationship: string;
  source: SimNode;
  target: SimNode;
}

interface LayoutEdge {
  id: number;
  relationship: string;
  source: LayoutNode;
  target: LayoutNode;
}

function computeLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  centerId: number | null,
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

  const nodeMap = new Map<number, LayoutNode>(layoutNodes.map((n) => [n.id, n]));
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
  nodes,
  edges,
  centerNodeId,
  onNodeClick,
  onBackgroundClick,
  onDiagnostics,
  onContextRestored,
  enableEffects = true,
}: NeuralDiscProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const appRef = useRef<Application | null>(null);
  const [dimensions, setDimensions] = useState({ width: 600, height: 600 });
  const [contextLost, setContextLost] = useState(false);
  const [hoveredNodeId, setHoveredNodeId] = useState<number | null>(null);
  const [prefersReducedMotion, setPrefersReducedMotion] = useState(false);

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
  const prevHoveredNodeId = useRef<number | null>(null);

  // Expose a method to intentionally crash the context for dev testing
  useEffect(() => {
    if (process.env.NODE_ENV !== 'development') return;

    (window as any).__crashPixi = () => {
      if (appRef.current) {
        const renderer: any = appRef.current.renderer;
        const gl = renderer.gl || renderer.context?.gl;
        if (gl) {
          gl.getExtension('WEBGL_lose_context')?.loseContext();
        }
      }
    };
    return () => {
      if (process.env.NODE_ENV === 'development') {
        delete (window as any).__crashPixi;
      }
    };
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
      if (onContextRestored) {
        onContextRestored();
      }
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
        
        // Explicitly sever DOM linkage to prevent detached canvas leaks across remounts
        if (appRef.current.canvas.parentNode) {
          appRef.current.canvas.parentNode.removeChild(appRef.current.canvas);
        }
        
        // Aggressive deep-clean destruction
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
        if (appRef.current) {
          appRef.current.renderer.resize(w, h);
        }
      }
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  // --- Compute Layout (Heavy) ---
  useEffect(() => {
    if (nodes.length === 0 || dimensions.width === 0) return;

    const startLayout = performance.now();
    const result = computeLayout(
      nodes,
      edges,
      centerNodeId,
      dimensions.width,
      dimensions.height,
    );
    const endLayout = performance.now();
    lastMetrics.current.layout = Math.round(endLayout - startLayout);

    setLayoutData(result);
  }, [nodes, edges, centerNodeId, dimensions.width, dimensions.height]);

  // --- render graph (Light) ---
  useEffect(() => {
    const app = appRef.current;
    if (!app || layoutData.layoutNodes.length === 0 || contextLost) return;

    const startRender = performance.now();
    const { layoutNodes, layoutEdges } = layoutData;

    // Determine if this render pass was triggered purely by a hover state change
    const isHoverPass = prevHoveredNodeId.current !== hoveredNodeId;
    prevHoveredNodeId.current = hoveredNodeId;

    // Determine if we should render heavy visual effects
    const shouldRenderEffects = enableEffects && !prefersReducedMotion;

    // Determine which nodes/edges are active based on hover
    const isHovering = hoveredNodeId !== null;
    const connectedIds = new Set<number>();
    
    if (isHovering) {
      connectedIds.add(hoveredNodeId!);
      layoutEdges.forEach(e => {
        if (e.source.id === hoveredNodeId) connectedIds.add(e.target.id);
        if (e.target.id === hoveredNodeId) connectedIds.add(e.source.id);
      });
    }

    // Determine if we should render heavy visual effects

    app.stage.removeChildren();

    const edgesContainer = new Container();
    edgesContainer.eventMode = 'none'; // Skip interaction crawling for edge layers
    app.stage.addChild(edgesContainer);

    const edgeGraphics = new Graphics();
    edgesContainer.addChild(edgeGraphics);

    // Draw non-hovered edges first (dimmed)
    layoutEdges.forEach((e) => {
      const isEdgeHovered = !isHovering || (e.source.id === hoveredNodeId || e.target.id === hoveredNodeId);
      if (isEdgeHovered) return;
      
      edgeGraphics.moveTo(e.source.x, e.source.y);
      edgeGraphics.lineTo(e.target.x, e.target.y);
    });
    edgeGraphics.stroke({ width: 1.2, color: 0x3f3f46, alpha: 0.2 });

    const activeEdgeGraphics = new Graphics();
    activeEdgeGraphics.eventMode = 'none'; // Skip interaction crawling
    edgesContainer.addChild(activeEdgeGraphics);

    // Draw hovered edges
    layoutEdges.forEach((e) => {
      const isEdgeHovered = !isHovering || (e.source.id === hoveredNodeId || e.target.id === hoveredNodeId);
      if (!isEdgeHovered) return;
      
      activeEdgeGraphics.moveTo(e.source.x, e.source.y);
      activeEdgeGraphics.lineTo(e.target.x, e.target.y);
    });
    activeEdgeGraphics.stroke({ width: isHovering ? 2.0 : 1.2, color: isHovering ? 0x71717a : 0x3f3f46, alpha: isHovering ? 0.9 : 0.6 });

    const nodesContainer = new Container();
    // Container itself is passive, children are static targets
    
    // --- GLOW LAYER ---
    const glowContainer = new Container();
    if (shouldRenderEffects) {
      // Quality 2 reduces passes dramatically compared to Quality 4, easing GPU load
      const blurFilter = new BlurFilter({ strength: 8, quality: 2 });
      glowContainer.filters = [blurFilter];
    }
    glowContainer.eventMode = 'none';
    app.stage.addChild(glowContainer);
    
    app.stage.addChild(nodesContainer);

    // --- EDGE PARTICLES LAYER ---
    const particlesContainer = new Container();
    particlesContainer.eventMode = 'none';
    if (shouldRenderEffects) {
      app.stage.addChild(particlesContainer);
    }

    let tickerCb: ((time: any) => void) | null = null;
    let centerCircleRef: Graphics | null = null;
    let centerGlowRef: Graphics | null = null;
    let edgeParticles: { sprite: Graphics, sx: number, sy: number, ex: number, ey: number, speed: number, offset: number }[] = [];

    // Pre-calculate active edges for particles
    if (shouldRenderEffects) {
      layoutEdges.forEach((e) => {
        // Only spawn particles on edges directly connected to the active center/hover node
        const isEdgeActive = isHovering 
          ? (e.source.id === hoveredNodeId || e.target.id === hoveredNodeId)
          : (e.source.id === centerNodeId || e.target.id === centerNodeId);
          
        if (isEdgeActive) {
          const p = new Graphics();
          p.circle(0, 0, 1.5);
          p.fill({ color: 0xd4d4d8, alpha: 0.9 });
          particlesContainer.addChild(p);
          
          edgeParticles.push({
            sprite: p,
            sx: e.source.x, sy: e.source.y,
            ex: e.target.x, ey: e.target.y,
            speed: 0.3 + Math.random() * 0.4, // Traversals per second
            offset: Math.random() // Start position phase offset
          });
        }
      });
    }

    layoutNodes.forEach((n) => {
      const color = colorMap[n.type] ?? 0x52525b;
      const isCenter = n.id === centerNodeId;
      const isNodeActive = !isHovering || connectedIds.has(n.id);
      const isDirectHover = n.id === hoveredNodeId;

      // Glow sprite
      if (isNodeActive && shouldRenderEffects) {
        const glow = new Graphics();
        glow.circle(0, 0, isCenter ? 20 : (isDirectHover ? 18 : 14));
        glow.fill({ color, alpha: isCenter || isDirectHover ? 0.4 : 0.15 });
        glow.x = n.x;
        glow.y = n.y;
        glowContainer.addChild(glow);
        if (isCenter) centerGlowRef = glow;
      }

      const circle = new Graphics();
      circle.circle(0, 0, isCenter ? 14 : (isDirectHover ? 12 : 10));
      circle.fill({ color });
      circle.stroke({ width: isCenter ? 2.5 : 1.5, color: 0x18181b });
      circle.x = n.x;
      circle.y = n.y;
      circle.alpha = isNodeActive ? 1.0 : 0.2;
      
      if (isCenter) {
        centerCircleRef = circle;
      }
      
      circle.eventMode = 'static';
      circle.cursor = 'pointer';
      
      const nodeId = n.id;
      circle.on('pointerenter', () => setHoveredNodeId(nodeId));
      circle.on('pointerleave', () => setHoveredNodeId(null));
      circle.on('pointerdown', () => {
        const clicked = nodes.find((nd) => nd.id === nodeId);
        if (clicked) onNodeClick(clicked);
      });
      nodesContainer.addChild(circle);

      // Show labels for center node or if hovering over this specific node/cluster
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
        text.eventMode = 'none'; // Exclude label text from hit testing
        nodesContainer.addChild(text);
      }
    });

    // Only run ticker loop for breathing animation and particles if effects are enabled and motion isn't reduced
    if (shouldRenderEffects && (centerCircleRef || edgeParticles.length > 0)) {
      tickerCb = () => {
        const time = performance.now() / 1000;
        
        // Breathing
        if (centerCircleRef && !centerCircleRef.destroyed) {
          const breathe = 1 + Math.sin(time * 3) * 0.06; // ~6% gentle breathing scale
          centerCircleRef.scale.set(breathe);
          if (centerGlowRef && !centerGlowRef.destroyed) {
            centerGlowRef.scale.set(breathe);
            centerGlowRef.alpha = 0.4 + Math.sin(time * 3) * 0.1;
          }
        }

        // Particle traversal
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

    app.stage.eventMode = 'static';
    app.stage.cursor = 'default';
    app.stage.on('pointerdown', (e) => {
      if (e.target === app.stage) {
        onBackgroundClick();
      }
    });

    const endRender = performance.now();
    const renderTime = Math.round(endRender - startRender);
    
    if (isHoverPass) {
      lastMetrics.current.hover = renderTime;
    } else {
      lastMetrics.current.render = renderTime;
    }
    
    if (onDiagnostics) {
      onDiagnostics({ ...lastMetrics.current });
    }

    return () => {
      if (tickerCb && app && app.ticker) {
        try {
          app.ticker.remove(tickerCb);
        } catch (e) {
          // Ignore if already destroyed
        }
      }
    };
  }, [layoutData, hoveredNodeId, centerNodeId, onNodeClick, onBackgroundClick, contextLost, nodes, onDiagnostics, enableEffects, prefersReducedMotion]);

  if (contextLost) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-zinc-500 bg-zinc-950 p-6 text-center">
        <div className="h-12 w-12 rounded-full border border-red-500/30 flex items-center justify-center mb-3 text-red-400 bg-red-500/10">
          !
        </div>
        <p className="text-sm font-medium text-zinc-300">Graphics Context Lost</p>
        <p className="text-xs text-zinc-500 mt-1 max-w-sm">
          Your browser has discarded the WebGL context, likely due to high memory pressure or an idle GPU. 
          Please refresh the page to restore the graph.
        </p>
      </div>
    );
  }

  if (nodes.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-zinc-500 bg-zinc-950">
        <div className="h-12 w-12 rounded-full border border-zinc-800 flex items-center justify-center mb-3">
          <div className="h-6 w-6 rounded-full bg-zinc-800" />
        </div>
        <p className="text-sm">Select a memory to explore its graph</p>
        <p className="text-xs text-zinc-600 mt-1">Click any stream item to begin</p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="w-full h-full relative bg-zinc-950 overflow-hidden" />
  );
}
