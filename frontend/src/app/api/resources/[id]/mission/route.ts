import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
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
      console.error("Supabase error updating resource mission:", error);
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    return NextResponse.json(data);
  } catch (err: any) {
    console.error("Unexpected error in resource mission update route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
