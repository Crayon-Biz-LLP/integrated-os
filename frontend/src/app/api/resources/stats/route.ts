import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  const supabase = await createServerSupabaseClient();

  const { data: allResources, error } = await supabase
    .from("resources")
    .select("id, mission_id, created_at");

  if (error) {
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

  return NextResponse.json({
    totalResources,
    activeMissionsWithResources,
    unmappedResources,
    recentResources,
  });
}
