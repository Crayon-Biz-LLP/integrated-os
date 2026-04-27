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

  const { data: resource, error: resourceError } = await supabase
    .from("resources")
    .select("mission_id")
    .eq("id", Number(id))
    .single();

  if (resourceError || !resource?.mission_id) {
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
    .eq("mission_id", resource.mission_id)
    .neq("id", Number(id))
    .limit(5);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const related = (data ?? []).map((r: any) => {
    const missionsData = Array.isArray(r.missions) ? r.missions[0] : r.missions;
    return {
      id: r.id,
      url: r.url,
      title: r.title,
      summary: r.summary,
      strategic_note: r.strategic_note,
      category: r.category,
      mission_id: r.mission_id,
      created_at: r.created_at,
      enriched_at: r.enriched_at,
      mission_title: missionsData?.title ?? null,
      mission_status: missionsData?.status ?? null,
      mission_description: missionsData?.description ?? null,
      hostname: getHostname(r.url),
    };
  });

  return NextResponse.json(related);
}
