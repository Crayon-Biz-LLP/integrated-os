import { createServerSupabaseClient } from "@/lib/supabase-server";
import { computeTaskStats } from "@/lib/tasks/stats";
import type { Task } from "@/lib/tasks/types";
import { TasksShell } from "./tasks-shell";

export const dynamic = 'force-dynamic';

export default async function Page() {
  const supabase = await createServerSupabaseClient();

  const [tasksRes, statsRes, orgsRes] = await Promise.all([
    supabase
      .from("tasks")
      .select(`
        id, title, status, priority, estimated_minutes,
        is_revenue_critical, deadline, created_at, completed_at,
        reminder_at, duration_mins, recurrence, organization_id
      `)
      .eq("is_current", true)
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("tasks")
      .select("id, status, reminder_at, deadline, completed_at")
      .eq("is_current", true)
      .limit(500),
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

  function mapTask(t: any): Task {
    return {
      id: t.id,
      title: t.title,
      status: t.status ?? "todo",
      priority: t.priority ?? "medium",
      organization_id: t.organization_id ?? null,
      organization_name: t.organization_id ? orgNames[t.organization_id] : null,
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

  const tasks: Task[] = (tasksRes.data ?? []).map(mapTask);
  const stats = computeTaskStats(statsRes.data ?? []);

  return <TasksShell initialTasks={tasks} initialStats={stats} />;
}