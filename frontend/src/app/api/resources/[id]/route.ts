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
      .eq("id", Number(id))
      .single();

    if (error) {
      console.error("Supabase error fetching resource:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const clusterData = Array.isArray(data.clusters) ? data.clusters[0] : data.clusters;

    const resource = {
      id: data.id,
      url: data.url,
      title: data.title,
      summary: data.summary,
      strategic_note: data.strategic_note,
      category: data.category,
      cluster_id: data.cluster_id,
      created_at: data.created_at,
      enriched_at: data.enriched_at,
      hostname: getHostname(data.url),
      cluster_title: clusterData?.title ?? null,
      cluster_status: clusterData?.status ?? null,
      cluster_description: clusterData?.description ?? null,
    };

    return NextResponse.json(resource);
  } catch (err: any) {
    console.error("Unexpected error in resource [id] route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
