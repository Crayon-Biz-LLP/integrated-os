import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const memoryId = searchParams.get("memory_id");

  if (!memoryId) {
    return NextResponse.json({ error: "memory_id required" }, { status: 400 });
  }

  const supabase = await createServerSupabaseClient();

  // Find memory node
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
    return NextResponse.json({ entity_node_id: null }, { status: 404 });
  }

  // Find primary MENTIONS edge — rank by weight desc for strongest entity association
  const { data: mentionsEdges } = await supabase
    .from("graph_edges")
    .select("target_node_id")
    .eq("source_node_id", memoryNodeId)
    .eq("relationship", "MENTIONS")
    .order("weight", { ascending: false })
    .order("created_at", { ascending: false })
    .limit(1);

  if (mentionsEdges && mentionsEdges.length > 0) {
    return NextResponse.json({ entity_node_id: mentionsEdges[0].target_node_id });
  }

  return NextResponse.json({ entity_node_id: null }, { status: 404 });
}
