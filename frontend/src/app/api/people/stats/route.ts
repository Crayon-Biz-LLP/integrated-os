import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(req: NextRequest) {
  const supabase = await createServerSupabaseClient();

  // Fetch all people
  const { data: people, error: peopleError } = await supabase
    .from("people")
    .select("id, name, strategic_weight, created_at");

  if (peopleError) {
    return NextResponse.json({ error: peopleError.message }, { status: 500 });
  }

  // Fetch open tasks
  const { data: openTasks, error: tasksError } = await supabase
    .from("tasks")
    .select("id, title, status")
    .filter("status", "not.in", "(done,cancelled)");

  if (tasksError) {
    return NextResponse.json({ error: tasksError.message }, { status: 500 });
  }

  const now = new Date();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);

  // Compute stats
  const total = people?.length || 0;
  const highPriority = (people || []).filter((p: any) => (p.strategic_weight || 0) >= 8).length;

  // Count people with active tasks (name appears in open task title)
  const peopleWithActiveTasks = new Set(
    (people || [])
      .filter((p: any) =>
        (openTasks || []).some((t: any) =>
          t.title.toLowerCase().includes(p.name.toLowerCase())
        )
      )
      .map((p: any) => p.id)
  );
  const withActiveTasks = peopleWithActiveTasks.size;

  const recentlyAdded = (people || []).filter((p: any) => {
    if (!p.created_at) return false;
    return new Date(p.created_at) > thirtyDaysAgo;
  }).length;

  return NextResponse.json({
    total,
    highPriority,
    withActiveTasks,
    recentlyAdded,
  });
}
