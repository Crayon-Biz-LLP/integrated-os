import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  const supabase = await createServerSupabaseClient();
  const { data, error } = await supabase
    .from("email_drafts")
    .select(`*, message:messages(subject, sender_id, sender_name, source)`)
    .eq("status", "pending")
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const mappedData = (data || []).map((row: any) => ({
    ...row,
    email_id: row.message_id,
    email: row.message ? {
      subject: row.message.subject,
      sender_email: row.message.sender_id,
      sender: row.message.sender_name,
      source: row.message.source,
    } : null
  }));

  return NextResponse.json(mappedData);
}

export async function PATCH(request: NextRequest) {
  const supabase = await createServerSupabaseClient();
  const body = await request.json();
  const { id, action, draft_body } = body;

  if (!id) {
    return NextResponse.json({ error: "id is required" }, { status: 400 });
  }

  if (action === "rejected") {
    const { error } = await supabase
      .from("email_drafts")
      .update({ status: "rejected" })
      .eq("id", id);
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json({ success: true });
  }

  if (draft_body !== undefined) {
    const { error } = await supabase
      .from("email_drafts")
      .update({ draft_body })
      .eq("id", id);
    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }
    return NextResponse.json({ success: true });
  }

  return NextResponse.json(
    { error: "action or draft_body required" },
    { status: 400 },
  );
}
