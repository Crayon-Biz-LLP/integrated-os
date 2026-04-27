import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  const supabase = await createServerSupabaseClient();

  const { data: projects, error } = await supabase
    .from("projects")
    .select("id, status, is_active");

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const { data: tasks, error: tasksError } = await supabase
    .from("tasks")
    .select("id")
    .not("status", "in", '("done","cancelled")');

  if (tasksError) {
    return NextResponse.json({ error: tasksError.message }, { status: 500 });
  }

  const totalActive = (projects ?? []).filter(
    (p) => p.is_active === true && p.status === "active"
  ).length;

  const totalArchived = (projects ?? []).filter(
    (p) => p.status === "archived"
  ).length;

  const totalOpenTasks = (tasks ?? []).length;

  const activeProjectIds = new Set(
    (projects ?? [])
      .filter((p) => p.is_active === true && p.status === "active")
      .map((p) => p.id)
  );

  const taskCountByProject: Record<number, number> = {};
  (tasks ?? []).forEach((t) => {
    if (t.id) {
      taskCountByProject[t.id] = (taskCountByProject[t.id] || 0) + 1;
    }
  });

  const idleProjects = Array.from(activeProjectIds).filter((id) => {
    const count = (tasks ?? []).filter((t) => t.project_id === id).length;
    return count === 0;
  }).length;

  return NextResponse.json({
    totalActive,
    totalArchived,
    totalOpenTasks,
    idleProjects,
  });
}