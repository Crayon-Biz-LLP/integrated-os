import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

function unionFind(n: number) {
  const parent = Array.from({ length: n }, (_, i) => i);
  function find(x: number): number {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]];
      x = parent[x];
    }
    return x;
  }
  function union(a: number, b: number) {
    const ra = find(a), rb = find(b);
    if (ra !== rb) parent[rb] = ra;
  }
  return { find, union, parent };
}

function minutesDiff(a: string, b: string): number {
  return Math.abs(new Date(a).getTime() - new Date(b).getTime()) / 60000;
}

function makeTitle(memories: any[], entities: any[]): string {
  // If a single entity appears in >50% of memories, title by entity
  if (entities.length === 1) {
    return `About ${entities[0].label}`;
  }
  const entityCounts = new Map<string, number>();
  for (const m of memories) {
    for (const eid of (m.entity_ids || [])) {
      entityCounts.set(eid, (entityCounts.get(eid) || 0) + 1);
    }
  }
  const [topEid, topCount] = [...entityCounts.entries()].sort((a, b) => b[1] - a[1])[0] || [];
  if (topEid && topCount > memories.length * 0.5) {
    const topEntity = entities.find((e: any) => e.id === topEid);
    if (topEntity) return `About ${topEntity.label}`;
  }

  const types = new Set(memories.map((m: any) => m.memory_type).filter(Boolean));
  if (types.size === 1) {
    const t = [...types][0].replace(/_/g, " ");
    return t.charAt(0).toUpperCase() + t.slice(1);
  }

  return "Recent notes";
}

function makeSummary(memories: any[]): string {
  for (const m of memories) {
    const cleaned = (m.content || "")
      .replace(/\[.*?\]/g, "")
      .replace(/\*\*(.*?)\*\*/g, "$1")
      .replace(/\s+/g, " ")
      .trim();
    if (cleaned.length > 30) {
      return cleaned.length > 160 ? cleaned.slice(0, 160) + "..." : cleaned;
    }
  }
  return "";
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const nodeId = searchParams.get("node_id");
  const limit = Math.min(Number(searchParams.get("limit")) || 40, 80);

  const supabase = await createServerSupabaseClient();

  // Step 1: Fetch recent graph-linked memories
  let memoryIds: number[] = [];

  if (nodeId) {
    // Filtered by entity node: find memories that MENTIONS this node
    const { data: edges } = await supabase
      .from("graph_edges")
      .select("source_node_id")
      .eq("target_node_id", nodeId)
      .eq("relationship", "MENTIONS")
      .order("created_at", { ascending: false })
      .limit(limit * 2);

    if (edges && edges.length > 0) {
      const memNodeIds = edges.map((e: any) => e.source_node_id);
      const { data: memNodes } = await supabase
        .from("graph_nodes")
        .select("metadata")
        .in("id", memNodeIds)
        .eq("type", "memory");

      memoryIds = (memNodes || [])
        .map((n: any) => n.metadata?.memory_id)
        .filter(Boolean)
        .map(Number);
    }
  }

  // If no node filter or no results, get recent graph-linked memories
  if (memoryIds.length === 0 && !nodeId) {
    const { data: recentMemories } = await supabase
      .from("memories")
      .select("id,content,memory_type,created_at,metadata")
      .order("created_at", { ascending: false })
      .limit(limit * 3);

    if (recentMemories && recentMemories.length > 0) {
      const labels = recentMemories.map((m: any) => `Memory_${m.id}`);
      const { data: linkedNodes } = await supabase
        .from("graph_nodes")
        .select("metadata")
        .in("label", labels)
        .eq("type", "memory");

      const linkedIds = new Set(
        (linkedNodes || []).map((n: any) => n.metadata?.memory_id).filter(Boolean).map(Number)
      );
      memoryIds = recentMemories.filter((m: any) => linkedIds.has(m.id)).slice(0, limit).map((m: any) => m.id);
    }
  }

  if (memoryIds.length === 0) {
    return NextResponse.json({ episodes: [] });
  }

  // Step 2: Fetch the memory records
  const { data: memories } = await supabase
    .from("memories")
    .select("id,content,memory_type,created_at,metadata")
    .in("id", memoryIds)
    .order("created_at", { ascending: false });

  if (!memories || memories.length === 0) {
    return NextResponse.json({ episodes: [] });
  }

  // Step 3: Find memory nodes and their MENTIONS edges
  const memLabels = memories.map((m: any) => `Memory_${m.id}`);
  const { data: memNodeData } = await supabase
    .from("graph_nodes")
    .select("id, label, metadata")
    .in("label", memLabels)
    .eq("type", "memory");

  const memNodeMap = new Map<string, string>();
  for (const node of memNodeData || []) {
    const mid = node.metadata?.memory_id;
    if (mid) memNodeMap.set(String(mid), node.id);
  }

  const memoryNodeIds = [...memNodeMap.values()];
  const { data: mentionsEdges } = await supabase
    .from("graph_edges")
    .select("source_node_id, target_node_id")
    .in("source_node_id", memoryNodeIds)
    .eq("relationship", "MENTIONS");

  // Build memory → entity IDs map
  const memToEntities = new Map<number, string[]>();
  for (const edge of mentionsEdges || []) {
    // Find which memory node this edge came from
    for (const [memId, nodeId] of memNodeMap) {
      if (nodeId === edge.source_node_id) {
        const existing = memToEntities.get(Number(memId)) || [];
        existing.push(edge.target_node_id);
        memToEntities.set(Number(memId), existing);
        break;
      }
    }
  }

  // Step 4: Resolve entity labels
  const allEntityIds = new Set<string>();
  for (const ids of memToEntities.values()) ids.forEach((id) => allEntityIds.add(id));

  const entityMap = new Map<string, { id: string; label: string; type: string }>();
  if (allEntityIds.size > 0) {
    const batches: string[][] = [];
    const arr = Array.from(allEntityIds);
    for (let i = 0; i < arr.length; i += 200) batches.push(arr.slice(i, i + 200));

    const results = await Promise.all(
      batches.map((batch) =>
        supabase.from("graph_nodes").select("id,label,type").in("id", batch)
      )
    );
    for (const r of results) {
      for (const n of r.data || []) {
        entityMap.set(n.id, { id: n.id, label: n.label, type: n.type });
      }
    }
  }

  // Step 5: Cluster memories
  type MemoWithEntities = any;
  const enriched: MemoWithEntities[] = memories.map((m: any) => ({
    ...m,
    entity_ids: memToEntities.get(m.id) || [],
    source: m.metadata?.source || null,
  }));

  if (enriched.length === 0) {
    return NextResponse.json({ episodes: [] });
  }

  const uf = unionFind(enriched.length);
  for (let i = 0; i < enriched.length; i++) {
    for (let j = i + 1; j < enriched.length; j++) {
      const a = enriched[i], b = enriched[j];
      const sharedEntity = a.entity_ids.some((eid: string) => b.entity_ids.includes(eid));
      const sameSource = a.source && b.source && a.source === b.source;
      const timeDiff = minutesDiff(a.created_at, b.created_at);
      const sameType = a.memory_type && b.memory_type && a.memory_type === b.memory_type;

      if (sharedEntity && timeDiff < 120) { uf.union(i, j); }
      else if (sameSource && timeDiff < 60) { uf.union(i, j); }
      else if (sameType && timeDiff < 30) { uf.union(i, j); }
    }
  }

  // Build clusters from union-find
  const clusters = new Map<number, MemoWithEntities[]>();
  for (let i = 0; i < enriched.length; i++) {
    const root = uf.find(i);
    if (!clusters.has(root)) clusters.set(root, []);
    clusters.get(root)!.push(enriched[i]);
  }

  // Step 6: Build episodes from clusters
  const episodes = Array.from(clusters.values()).map((cluster) => {
    const sorted = cluster.sort(
      (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
    );
    const entityIdSet = new Set<string>();
    for (const m of sorted) m.entity_ids.forEach((eid: string) => entityIdSet.add(eid));
    const entities = Array.from(entityIdSet)
      .map((id) => entityMap.get(id))
      .filter(Boolean);

    return {
      id: `ep_${sorted[0].id}_${sorted.length}`,
      title: makeTitle(sorted, entities),
      summary: makeSummary(sorted),
      memory_type: sorted[0].memory_type,
      entities,
      timestamp: sorted[0].created_at,
      count: sorted.length,
      graph_node_ids: Array.from(entityIdSet),
      memory_ids: sorted.map((m) => m.id),
      memories: sorted.map((m) => ({
        id: m.id,
        content: m.content,
        memory_type: m.memory_type,
        created_at: m.created_at,
      })),
    };
  });

  // Sort episodes by newest first
  episodes.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

  return NextResponse.json({ episodes });
}
