import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Email, EmailStats as EmailStatsData, EmailPendingTask, EmailDraft } from "@/lib/emails/types";
import { EmailsShell } from "./emails-shell";

export const dynamic = 'force-dynamic';

export default async function EmailsPage() {
  const supabase = await createServerSupabaseClient();

  const [emailsRes, emailClassRes, pendingTasksRes, draftsRes, pendingDraftsCountRes] = await Promise.all([
    supabase
      .from("emails")
      .select(`
        id, subject, sender, sender_email, body_summary,
        classification, source, received_at,
        linked_project_id, linked_person_id,
        linked_project:projects(name),
        linked_person:people(name)
      `)
      .order("received_at", { ascending: false })
      .limit(100),
    supabase
      .from("emails")
      .select("classification")
      .limit(500),
    supabase
      .from("email_pending_tasks")
      .select(`*, email:emails(subject, sender_email, sender)`)
      .is("danny_decision", null)
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("email_drafts")
      .select(`*, email:emails(subject, sender_email, sender, source)`)
      .eq("status", "pending")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("email_drafts")
      .select("id", { count: "exact", head: true })
      .eq("status", "pending"),
  ]);

  const emails = (emailsRes.data ?? []) as unknown as Email[];
  const emailClassList = emailClassRes.data ?? [];
  const pendingTasks = (pendingTasksRes.data ?? []) as unknown as EmailPendingTask[];
  const drafts = (draftsRes.data ?? []) as unknown as EmailDraft[];

  const emailStats: EmailStatsData = {
    total: emailClassList.length,
    actionable: emailClassList.filter((e: any) => e.classification === "actionable").length,
    fyi: emailClassList.filter((e: any) => e.classification === "fyi").length,
    pending_tasks: pendingTasks.length,
    pending_drafts: pendingDraftsCountRes.count ?? 0,
  };

  return (
    <EmailsShell
      initialEmails={emails}
      initialStats={emailStats}
      initialPendingTasks={pendingTasks}
      initialDrafts={drafts}
      initialStatsLoading={false}
    />
  );
}
