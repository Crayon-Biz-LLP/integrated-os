import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json();
  const { role, strategic_weight } = body;

  if (role === undefined && strategic_weight === undefined) {
    return NextResponse.json(
      { error: "No fields to update" },
      { status: 400 }
    );
  }

  const updates: any = {};
  if (role !== undefined) updates.role = role;
  if (strategic_weight !== undefined) {
    if (typeof strategic_weight !== 'number' || strategic_weight < 1 || strategic_weight > 10) {
      return NextResponse.json(
        { error: "strategic_weight must be a number between 1 and 10" },
        { status: 400 }
      );
    }
    updates.strategic_weight = strategic_weight;
  }

  const supabase = await createServerSupabaseClient();

  const { data, error } = await supabase
    .from("people")
    .update(updates)
    .eq("id", Number(id))
    .select("id, name, role, strategic_weight, created_at")
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json(data);
}
