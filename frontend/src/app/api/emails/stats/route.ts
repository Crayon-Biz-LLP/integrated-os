import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  const supabase = await createServerSupabaseClient();

  const [emailsRes, pendingTasksRes, pendingDraftsRes] = await Promise.all([
    supabase.from("messages").select("classification").eq("channel", "email").limit(500),
    supabase.from("messages").select("id", { count: "exact", head: true }).eq("channel", "email").is("danny_decision", null).in("classification", ["actionable", "fyi"]),
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
