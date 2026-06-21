import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

async function resolveNodeId(
  supabase: any,
  nodeId: string | null,
  pageId: string | null,
): Promise<string | null> {
  if (nodeId) return nodeId;
  if (!pageId) return null;

  const { data: pageNodes } = await supabase
    .from("graph_nodes")
    .select("id")
    .eq("canonical_page_id", Number(pageId))
    .limit(1);

  if (pageNodes && pageNodes.length > 0) {
    return String(pageNodes[0].id);
  }

  return null;
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const nodeId = await resolveNodeId(
    supabase,
    searchParams.get("node_id"),
    searchParams.get("page_id"),
  );

  if (!nodeId) {
    if (searchParams.get("page_id")) {
      return NextResponse.json({ error: "No nodes linked to this page" }, { status: 404 });
    }
    return NextResponse.json({ error: "node_id or page_id required" }, { status: 400 });
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
