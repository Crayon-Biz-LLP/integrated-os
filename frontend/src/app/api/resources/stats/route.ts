import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
    const supabase = await createServerSupabaseClient();

    const { data: allResources, error } = await supabase
      .from("resources")
      .select("id, cluster_id, created_at")
      .limit(500);

    if (error) {
      console.error("Supabase error fetching resources stats:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const now = new Date();
    const thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);

    const totalResources = (allResources ?? []).length;

    const resourcesWithClusters = (allResources ?? []).filter(r => r.cluster_id !== null);
    const activeClustersWithResources = new Set(resourcesWithClusters.map(r => r.cluster_id)).size;

    const unmappedResources = (allResources ?? []).filter(r => r.cluster_id === null).length;

    const recentResources = (allResources ?? []).filter(r => {
      if (!r.created_at) return false;
      return new Date(r.created_at) >= thirtyDaysAgo;
    }).length;

    const stats = {
      totalResources,
      activeClustersWithResources,
      unmappedResources,
      recentResources,
    };

    return NextResponse.json(stats);
  } catch (err: any) {
    console.error("Unexpected error in stats route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
