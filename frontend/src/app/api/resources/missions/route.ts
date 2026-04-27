import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
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
    .order("title", { ascending: true });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const { data: resources } = await supabase
    .from("resources")
    .select("mission_id")
    .not("mission_id", "is", null);

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
}
