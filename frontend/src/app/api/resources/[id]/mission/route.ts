import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const { mission_id } = await req.json();
  const supabase = await createServerSupabaseClient();

  const { data, error } = await supabase
    .from("resources")
    .update({ mission_id: mission_id || null })
    .eq("id", Number(id))
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
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json(data);
}
