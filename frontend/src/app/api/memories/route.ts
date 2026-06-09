import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();
  const type = searchParams.get("type");

  if (type === "pages") {
    const { data, error } = await supabase
      .from("canonical_pages")
      .select(
        "id,title,project_id,source_count,last_synth_at,updated_at,is_sparse,category",
      )
      .limit(100000)
      .order("updated_at", { ascending: false });
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json(data || [], {
      headers: {
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
      },
    });
  }

  if (type === "page") {
    const id = searchParams.get("id");
    if (!id) {
      return NextResponse.json({ error: "id required" }, { status: 400 });
    }
    const { data, error } = await supabase
      .from("canonical_pages")
      .select(
        "id,title,content,project_id,source_count,last_synth_at,updated_at,is_sparse,category",
      )
      .eq("id", id)
      .single();
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json(data, {
      headers: {
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
      },
    });
  }

  if (type === "nodes") {
    const pageId = searchParams.get("pageId");
    let query = supabase
      .from("graph_nodes")
      .select("id,label,type,canonical_page_id")
      .limit(100000)
      .order("type", { ascending: true });
    if (pageId) {
      query = query.eq("canonical_page_id", Number(pageId));
    }
    const { data, error } = await query;
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json(data || [], {
      headers: {
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
      },
    });
  }

  if (type === "edges") {
    const pageId = searchParams.get("pageId");
    if (pageId) {
      const { data: nodes, error: nodeError } = await supabase
        .from("graph_nodes")
        .select("id")
        .eq("canonical_page_id", Number(pageId))
        .limit(100000);
      if (nodeError || !nodes || nodes.length === 0) {
        return NextResponse.json([]);
      }
      const nodeIds = nodes.map((n) => n.id);
      const { data, error } = await supabase
        .from("graph_edges")
        .select("id,source_node_id,target_node_id,relationship")
        .or(
          `source_node_id.in.(${nodeIds.join(",")}),target_node_id.in.(${nodeIds.join(",")})`,
        )
        .limit(100000);
      if (error) {
        return NextResponse.json([], {
          status: 200,
          headers: {
            "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
          },
        });
      }
      return NextResponse.json(data || [], {
        headers: {
          "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        },
      });
    }

    const { data, error } = await supabase
      .from("graph_edges")
      .select("id,source_node_id,target_node_id,relationship")
      .limit(100000);
    if (error) {
      return NextResponse.json([], {
        status: 200,
        headers: {
          "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        },
      });
    }
    return NextResponse.json(data || [], {
      headers: {
        "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
      },
    });
  }

  return NextResponse.json({ error: "invalid type" }, { status: 400 });
}
