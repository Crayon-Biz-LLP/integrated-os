import { createServerSupabaseClient } from "@/lib/supabase-server";
import { computeTaskStats } from "@/lib/tasks/stats";
import type { Task, TaskStats } from "@/lib/tasks/types";
import type { EmailPendingTask, EmailStats } from "@/lib/emails/types";
import { DashboardShell } from "./dashboard-shell";

export const dynamic = 'force-dynamic';

function mapOpenTask(t: any): Task {
  const proj = Array.isArray(t.projects) ? t.projects[0] : t.projects;
  return {
    id: t.id,
    title: t.title,
    status: t.status ?? "todo",
    priority: t.priority ?? "medium",
    project_id: t.project_id,
    project_name: proj?.name ?? "Inbox",
    project_org_tag: proj?.org_tag ?? null,
    estimated_minutes: t.estimated_minutes,
    is_revenue_critical: t.is_revenue_critical ?? false,
    deadline: t.deadline,
    created_at: t.created_at,
    completed_at: t.completed_at,
    reminder_at: t.reminder_at,
    duration_mins: t.duration_mins,
  };
}

export default async function DashboardPage() {
  const supabase = await createServerSupabaseClient();

  const [tasksRes, taskStatsRes, pendingEmailsRes, emailClassRes, pendingDraftsCountRes] = await Promise.all([
    supabase
      .from("tasks")
      .select(`
        id, title, status, priority, project_id, estimated_minutes,
        is_revenue_critical, deadline, created_at, completed_at,
        reminder_at, duration_mins,
        projects ( id, name, org_tag )
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
      .from("email_pending_tasks")
      .select(`*, email:emails(subject, sender_email, sender)`)
      .is("danny_decision", null)
      .limit(100),
    supabase
      .from("emails")
      .select("classification")
      .limit(500),
    supabase
      .from("email_drafts")
      .select("id", { count: "exact", head: true })
      .eq("status", "pending"),
  ]);

  const openTasks: Task[] = (tasksRes.data ?? []).map(mapOpenTask);
  const taskStats: TaskStats = computeTaskStats(taskStatsRes.data ?? []);
  const pendingEmails: EmailPendingTask[] = (pendingEmailsRes.data ?? []) as unknown as EmailPendingTask[];

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
