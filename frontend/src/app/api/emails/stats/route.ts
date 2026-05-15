import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  const supabase = await createServerSupabaseClient();

  const [emailsRes, pendingTasksRes, pendingDraftsRes] = await Promise.all([
    supabase.from("emails").select("classification").limit(500),
    supabase.from("email_pending_tasks").select("id", { count: "exact", head: true }).is("danny_decision", null),
    supabase.from("email_drafts").select("id", { count: "exact", head: true }).eq("status", "pending"),
  ]);

  const emails = emailsRes.data ?? [];
  const total = emails.length;
  const actionable = emails.filter((e) => e.classification === "actionable").length;
  const fyi = emails.filter((e) => e.classification === "fyi").length;

  return NextResponse.json({
    total,
    actionable,
    fyi,
    pending_tasks: pendingTasksRes.count ?? 0,
    pending_drafts: pendingDraftsRes.count ?? 0,
  });
}
