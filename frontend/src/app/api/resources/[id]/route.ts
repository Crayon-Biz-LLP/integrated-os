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
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
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
        missions!mission_id(id, title, status, description)
      `)
      .eq("id", Number(id))
      .single();

    if (error) {
      console.error("Supabase error fetching resource:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const missionData = Array.isArray(data.missions) ? data.missions[0] : data.missions;

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
      hostname: getHostname(data.url),
      mission_title: missionData?.title ?? null,
      mission_status: missionData?.status ?? null,
      mission_description: missionData?.description ?? null,
    };

    return NextResponse.json(resource);
  } catch (err: any) {
    console.error("Unexpected error in resource [id] route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
