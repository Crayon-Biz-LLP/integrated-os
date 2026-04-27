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
      enriched_at
    `)
    .eq("id", Number(id))
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const resource: any = {
    id: data.id,
    url: data.url,
    title: data.title,
    summary: data.summary,
    strategic_note: data.strategic_note,
    category: data.category,
    mission_id: data.mission_id,
    created_at: data.created_at,
    enriched_at: data.enriched_at,
    hostname: getHostname(data.url),
    mission_title: null,
    mission_status: null,
    mission_description: null,
  };

  if (data.mission_id) {
    const { data: missionData } = await supabase
      .from("missions")
      .select("id, title, status, description")
      .eq("id", data.mission_id)
      .single();
    
    if (missionData) {
      resource.mission_title = missionData.title;
      resource.mission_status = missionData.status;
      resource.mission_description = missionData.description;
    }
  }

  return NextResponse.json(resource);
}
