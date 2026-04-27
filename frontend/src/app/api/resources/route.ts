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
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const search = searchParams.get("search");
  const mission = searchParams.get("mission");
  const category = searchParams.get("category");
  const sort = searchParams.get("sort") || "newest";

  let query = supabase
    .from("resources")
    .select(`
      id,
      url,
      title,
      summary,
      strategic_note,
      category,
      mission_id,
      created_at,
      enriched_at
    `);

  if (search) {
    query = query.or(
      `title.ilike.%${search}%,summary.ilike.%${search}%,strategic_note.ilike.%${search}%,category.ilike.%${search}%`
    );
  }

  if (mission === "unmapped") {
    query = query.is("mission_id", null);
  } else if (mission && mission !== "all") {
    query = query.eq("mission_id", Number(mission));
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
    case "mission":
      query = query.order("mission_id", { ascending: true, nullsFirst: false });
      break;
    default:
      query = query.order("created_at", { ascending: false });
  }

  const { data, error } = await query;

  if (error) {
    console.error("Error fetching resources:", error);
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const resources: any[] = (data ?? []).map((r: any) => ({
    id: r.id,
    url: r.url,
    title: r.title,
    summary: r.summary,
    strategic_note: r.strategic_note,
    category: r.category,
    mission_id: r.mission_id,
    created_at: r.created_at,
    enriched_at: r.enriched_at,
    hostname: getHostname(r.url),
    mission_title: null,
    mission_status: null,
    mission_description: null,
  }));

  const missionIds = [...new Set(resources.map(r => r.mission_id).filter(Boolean))];
  
  if (missionIds.length > 0) {
    const { data: missionsData } = await supabase
      .from("missions")
      .select("id, title, status, description")
      .in("id", missionIds);
    
    const missionsMap: Record<number, any> = {};
    for (const m of (missionsData ?? [])) {
      missionsMap[m.id] = m;
    }

    for (const r of resources) {
      if (r.mission_id && missionsMap[r.mission_id]) {
        r.mission_title = missionsMap[r.mission_id].title;
        r.mission_status = missionsMap[r.mission_id].status;
        r.mission_description = missionsMap[r.mission_id].description;
      }
    }
  }

  return NextResponse.json(resources);
}
