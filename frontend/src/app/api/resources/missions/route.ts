import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  try {
    const supabase = await createServerSupabaseClient();

    console.log("Missions API - Fetching active missions");

    const { data: missions, error } = await supabase
      .from("missions")
      .select(`
        id,
        title,
        description,
        status
      `)
      .eq("status", "active")
      .order("title", { ascending: true });

    if (error) {
      console.error("Supabase error fetching missions:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    console.log("Missions API - Fetched missions count:", missions?.length || 0);

    const { data: resources, error: resourcesError } = await supabase
      .from("resources")
      .select("mission_id")
      .not("mission_id", "is", null);

    if (resourcesError) {
      console.error("Supabase error fetching resources for count:", resourcesError);
    }

    const resourceCountMap: Record<number, number> = {};
    for (const r of (resources ?? [])) {
      if (r.mission_id) {
        resourceCountMap[r.mission_id] = (resourceCountMap[r.mission_id] || 0) + 1;
      }
    }

    const result = (missions ?? []).map((m: any) => ({
      id: m.id,
      title: m.title,
      description: m.description,
      status: m.status,
      resource_count: resourceCountMap[m.id] || 0,
    }));

    return NextResponse.json(result);
  } catch (err: any) {
    console.error("Unexpected error in missions route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
