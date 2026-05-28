import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const classification = searchParams.get("classification");
  const source = searchParams.get("source");
  const search = searchParams.get("search");

  let query = supabase
    .from("emails")
    .select(`
      id, subject, sender, sender_email, body_summary,
      classification, source, received_at,
      linked_project_id, linked_person_id,
      linked_project:projects(name),
      linked_person:people(name)
    `)
    .order("received_at", { ascending: false })
    .limit(100);

  if (classification && classification !== "all") {
    query = query.eq("classification", classification);
  }
  if (source && source !== "all") {
    query = query.eq("source", source);
  }
  if (search) {
    query = query.or(
      `subject.ilike.%${search}%,sender_email.ilike.%${search}%,sender.ilike.%${search}%`,
    );
  }

  const { data, error } = await query;
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  return NextResponse.json(data || []);
}
