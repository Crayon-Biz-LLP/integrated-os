'use client';

import * as d3 from 'd3';
import { useRef, useEffect } from 'react';
import { GraphNode, GraphEdge } from '@/lib/memories/types';

interface EgoGraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  width?: number;
  height?: number;
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

export default function EgoGraph({ nodes, edges, width = 320, height = 280 }: EgoGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    if (!svgRef.current || nodes.length === 0) return;

    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    const simNodes: SimNode[] = nodes.map((n) => ({
      ...n,
      x: width / 2,
      y: height / 2,
    }));

    const simEdges: SimEdge[] = edges
      .filter((e) => nodes.some((n) => n.id === e.source_node_id) && nodes.some((n) => n.id === e.target_node_id))
      .map((e) => ({
        ...e,
        source: simNodes.find((n) => n.id === e.source_node_id)!,
        target: simNodes.find((n) => n.id === e.target_node_id)!,
      }));

    const sim = d3
      .forceSimulation(simNodes)
      .force('link', d3.forceLink<SimNode, SimEdge>(simEdges).distance(60).strength(0.8))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide(20));

    sim.tick(300);
    sim.stop();

    const edgeGroup = svg.append('g');
    simEdges.forEach((edge) => {
      const s = edge.source;
      const t = edge.target;
      if (!s || !t) return;

      edgeGroup
        .append('line')
        .attr('x1', s.x!)
        .attr('y1', s.y!)
        .attr('x2', t.x!)
        .attr('y2', t.y!)
        .attr('stroke', '#3f3f46')
        .attr('stroke-width', 1.5);

      const midX = ((s.x! + t.x!) / 2);
      const midY = ((s.y! + t.y!) / 2);
      const label = edge.relationship.length > 12 ? edge.relationship.slice(0, 12) + '...' : edge.relationship;
      edgeGroup
        .append('text')
        .attr('x', midX)
        .attr('y', midY)
        .attr('font-size', 8)
        .attr('fill', '#71717a')
        .attr('text-anchor', 'middle')
        .text(label);
    });

    const nodeGroup = svg.append('g');
    simNodes.forEach((node) => {
      const fill = colorMap[node.type] || '#52525b';

      nodeGroup
        .append('circle')
        .attr('cx', node.x!)
        .attr('cy', node.y!)
        .attr('r', 10)
        .attr('fill', fill)
        .attr('stroke', '#18181b')
        .attr('stroke-width', 1.5);

      const label = node.label.length > 14 ? node.label.slice(0, 14) + '...' : node.label;
      nodeGroup
        .append('text')
        .attr('x', node.x!)
        .attr('y', node.y! + 20)
        .attr('font-size', 9)
        .attr('fill', '#d4d4d8')
        .attr('text-anchor', 'middle')
        .text(label);
    });
  }, [nodes, edges, width, height]);

  if (nodes.length === 0) return null;

  return (
    <svg
      ref={svgRef}
      width={width}
      height={height}
      style={{ overflow: 'hidden' }}
    />
  );
}
