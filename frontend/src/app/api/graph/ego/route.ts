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

  // Fetch 1-hop edges from Danny, deterministically ordered
  const { data: hop1Edges } = await supabase
    .from("graph_edges")
    .select("id,source_node_id,target_node_id,relationship,weight")
    .or(`source_node_id.eq.${dannyId},target_node_id.eq.${dannyId}`)
    .order("weight", { ascending: false })
    .order("created_at", { ascending: false })
    .limit(200);

  const neighborIds = new Set<string>();
  (hop1Edges || []).forEach((e) => {
    if (e.source_node_id !== dannyId) neighborIds.add(e.source_node_id);
    if (e.target_node_id !== dannyId) neighborIds.add(e.target_node_id);
  });

  let allEdges = [...(hop1Edges || [])];
  let hop2NeighborIds = new Set<string>();

  // Fetch 2-hop if depth > 1
  if (depth > 1 && neighborIds.size > 0) {
    const hop1Ids = Array.from(neighborIds);

    const { data: hop2Edges } = await supabase
      .from("graph_edges")
      .select("id,source_node_id,target_node_id,relationship,weight")
      .in("source_node_id", hop1Ids)
      .in("target_node_id", hop1Ids)
      .order("weight", { ascending: false })
      .order("created_at", { ascending: false })
      .limit(500);

    (hop2Edges || []).forEach((e) => {
      allEdges.push(e);
      if (!neighborIds.has(e.source_node_id) && e.source_node_id !== dannyId) {
        hop2NeighborIds.add(e.source_node_id);
      }
      if (!neighborIds.has(e.target_node_id) && e.target_node_id !== dannyId) {
        hop2NeighborIds.add(e.target_node_id);
      }
    });

    // Bridge edges back to Danny/1-hop
    if (hop2NeighborIds.size > 0) {
      const hop2Ids = Array.from(hop2NeighborIds);

      const { data: bridgingEdges } = await supabase
        .from("graph_edges")
        .select("id,source_node_id,target_node_id,relationship,weight")
        .in("source_node_id", hop2Ids)
        .in("target_node_id", [dannyId, ...hop1Ids])
        .order("weight", { ascending: false })
        .order("created_at", { ascending: false })
        .limit(200);

      (bridgingEdges || []).forEach((e) => allEdges.push(e));

      const { data: bridgingEdgesReverse } = await supabase
        .from("graph_edges")
        .select("id,source_node_id,target_node_id,relationship,weight")
        .in("source_node_id", [dannyId, ...hop1Ids])
        .in("target_node_id", hop2Ids)
        .order("weight", { ascending: false })
        .order("created_at", { ascending: false })
        .limit(200);

      (bridgingEdgesReverse || []).forEach((e) => allEdges.push(e));
    }
  }

  // Collect all unique node IDs
  const allNodeIds = new Set<string>([dannyId]);
  neighborIds.forEach((id) => allNodeIds.add(id));
  hop2NeighborIds.forEach((id) => allNodeIds.add(id));

  // Fetch nodes with deterministic ordering before capping
  const { data: allNodes } = await supabase
    .from("graph_nodes")
    .select("id,label,type,canonical_page_id")
    .in("id", Array.from(allNodeIds))
    .order("reference_count", { ascending: false })
    .order("created_at", { ascending: false })
    .limit(cap);

  const cappedNodeIds = new Set((allNodes || []).map((n: any) => n.id));

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
