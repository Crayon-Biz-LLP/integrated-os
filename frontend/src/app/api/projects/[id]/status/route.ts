import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const { status } = await req.json();

  if (!status || !["active", "archived"].includes(status)) {
    return NextResponse.json(
      { error: "Invalid status. Must be 'active' or 'archived'" },
      { status: 400 }
    );
  }

  const supabase = await createServerSupabaseClient();

  const { data, error } = await supabase
    .from("projects")
    .update({
      status,
      is_active: status === "active",
    })
    .eq("id", Number(id))
    .select("*")
    .single();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json(data);
}