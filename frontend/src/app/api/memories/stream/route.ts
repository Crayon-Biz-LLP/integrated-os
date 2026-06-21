import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const nodeId = searchParams.get("node_id");
  const limit = Math.min(Number(searchParams.get("limit")) || 20, 50);

  const supabase = await createServerSupabaseClient();

  // If filtered by node_id, find memories that mention this node
  if (nodeId) {
    // 1. Find all edges where target is the node_id and relationship is MENTIONS (or EVOKES from the node)
    // Actually, memory nodes have MENTIONS edges to entity nodes.
    const { data: edges } = await supabase
      .from("graph_edges")
      .select("source_node_id")
      .eq("target_node_id", nodeId)
      .eq("relationship", "MENTIONS")
      .order("created_at", { ascending: false })
      .limit(50);
      
    if (edges && edges.length > 0) {
      const memoryNodeIds = edges.map(e => e.source_node_id);
      // 2. Get the memory nodes to find their memory_ids
      const { data: memoryNodes } = await supabase
        .from("graph_nodes")
        .select("metadata")
        .in("id", memoryNodeIds)
        .eq("type", "memory");
        
      if (memoryNodes && memoryNodes.length > 0) {
        const memoryIds = memoryNodes
          .map(n => n.metadata?.memory_id)
          .filter(Boolean)
          .map(id => Number(id));
          
        if (memoryIds.length > 0) {
          const { data: memories } = await supabase
            .from("memories")
            .select("id,content,memory_type,created_at")
            .in("id", memoryIds)
            .order("created_at", { ascending: false })
            .limit(limit);
            
          return NextResponse.json({ items: memories || [] });
        }
      }
    }
  }

  // Default: return recent memories that have graph linkages
  // To do this efficiently, we can just fetch recent memories, then filter those that exist in graph_nodes.
  // Or we can just return recent memories, as the prompt says "that either already link to graph nodes, or can deterministically resolve".
  // For now, let's just fetch the 50 most recent memories, and filter down to those that have a corresponding memory node.
  
  const { data: recentMemories } = await supabase
    .from("memories")
    .select("id,content,memory_type,created_at")
    .order("created_at", { ascending: false })
    .limit(limit * 2); // Fetch extra to account for filtering
    
  if (!recentMemories || recentMemories.length === 0) {
    return NextResponse.json({ items: [] });
  }
  
  // Find which of these memories have a graph node
  const memoryLabels = recentMemories.map(m => `Memory_${m.id}`);
  const { data: linkedNodes } = await supabase
    .from("graph_nodes")
    .select("metadata")
    .in("label", memoryLabels)
    .eq("type", "memory");
    
  const linkedMemoryIds = new Set(
    (linkedNodes || [])
      .map(n => n.metadata?.memory_id)
      .filter(Boolean)
      .map(id => Number(id))
  );
  
  // Filter and limit
  const filteredMemories = recentMemories
    .filter(m => linkedMemoryIds.has(m.id))
    .slice(0, limit);

  return NextResponse.json({ items: filteredMemories });
}
