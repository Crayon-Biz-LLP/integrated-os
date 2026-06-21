import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const nodeId = searchParams.get("node_id");
  const memoryId = searchParams.get("memory_id");

  if (!nodeId && !memoryId) {
    return NextResponse.json({ error: "node_id or memory_id required" }, { status: 400 });
  }

  // If memory_id is provided, resolve to the primary entity node
  let effectiveNodeId = nodeId;
  if (memoryId && !nodeId) {
    // Find the memory node for this memory
    const { data: memNodes } = await supabase
      .from("graph_nodes")
      .select("id")
      .eq("type", "memory")
      .eq("label", `Memory_${memoryId}`)
      .limit(1);

    let memoryNodeId: string | null = null;
    if (memNodes && memNodes.length > 0) {
      memoryNodeId = memNodes[0].id;
    } else {
      const { data: metaNodes } = await supabase
        .from("graph_nodes")
        .select("id, metadata")
        .eq("type", "memory")
        .limit(100);
      const found = metaNodes?.find((n: any) => n.metadata?.memory_id == memoryId);
      if (found) memoryNodeId = found.id;
    }

    if (!memoryNodeId) {
      return NextResponse.json({ error: "No nodes linked to this memory" }, { status: 404 });
    }

    // Find the best MENTIONS edge by weight desc — picks the strongest entity link
    const { data: mentionsEdges } = await supabase
      .from("graph_edges")
      .select("target_node_id")
      .eq("source_node_id", memoryNodeId)
      .eq("relationship", "MENTIONS")
      .order("weight", { ascending: false })
      .order("created_at", { ascending: false })
      .limit(1);

    if (mentionsEdges && mentionsEdges.length > 0) {
      effectiveNodeId = mentionsEdges[0].target_node_id;
    } else {
      effectiveNodeId = memoryNodeId;
    }
  }

  const { data: centerNode } = await supabase
    .from("graph_nodes")
    .select("id,label,type,canonical_page_id")
    .eq("id", effectiveNodeId)
    .single();

  if (!centerNode) {
    return NextResponse.json({ error: "Node not found" }, { status: 404 });
  }

  const { data: edges } = await supabase
    .from("graph_edges")
    .select("id,source_node_id,target_node_id,relationship")
    .or(`source_node_id.eq.${effectiveNodeId},target_node_id.eq.${effectiveNodeId}`)
    .order("created_at", { ascending: false })
    .limit(200);

  const neighborIds = new Set<string>();
  (edges || []).forEach((e) => {
    if (e.source_node_id !== effectiveNodeId) neighborIds.add(e.source_node_id);
    if (e.target_node_id !== effectiveNodeId) neighborIds.add(e.target_node_id);
  });

  let internalEdges: any[] = [];
  if (neighborIds.size > 0) {
    const ids = Array.from(neighborIds);
    const { data: inner } = await supabase
      .from("graph_edges")
      .select("id,source_node_id,target_node_id,relationship")
      .in("source_node_id", ids)
      .in("target_node_id", ids)
      .order("created_at", { ascending: false })
      .limit(500);
    internalEdges = inner || [];
  }

  let neighborNodes: any[] = [];
  if (neighborIds.size > 0) {
    const { data: nodes } = await supabase
      .from("graph_nodes")
      .select("id,label,type,canonical_page_id")
      .in("id", Array.from(neighborIds))
      .order("reference_count", { ascending: false })
      .order("created_at", { ascending: false })
      .limit(200);
    neighborNodes = nodes || [];
  }

  const allEdges = [...(edges || []), ...internalEdges];

  const seen = new Set<string>();
  const dedupedEdges = allEdges.filter((e) => {
    const [a, b] = [e.source_node_id, e.target_node_id].sort();
    const key = `${a}-${b}-${e.relationship}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  return NextResponse.json({
    center: centerNode,
    nodes: [centerNode, ...neighborNodes],
    edges: dedupedEdges,
  });
}
