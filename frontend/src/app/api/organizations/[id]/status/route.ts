import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const { is_active } = await req.json();

  if (typeof is_active !== "boolean") {
    return NextResponse.json(
      { error: "is_active (boolean) required" },
      { status: 400 }
    );
  }

  const supabase = await createServerSupabaseClient();

  const { data, error } = await supabase
    .from("organizations")
    .update({ is_active })
    .eq("id", id)
    .select("id, name, is_active, created_at")
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json(data);
}