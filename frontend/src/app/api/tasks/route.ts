import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const search = searchParams.get("search");
  const status = searchParams.get("status");
  const priority = searchParams.get("priority");
  const projectId = searchParams.get("projectId");
  const dueWindow = searchParams.get("dueWindow");

  let query = supabase
    .from("tasks")
    .select(`
      id,
      title,
      status,
      priority,
      project_id,
      estimated_minutes,
      is_revenue_critical,
      deadline,
      created_at,
      completed_at,
      reminder_at,
      duration_mins,
      projects (
        id,
        name,
        org_tag
      )
    `)
    .order("created_at", { ascending: false });

  if (search) query = query.ilike("title", `%${search}%`);
  if (status && status !== "all") query = query.eq("status", status);
  if (priority && priority !== "all") query = query.eq("priority", priority);
  if (projectId && projectId !== "all") query = query.eq("project_id", Number(projectId));

  const { data, error } = await query;

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const now = new Date();

  let tasks = (data ?? []).map((t: any) => ({
    id: t.id,
    title: t.title,
    status: t.status ?? "todo",
    priority: t.priority ?? "medium",
    project_id: t.project_id,
    project_name: t.projects?.name ?? "Inbox",
    project_org_tag: t.projects?.org_tag ?? null,
    estimated_minutes: t.estimated_minutes,
    is_revenue_critical: t.is_revenue_critical ?? false,
    deadline: t.deadline,
    created_at: t.created_at,
    completed_at: t.completed_at,
    reminder_at: t.reminder_at,
    duration_mins: t.duration_mins,
  }));

  if (dueWindow && dueWindow !== "all") {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const tomorrow = new Date(today.getTime() + 86400000);

    tasks = tasks.filter((t) => {
      const isDone = ["done", "cancelled"].includes(t.status);
      const dueDate = t.reminder_at || t.deadline;
      if (!dueDate || isDone) return false;
      const d = new Date(dueDate);

      if (dueWindow === "today") return d >= today && d < tomorrow;
      if (dueWindow === "overdue") return d < now;
      if (dueWindow === "upcoming") return d >= tomorrow;
      return true;
    });
  }

  return NextResponse.json(tasks);
}