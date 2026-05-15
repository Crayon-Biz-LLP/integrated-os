import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
    const supabase = await createServerSupabaseClient();

    const { data: missions, error } = await supabase
      .from("missions")
      .select(`
        id,
        title,
        description,
        status
      `)
      .eq("status", "active")
      .order("title", { ascending: true })
      .limit(100);

    if (error) {
      console.error("Supabase error fetching missions:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const { data: resources, error: resourcesError } = await supabase
      .from("resources")
      .select("mission_id")
      .not("mission_id", "is", null)
      .limit(500);

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
