import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const nodeId = searchParams.get("node_id");
  const limit = Math.min(Number(searchParams.get("limit")) || 20, 50);

  const supabase = await createServerSupabaseClient();

  if (nodeId) {
    const { data: node } = await supabase
      .from("graph_nodes")
      .select("canonical_page_id")
      .eq("id", Number(nodeId))
      .maybeSingle();

    if (node?.canonical_page_id) {
      const { data: page } = await supabase
        .from("canonical_pages")
        .select("id,title,content,updated_at,category")
        .eq("id", node.canonical_page_id)
        .maybeSingle();

      const { data: recentPages } = await supabase
        .from("canonical_pages")
        .select("id,title,content,updated_at,category")
        .order("updated_at", { ascending: false })
        .limit(limit);

      let items = recentPages || [];
      if (page && !items.some((p: any) => p.id === page.id)) {
        items = [page, ...items].slice(0, limit);
      }

      return NextResponse.json({ items });
    }

    const { data: nodesWithPage } = await supabase
      .from("graph_nodes")
      .select("canonical_page_id")
      .eq("id", Number(nodeId))
      .not("canonical_page_id", "is", null)
      .limit(50);

    if (nodesWithPage && nodesWithPage.length > 0) {
      const pageIds = nodesWithPage
        .map((n: any) => n.canonical_page_id)
        .filter(Boolean);
      const { data: pages } = await supabase
        .from("canonical_pages")
        .select("id,title,content,updated_at,category")
        .in("id", pageIds)
        .order("updated_at", { ascending: false })
        .limit(limit);

      return NextResponse.json({ items: pages || [] });
    }
  }

  const { data: pages } = await supabase
    .from("canonical_pages")
    .select("id,title,content,updated_at,category")
    .order("updated_at", { ascending: false })
    .limit(limit);

  return NextResponse.json({ items: pages || [] });
}
