import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

async function resolveNodeId(
  supabase: any,
  nodeId: string | null,
  memoryId: string | null,
): Promise<string | null> {
  if (nodeId) return nodeId;
  if (!memoryId) return null;

  // Find the memory node corresponding to this memory_id
  const { data: memNodes } = await supabase
    .from("graph_nodes")
    .select("id")
    .eq("type", "memory")
    .eq("label", `Memory_${memoryId}`)
    .limit(1);

  if (memNodes && memNodes.length > 0) {
    return String(memNodes[0].id);
  }
  
  // Fallback: check metadata if label convention changes
  const { data: metaNodes } = await supabase
    .from("graph_nodes")
    .select("id, metadata")
    .eq("type", "memory")
    .limit(100); // we can't easily query jsonb in this generic client without specific syntax
    
  const found = metaNodes?.find((n: any) => n.metadata?.memory_id == memoryId);
  if (found) {
    return String(found.id);
  }

  return null;
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const nodeId = await resolveNodeId(
    supabase,
    searchParams.get("node_id"),
    searchParams.get("memory_id"),
  );

  if (!nodeId) {
    if (searchParams.get("memory_id")) {
      return NextResponse.json({ error: "No nodes linked to this memory" }, { status: 404 });
    }
    return NextResponse.json({ error: "node_id or memory_id required" }, { status: 400 });
  }

  const { data: centerNode } = await supabase
    .from("graph_nodes")
    .select("id,label,type,canonical_page_id")
    .eq("id", Number(nodeId))
    .single();

  if (!centerNode) {
    return NextResponse.json({ error: "Node not found" }, { status: 404 });
  }

  const { data: edges } = await supabase
    .from("graph_edges")
    .select("id,source_node_id,target_node_id,relationship")
    .or(`source_node_id.eq.${nodeId},target_node_id.eq.${nodeId}`)
    .limit(200);

  const neighborIds = new Set<number>();
  (edges || []).forEach((e) => {
    if (e.source_node_id !== Number(nodeId)) neighborIds.add(e.source_node_id);
    if (e.target_node_id !== Number(nodeId)) neighborIds.add(e.target_node_id);
  });

  let internalEdges: any[] = [];
  if (neighborIds.size > 0) {
    const ids = Array.from(neighborIds);
    const { data: inner } = await supabase
      .from("graph_edges")
      .select("id,source_node_id,target_node_id,relationship")
      .in("source_node_id", ids)
      .in("target_node_id", ids)
      .limit(500);
    internalEdges = inner || [];
  }

  let neighborNodes: any[] = [];
  if (neighborIds.size > 0) {
    const { data: nodes } = await supabase
      .from("graph_nodes")
      .select("id,label,type,canonical_page_id")
      .in("id", Array.from(neighborIds))
      .limit(200);
    neighborNodes = nodes || [];
  }

  const allEdges = [...(edges || []), ...internalEdges];

  const seen = new Map<string, boolean>();
  const dedupedEdges = allEdges.filter((e) => {
    const key = `${Math.min(e.source_node_id, e.target_node_id)}-${Math.max(e.source_node_id, e.target_node_id)}-${e.relationship}`;
    if (seen.has(key)) return false;
    seen.set(key, true);
    return true;
  });

  return NextResponse.json({
    center: centerNode,
    nodes: [centerNode, ...neighborNodes],
    edges: dedupedEdges,
  });
}
