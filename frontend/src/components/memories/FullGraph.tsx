'use client';

import * as d3 from 'd3';
import { useRef, useEffect, useCallback } from 'react';
import { GraphNode, GraphEdge } from '@/lib/memories/types';

interface FullGraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onNodeClick: (node: GraphNode) => void;
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

interface SimNode extends d3.SimulationNodeDatum {
  id: number;
  label: string;
  type: string;
  canonical_page_id: number | null;
}

interface SimEdge {
  id: number;
  source_node_id: number;
  target_node_id: number;
  relationship: string;
  source: SimNode;
  target: SimNode;
}

export default function FullGraph({ nodes, edges, onNodeClick }: FullGraphProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  const ticked = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const svg = d3.select(container).select('svg');
    const g = svg.select('g');
    g.selectAll('line')
      .attr('x1', (d: unknown) => (d as SimEdge).source.x!)
      .attr('y1', (d: unknown) => (d as SimEdge).source.y!)
      .attr('x2', (d: unknown) => (d as SimEdge).target.x!)
      .attr('y2', (d: unknown) => (d as SimEdge).target.y!);
    g.selectAll('circle')
      .attr('cx', (d: unknown) => (d as SimNode).x!)
      .attr('cy', (d: unknown) => (d as SimNode).y!);
    g.selectAll('text')
      .attr('x', (d: unknown) => (d as SimNode).x!)
      .attr('y', (d: unknown) => (d as SimNode).y! + 22);
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || nodes.length === 0) return;

    const { width, height } = container.getBoundingClientRect();
    const svg = d3.select(container).selectAll('svg').data([null]).join('svg')
      .attr('width', width)
      .attr('height', height)
      .style('background', '#09090b')
      .style('cursor', 'grab');

    svg.selectAll('*').remove();

    const g = svg.append('g');

    const simNodes: SimNode[] = nodes.map((n) => ({ ...n }));

    const simEdges: SimEdge[] = edges
      .filter((e) => nodes.some((n) => n.id === e.source_node_id) && nodes.some((n) => n.id === e.target_node_id))
      .map((e) => ({
        ...e,
        source: simNodes.find((n) => n.id === e.source_node_id)!,
        target: simNodes.find((n) => n.id === e.target_node_id)!,
      }));

    const simulation = d3
      .forceSimulation(simNodes)
      .force('link', d3.forceLink<SimNode, SimEdge>(simEdges).id((d) => d.id).distance(90).strength(0.5))
      .force('charge', d3.forceManyBody().strength(-200))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide(22));

    let tickCount = 0;
    simulation.on('tick', () => {
      tickCount++;
      if (tickCount > 250) { simulation.stop(); }
      ticked();
    });

    const zoom = d3.zoom()
      .scaleExtent([0.2, 4])
      .on('zoom', (event) => {
        g.attr('transform', event.transform);
      });
    svg.call(zoom as any);

    const drag = d3.drag<SVGCircleElement, SimNode>()
      .on('start', (event, d) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on('drag', (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on('end', (event, d) => {
        if (!event.active) simulation.alphaTarget(0);
        d.fx = null;
        d.fy = null;
      });

    const edgeGroup = g.append('g');
    edgeGroup.selectAll('line')
      .data(simEdges)
      .join('line')
      .attr('stroke', '#3f3f46')
      .attr('stroke-width', 1.2)
      .attr('opacity', 0.6)
      .on('mouseenter', function () {
        d3.select(this).attr('opacity', 1);
      })
      .on('mouseleave', function () {
        d3.select(this).attr('opacity', 0.6);
      })
      .append('title')
      .text((d) => d.relationship);

    const nodeGroup = g.append('g');
    nodeGroup.selectAll('circle')
      .data(simNodes)
      .join('circle')
      .attr('r', 10)
      .attr('fill', (d) => colorMap[d.type] || '#52525b')
      .attr('stroke', '#18181b')
      .attr('stroke-width', 1.5)
      .on('mouseenter', function (_, d) {
        d3.select(this).attr('r', 13).attr('filter', 'drop-shadow(0 0 4px rgba(0,0,0,0.5))');
      })
      .on('mouseleave', function (_, d) {
        d3.select(this).attr('r', 10).attr('filter', null);
      })
      .on('click', (event, d) => {
        event.stopPropagation();
        onNodeClick(d);
      })
      .call(drag as any);

    nodeGroup.selectAll('text')
      .data(simNodes)
      .join('text')
      .attr('font-size', 9)
      .attr('fill', '#d4d4d8')
      .attr('text-anchor', 'middle')
      .text((d) => d.label.length > 16 ? d.label.slice(0, 16) + '...' : d.label);

    svg.on('click', () => {
      onNodeClick(null as any);
    });

    return () => {
      simulation.stop();
    };
  }, [nodes, edges, onNodeClick, ticked]);

  return (
    <div ref={containerRef} className="w-full h-full" />
  );
}
