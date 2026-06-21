import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

async function resolveRootNodeId(supabase: any): Promise<{ id: string; label: string; type: string; canonical_page_id: number | null } | null> {
  // 1. Stable lookup: core_config key 'root_entity_id'
  const { data: config } = await supabase
    .from("core_config")
    .select("content")
    .eq("key", "root_entity_id")
    .maybeSingle();

  if (config?.content) {
    const rootId = config.content.trim();
    const { data: node } = await supabase
      .from("graph_nodes")
      .select("id,label,type,canonical_page_id")
      .eq("id", rootId)
      .maybeSingle();
    if (node) return node;
  }

  // 2. Fallback: stable label + type match
  const { data: node } = await supabase
    .from("graph_nodes")
    .select("id,label,type,canonical_page_id")
    .ilike("label", "Danny")
    .eq("type", "person")
    .maybeSingle();

  return node || null;
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const depth = Math.min(Number(searchParams.get("depth")) || 2, 3);
  const cap = Math.min(Number(searchParams.get("cap")) || 80, 200);

  const supabase = await createServerSupabaseClient();

  const dannyNode = await resolveRootNodeId(supabase);
  if (!dannyNode) {
    return NextResponse.json({ error: "Root node not found" }, { status: 404 });
  }

  const dannyId = dannyNode.id;

  // Fetch ALL 1-hop edges from Danny — no limit, we need the full neighbourhood
  const { data: hop1Edges } = await supabase
    .from("graph_edges")
    .select("id,source_node_id,target_node_id,relationship,weight")
    .or(`source_node_id.eq.${dannyId},target_node_id.eq.${dannyId}`);

  const neighborIds = new Set<string>();
  (hop1Edges || []).forEach((e) => {
    if (e.source_node_id !== dannyId) neighborIds.add(e.source_node_id);
    if (e.target_node_id !== dannyId) neighborIds.add(e.target_node_id);
  });

  let allEdges = [...(hop1Edges || [])];

  // Build the full node set (1-hop only by default, 2-hop if depth > 1)
  const allNodeIds = new Set<string>([dannyId]);
  neighborIds.forEach((id) => allNodeIds.add(id));

  if (depth > 1 && neighborIds.size > 0) {
    // Fetch neighbor node types to identify entity nodes (non-memory)
    const { data: neighborNodes } = await supabase
      .from("graph_nodes")
      .select("id,type")
      .in("id", Array.from(neighborIds));

    const entityNodeIds = (neighborNodes || [])
      .filter((n: any) => n.type !== "memory")
      .map((n: any) => n.id);

    // 2-hop: query edges for each entity neighbor individually but sequentially bounded
    let hop2NeighborIds = new Set<string>();
    let hop2Edges: any[] = [];

    for (const entityId of entityNodeIds.slice(0, 30)) {
      const { data: eEdges } = await supabase
        .from("graph_edges")
        .select("id,source_node_id,target_node_id,relationship,weight")
        .or(`source_node_id.eq.${entityId},target_node_id.eq.${entityId}`)
        .limit(20);

      if (eEdges) {
        for (const e of eEdges) {
          if (allNodeIds.has(e.source_node_id) && allNodeIds.has(e.target_node_id)) continue;
          if (e.source_node_id !== dannyId && !neighborIds.has(e.source_node_id)) {
            hop2NeighborIds.add(e.source_node_id);
          }
          if (e.target_node_id !== dannyId && !neighborIds.has(e.target_node_id)) {
            hop2NeighborIds.add(e.target_node_id);
          }
          hop2Edges.push(e);
        }
      }
    }

    // Merge 2-hop edges and node IDs
    hop2NeighborIds.forEach((id) => allNodeIds.add(id));
    allEdges.push(...hop2Edges);
  }

  // Fetch nodes — split into parallel batches to avoid URL length limits with 820+ UUIDs
  const allNodeIdArr = Array.from(allNodeIds);
  const batches: string[][] = [];
  for (let i = 0; i < allNodeIdArr.length; i += 200) {
    batches.push(allNodeIdArr.slice(i, i + 200));
  }

  const nodeResults = await Promise.all(
    batches.map((batch) =>
      supabase
        .from("graph_nodes")
        .select("id,label,type,canonical_page_id")
        .in("id", batch)
        .order("reference_count", { ascending: false })
        .order("created_at", { ascending: false })
    )
  );

  const allNodeMap = new Map<string, any>();
  for (const result of nodeResults) {
    for (const node of result.data || []) {
      if (!allNodeMap.has(node.id)) {
        allNodeMap.set(node.id, node);
      }
    }
  }

  // Sort by entity type priority first, then reference_count
  const typeOrder: Record<string, number> = {
    person: 1, organization: 2, project: 3, place: 4,
    cluster: 5, task: 6, emotional_state: 7, concept: 8, memory: 9,
  };

  const allNodes = Array.from(allNodeMap.values()).sort((a: any, b: any) => {
    const aPrio = typeOrder[a.type] ?? 99;
    const bPrio = typeOrder[b.type] ?? 99;
    if (aPrio !== bPrio) return aPrio - bPrio;
    return (b.reference_count ?? 0) - (a.reference_count ?? 0);
  }).slice(0, cap);

  const cappedNodeIds = new Set(allNodes.map((n: any) => n.id));

  // Filter edges to only include capped nodes
  const filteredEdges = allEdges.filter(
    (e) => cappedNodeIds.has(e.source_node_id) && cappedNodeIds.has(e.target_node_id),
  );

  // Dedup edges
  const seen = new Set<string>();
  const dedupedEdges = filteredEdges.filter((e) => {
    const [a, b] = [e.source_node_id, e.target_node_id].sort();
    const key = `${a}-${b}-${e.relationship}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  return NextResponse.json({
    center: dannyNode,
    nodes: allNodes || [],
    edges: dedupedEdges,
    danny_id: dannyId,
  });
}
