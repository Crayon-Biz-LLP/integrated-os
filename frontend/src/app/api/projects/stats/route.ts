import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

interface ProjectRow {
  id: number;
  status: string;
  is_active: boolean;
}

interface TaskRow {
  id: number;
  project_id: number | null;
}

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
    .select("id, project_id")
    .in("status", ["todo", "in_progress", "blocked"]);

  if (tasksError) {
    return NextResponse.json({ error: tasksError.message }, { status: 500 });
  }

  const projectsList = (projects ?? []) as ProjectRow[];
  const tasksList = (tasks ?? []) as TaskRow[];

  const totalActive = projectsList.filter(
    (p) => p.is_active === true && p.status === "active"
  ).length;

  const totalArchived = projectsList.filter(
    (p) => p.status === "archived"
  ).length;

  const totalOpenTasks = tasksList.length;

  const activeProjectIds = new Set(
    projectsList
      .filter((p) => p.is_active === true && p.status === "active")
      .map((p) => p.id)
  );

  const idleProjects = Array.from(activeProjectIds).filter((id) => {
    const count = tasksList.filter((t) => t.project_id === id).length;
    return count === 0;
  }).length;

  return NextResponse.json({
    totalActive,
    totalArchived,
    totalOpenTasks,
    idleProjects,
  });
}