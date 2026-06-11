import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Email, EmailStats as EmailStatsData, EmailPendingTask, EmailDraft } from "@/lib/emails/types";
import { EmailsShell } from "./emails-shell";

export const dynamic = 'force-dynamic';

export default async function EmailsPage() {
  const supabase = await createServerSupabaseClient();

  const [emailsRes, emailClassRes, pendingTasksRes, draftsRes, pendingDraftsCountRes] = await Promise.all([
    supabase
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
      .limit(100),
    supabase
      .from("messages")
      .select("classification")
      .eq("channel", "email")
      .limit(500),
    supabase
      .from("messages")
      .select(`
        id, suggested_title, suggested_project, is_human_sender,
        created_at, danny_decision,
        subject, sender_id, sender_name
      `)
      .eq("channel", "email")
      .is("danny_decision", null)
      .eq("classification", "actionable")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("email_drafts")
      .select(`*, message:messages(subject, sender_id, sender_name, source)`)
      .eq("status", "pending")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("email_drafts")
      .select("id", { count: "exact", head: true })
      .eq("status", "pending"),
  ]);

  const rawEmails = emailsRes.data ?? [];
  const emails = rawEmails.map((row: any) => ({
    ...row,
    sender: row.sender_name,
    sender_email: row.sender_id,
    body_summary: row.metadata?.body_summary ?? row.body,
  })) as unknown as Email[];

  const emailClassList = emailClassRes.data ?? [];
  
  const rawPendingTasks = pendingTasksRes.data ?? [];
  const pendingTasks = rawPendingTasks.map((row: any) => ({
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
  })) as unknown as EmailPendingTask[];

  const rawDrafts = draftsRes.data ?? [];
  const drafts = rawDrafts.map((row: any) => ({
    ...row,
    email_id: row.message_id,
    email: row.message ? {
      subject: row.message.subject,
      sender_email: row.message.sender_id,
      sender: row.message.sender_name,
      source: row.message.source,
    } : null
  })) as unknown as EmailDraft[];

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
