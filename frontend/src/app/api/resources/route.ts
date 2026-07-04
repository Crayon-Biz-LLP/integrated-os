import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

function getHostname(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return null;
  }
}

export async function GET(req: NextRequest) {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
    const { searchParams } = new URL(req.url);
    const supabase = await createServerSupabaseClient();

    const search = searchParams.get("search");
    const cluster = searchParams.get("cluster");
    const category = searchParams.get("category");
    const sort = searchParams.get("sort") || "newest";

    let query = supabase
      .from("resources")
      .select(`
        id,
        url,
        title,
        summary,
        category,
        strategic_note,
        cluster_id,
        created_at,
        enriched_at,
        dismissed_at,
        clusters!cluster_id(id, title, status, description)
      `)
      .is('dismissed_at', null);

    if (search) {
      query = query.or(
        `title.ilike.%${search}%,summary.ilike.%${search}%,strategic_note.ilike.%${search}%,category.ilike.%${search}%`
      );
    }

    if (cluster === "unmapped") {
      query = query.is("cluster_id", null);
    } else if (cluster && cluster !== "all") {
      query = query.eq("cluster_id", Number(cluster));
    }

    if (category && category !== "all") {
      query = query.eq("category", category);
    }

    switch (sort) {
      case "oldest":
        query = query.order("created_at", { ascending: true });
        break;
      case "title":
        query = query.order("title", { ascending: true, nullsFirst: false });
        break;
      case "category":
        query = query.order("category", { ascending: true, nullsFirst: false });
        break;
      case "cluster":
        query = query.order("cluster_id", { ascending: true, nullsFirst: false });
        break;
      default:
        query = query.order("created_at", { ascending: false });
    }

    query = query.limit(100);
    const { data, error } = await query;

    if (error) {
      console.error("Supabase error fetching resources:", error);
      return NextResponse.json({ error: error.message, details: error }, { status: 500 });
    }

    const resources = (data ?? []).map((r: any) => {
      const clusterData = Array.isArray(r.clusters) ? r.clusters[0] : r.clusters;
      return {
        id: r.id,
        url: r.url,
        title: r.title,
        summary: r.summary,
        strategic_note: r.strategic_note,
        category: r.category,
        cluster_id: r.cluster_id,
        created_at: r.created_at,
        enriched_at: r.enriched_at,
        hostname: getHostname(r.url),
        cluster_title: clusterData?.title ?? null,
        cluster_status: clusterData?.status ?? null,
        cluster_description: clusterData?.description ?? null,
      };
    });

    return NextResponse.json(resources);
  } catch (err: any) {
    console.error("Unexpected error in resources route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
