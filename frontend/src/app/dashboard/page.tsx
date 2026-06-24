import { createServerSupabaseClient } from "@/lib/supabase-server";
import { computeTaskStats } from "@/lib/tasks/stats";
import type { Task, TaskStats } from "@/lib/tasks/types";
import type { EmailPendingTask, EmailStats } from "@/lib/emails/types";
import { DashboardShell } from "./dashboard-shell";

export const dynamic = 'force-dynamic';

export default async function DashboardPage() {
  const supabase = await createServerSupabaseClient();

  const [tasksRes, taskStatsRes, pendingEmailsRes, emailClassRes, pendingDraftsCountRes, orgsRes] = await Promise.all([
    supabase
      .from("tasks")
      .select(`
        id, title, status, priority, project_id, estimated_minutes,
        is_revenue_critical, deadline, created_at, completed_at,
        reminder_at, duration_mins, recurrence, organization_id,
        projects ( id, name, organization_id )
      `)
      .eq("is_current", true)
      .filter("status", "not.in", "(done,cancelled)")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("tasks")
      .select("id, status, reminder_at, deadline, completed_at")
      .eq("is_current", true)
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
      .limit(100),
    supabase
      .from("messages")
      .select("classification")
      .eq("channel", "email")
      .limit(500),
    supabase
      .from("email_drafts")
      .select("id", { count: "exact", head: true })
      .eq("status", "pending"),
    supabase
      .from("organizations")
      .select("id, name")
  ]);

  const orgNames: Record<string, string> = {};
  if (orgsRes.data) {
    orgsRes.data.forEach((o: any) => {
      orgNames[o.id] = o.name;
    });
  }

  function mapOpenTask(t: any): Task {
    const proj = Array.isArray(t.projects) ? t.projects[0] : t.projects;
    const org_id = t.organization_id || proj?.organization_id;
    return {
      id: t.id,
      title: t.title,
      status: t.status ?? "todo",
      priority: t.priority ?? "medium",
      project_id: t.project_id,
      project_name: proj?.name ?? "General",
      organization_id: org_id ?? null,
      organization_name: org_id ? orgNames[org_id] : null,
      estimated_minutes: t.estimated_minutes,
      is_revenue_critical: t.is_revenue_critical ?? false,
      deadline: t.deadline,
      created_at: t.created_at,
      completed_at: t.completed_at,
      reminder_at: t.reminder_at,
      duration_mins: t.duration_mins,
      recurrence: t.recurrence ?? null,
    };
  }

  const openTasks: Task[] = (tasksRes.data ?? []).map(mapOpenTask);
  const taskStats: TaskStats = computeTaskStats(taskStatsRes.data ?? []);
  
  const rawPendingEmails = pendingEmailsRes.data ?? [];
  const pendingEmails: EmailPendingTask[] = rawPendingEmails.map((row: any) => ({
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

  const emailClassList = emailClassRes.data ?? [];
  const emailStats: EmailStats = {
    total: emailClassList.length,
    actionable: emailClassList.filter((e: any) => e.classification === "actionable").length,
    fyi: emailClassList.filter((e: any) => e.classification === "fyi").length,
    pending_tasks: pendingEmails.length,
    pending_drafts: pendingDraftsCountRes.count ?? 0,
  };

  return (
    <DashboardShell
      initialOpenTasks={openTasks}
      initialTaskStats={taskStats}
      initialPendingEmails={pendingEmails}
      initialEmailStats={emailStats}
    />
  );
}
