import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(req: NextRequest) {
  const supabase = await createServerSupabaseClient();
  const isOrgRoutingEnabled = true;

  if (!isOrgRoutingEnabled) {
    return NextResponse.json([]);
  }

  const { data: orgsData, error: orgsError } = await supabase
    .from("organizations")
    .select("*")
    .order("name", { ascending: true });

  if (orgsError) {
    return NextResponse.json({ error: orgsError.message }, { status: 500 });
  }

  return NextResponse.json(orgsData);
}
