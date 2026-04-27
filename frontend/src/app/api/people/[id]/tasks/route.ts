import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const { searchParams } = new URL(req.url);
  const name = searchParams.get("name");

  if (!name) {
    return NextResponse.json(
      { error: "name parameter is required" },
      { status: 400 }
    );
  }

  const supabase = await createServerSupabaseClient();

  const { data, error } = await supabase
    .from("tasks")
    .select(`
      id,
      title,
      status,
      priority,
      reminder_at,
      deadline,
      created_at,
      project_id,
      projects (
        name
      )
    `)
    .ilike("title", `%${name}%`)
    .filter("status", "not.in", "(done,cancelled)")
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  // Sort by priority first, then created_at
  const priorityOrder: Record<string, number> = {
    urgent: 1,
    high: 2,
    important: 3,
    medium: 4,
    low: 5,
  };

  const tasks = (data ?? [])
    .map((t: any) => ({
      id: t.id,
      title: t.title,
      status: t.status,
      priority: t.priority,
      reminder_at: t.reminder_at,
      deadline: t.deadline,
      created_at: t.created_at,
      project_id: t.project_id,
      project_name: t.projects?.name || null,
    }))
    .sort((a: any, b: any) => {
      const priorityDiff = (priorityOrder[a.priority] || 6) - (priorityOrder[b.priority] || 6);
      if (priorityDiff !== 0) return priorityDiff;
      return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
    });

  return NextResponse.json(tasks);
}
