import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const classification = searchParams.get("classification");
  const source = searchParams.get("source");
  const search = searchParams.get("search");

  let query = supabase
    .from("messages")
    .select(`
      id, subject, sender_name, sender_id, metadata, body,
      classification, source, received_at,
      linked_project_id, linked_person_id,
      linked_project:projects(name),
      linked_person:people(name)
    `)
    .eq("channel", "email")
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
      `subject.ilike.%${search}%,sender_id.ilike.%${search}%,sender_name.ilike.%${search}%`,
    );
  }

  const { data, error } = await query;
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  
  const mappedData = (data || []).map((row: any) => ({
    ...row,
    sender: row.sender_name,
    sender_email: row.sender_id,
    body_summary: row.metadata?.body_summary ?? row.body,
  }));
  
  return NextResponse.json(mappedData);
}
