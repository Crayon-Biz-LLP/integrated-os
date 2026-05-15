import { createServerSupabaseClient } from "@/lib/supabase-server";
import { computeTaskStats } from "@/lib/tasks/stats";
import type { Task, Project } from "@/lib/tasks/types";
import { TasksShell } from "./tasks-shell";

export const dynamic = 'force-dynamic';

function mapTask(t: any): Task {
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

export default async function Page() {
  const supabase = await createServerSupabaseClient();

  const [tasksRes, statsRes, projectsRes] = await Promise.all([
    supabase
      .from("tasks")
      .select(`
        id, title, status, priority, project_id, estimated_minutes,
        is_revenue_critical, deadline, created_at, completed_at,
        reminder_at, duration_mins,
        projects ( id, name, org_tag )
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
      .from("projects")
      .select("id, name, org_tag, is_active, status")
      .eq("is_active", true)
      .order("name", { ascending: true })
      .limit(100),
  ]);

  const tasks: Task[] = (tasksRes.data ?? []).map(mapTask);
  const projects: Project[] = (projectsRes.data ?? []) as Project[];
  const stats = computeTaskStats(statsRes.data ?? []);

  return <TasksShell initialTasks={tasks} initialStats={stats} projects={projects} />;
}