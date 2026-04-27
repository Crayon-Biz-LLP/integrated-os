import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const { project_id } = await req.json();

  const supabase = await createServerSupabaseClient();

  const { data, error } = await supabase
    .from("tasks")
    .update({ project_id })
    .eq("id", Number(id))
    .select("id, project_id")
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json(data);
}