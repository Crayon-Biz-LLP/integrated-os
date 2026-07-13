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

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
    const { id } = await params;
    const supabase = await createServerSupabaseClient();

    const { data: resource, error: resourceError } = await supabase
      .from("resources")
      .select("cluster_id")
      .eq("id", Number(id))
      .single();

    if (resourceError || !resource?.cluster_id) {
      return NextResponse.json([]);
    }

    const { data, error } = await supabase
      .from("resources")
      .select(`
        id,
        url,
        title,
        summary,
        strategic_note,
        category,
        cluster_id,
        created_at,
        enriched_at,
        clusters!cluster_id(id, title, status, description)
      `)
      .eq("is_current", true)
      .eq("cluster_id", resource.cluster_id)
      .neq("id", Number(id))
      .limit(5);

    if (error) {
      console.error("Supabase error fetching related resources:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const related = (data ?? []).map((r: any) => {
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

    return NextResponse.json(related);
  } catch (err: any) {
    console.error("Unexpected error in related resources route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
