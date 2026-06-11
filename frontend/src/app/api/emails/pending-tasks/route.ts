import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  const supabase = await createServerSupabaseClient();
  const { data, error } = await supabase
    .from("messages")
    .select(`
      id, suggested_title, suggested_project, is_human_sender,
      created_at, danny_decision,
      subject, sender_id, sender_name
    `)
    .eq("channel", "email")
    .is("danny_decision", null)
    .in("classification", ["actionable", "fyi"])
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const mappedData = (data || []).map(row => ({
    id: row.id,
    email_id: row.id,
    suggested_title: row.suggested_title,
    suggested_project: row.suggested_project,
    is_human_sender: row.is_human_sender,
    created_at: row.created_at,
    danny_decision: row.danny_decision,
    email: {
      subject: row.subject,
      sender_email: row.sender_id,
      sender: row.sender_name,
    }
  }));

  return NextResponse.json(mappedData);
}
