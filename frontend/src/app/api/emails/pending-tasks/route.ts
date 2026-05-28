import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  const supabase = await createServerSupabaseClient();
  const { data, error } = await supabase
    .from("email_pending_tasks")
    .select(`*, email:emails(subject, sender_email, sender)`)
    .is("danny_decision", null)
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  return NextResponse.json(data || []);
}
