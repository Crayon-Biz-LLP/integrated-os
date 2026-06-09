import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

async function fetchAllPaginated(
  supabase: any,
  table: string,
  select: string,
  buildQuery?: (q: any) => any,
) {
  let allData: any[] = [];
  let from = 0;
  const step = 1000;
  let keepGoing = true;
  while (keepGoing) {
    let q = supabase
      .from(table)
      .select(select)
      .range(from, from + step - 1);
    if (buildQuery) q = buildQuery(q);
    const { data, error } = await q;
    if (error) throw error;
    if (data && data.length > 0) {
      allData = allData.concat(data);
      if (data.length < step) keepGoing = false;
      else from += step;
    } else {
      keepGoing = false;
    }
  }
  return allData;
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();
  const type = searchParams.get("type");

  if (type === "pages") {
    try {
      const data = await fetchAllPaginated(
        supabase,
        "canonical_pages",
        "id,title,project_id,source_count,last_synth_at,updated_at,is_sparse,category",
        (q) => q.order("updated_at", { ascending: false }),
      );

      return NextResponse.json(data || [], {
        headers: {
          "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        },
      });
    } catch (error: any) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
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
    try {
      const data = await fetchAllPaginated(
        supabase,
        "graph_nodes",
        "id,label,type,canonical_page_id",
        (q) => {
          let q2 = q.order("type", { ascending: true });
          if (pageId) q2 = q2.eq("canonical_page_id", Number(pageId));
          return q2;
        },
      );
      return NextResponse.json(data || [], {
        headers: {
          "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        },
      });
    } catch (error: any) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
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
      try {
        const data = await fetchAllPaginated(
          supabase,
          "graph_edges",
          "id,source_node_id,target_node_id,relationship",
          (q) =>
            q.or(
              `source_node_id.in.(${nodeIds.join(",")}),target_node_id.in.(${nodeIds.join(",")})`,
            ),
        );
        return NextResponse.json(data || [], {
          headers: {
            "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
          },
        });
      } catch (error: any) {
        return NextResponse.json([], {
          status: 200,
          headers: {
            "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
          },
        });
      }
    }

    try {
      const data = await fetchAllPaginated(
        supabase,
        "graph_edges",
        "id,source_node_id,target_node_id,relationship",
      );
      return NextResponse.json(data || [], {
        headers: {
          "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        },
      });
    } catch (error: any) {
      return NextResponse.json([], {
        status: 200,
        headers: {
          "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
        },
      });
    }
  }

  return NextResponse.json({ error: "invalid type" }, { status: 400 });
}
