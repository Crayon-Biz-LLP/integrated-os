import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

const priorityOrder: Record<string, number> = {
  urgent: 0,
  high: 1,
  medium: 2,
  low: 3,
  important: 4,
};

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const supabase = await createServerSupabaseClient();

  const { data: tasks, error } = await supabase
    .from("tasks")
    .select(
      "id, title, status, priority, reminder_at, deadline, created_at, is_revenue_critical"
    )
    .eq("project_id", Number(id))
    .in("status", ["todo", "in_progress", "blocked"])
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const sortedTasks = (tasks ?? []).sort((a, b) => {
    const priorityA = priorityOrder[a.priority] ?? 5;
    const priorityB = priorityOrder[b.priority] ?? 5;
    if (priorityA !== priorityB) return priorityA - priorityB;
    return (
      new Date(b.created_at ?? 0).getTime() -
      new Date(a.created_at ?? 0).getTime()
    );
  });

  return NextResponse.json(sortedTasks);
}