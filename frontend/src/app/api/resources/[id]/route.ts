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
      mission_id,
      created_at,
      enriched_at,
      missions:missions!resources_mission_id_fkey (
        id,
        title,
        status,
        description
      )
    `)
    .eq("id", Number(id))
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const missionsData = Array.isArray(data.missions) ? data.missions[0] : data.missions;
  const resource = {
    id: data.id,
    url: data.url,
    title: data.title,
    summary: data.summary,
    strategic_note: data.strategic_note,
    category: data.category,
    mission_id: data.mission_id,
    created_at: data.created_at,
    enriched_at: data.enriched_at,
    mission_title: missionsData?.title ?? null,
    mission_status: missionsData?.status ?? null,
    mission_description: missionsData?.description ?? null,
    hostname: getHostname(data.url),
  };

  return NextResponse.json(resource);
}
