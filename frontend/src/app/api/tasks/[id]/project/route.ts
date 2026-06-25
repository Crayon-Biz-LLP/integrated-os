import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();
  const { project_id } = body;
  
  const updateData: any = { project_id };
  if ('organization_id' in body) {
    updateData.organization_id = body.organization_id;
  }

  const supabase = await createServerSupabaseClient();

  const { data, error } = await supabase
    .from("tasks")
    .update(updateData)
    .eq("id", Number(id))
    .select("id, project_id, organization_id")
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json(data);
}