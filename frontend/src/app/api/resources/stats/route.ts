import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  try {
    const supabase = await createServerSupabaseClient();

    console.log("Stats API - Fetching resource stats");

    const { data: allResources, error } = await supabase
      .from("resources")
      .select("id, mission_id, created_at");

    if (error) {
      console.error("Supabase error fetching resources stats:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const now = new Date();
    const thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);

    const totalResources = (allResources ?? []).length;

    const resourcesWithMissions = (allResources ?? []).filter(r => r.mission_id !== null);
    const activeMissionsWithResources = new Set(resourcesWithMissions.map(r => r.mission_id)).size;

    const unmappedResources = (allResources ?? []).filter(r => r.mission_id === null).length;

    const recentResources = (allResources ?? []).filter(r => {
      if (!r.created_at) return false;
      return new Date(r.created_at) >= thirtyDaysAgo;
    }).length;

    const stats = {
      totalResources,
      activeMissionsWithResources,
      unmappedResources,
      recentResources,
    };

    console.log("Stats API - Returning stats:", stats);

    return NextResponse.json(stats);
  } catch (err: any) {
    console.error("Unexpected error in stats route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
