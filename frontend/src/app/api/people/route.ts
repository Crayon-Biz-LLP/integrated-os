import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const search = searchParams.get("search");
  const tier = searchParams.get("tier");
  const sort = searchParams.get("sort") || "strategic_weight";

  const supabase = await createServerSupabaseClient();

  // Fetch all people
  const { data: people, error: peopleError } = await supabase
    .from("people")
    .select("id, name, role, strategic_weight, created_at")
    .order("created_at", { ascending: false });

  if (peopleError) {
    return NextResponse.json({ error: peopleError.message }, { status: 500 });
  }

  // Fetch open tasks to compute active_task_count
  const { data: openTasks, error: tasksError } = await supabase
    .from("tasks")
    .select("id, title, status")
    .filter("status", "not.in", "(done,cancelled)");

  if (tasksError) {
    return NextResponse.json({ error: tasksError.message }, { status: 500 });
  }

  // Compute active_task_count for each person
  let result = (people || []).map((person: any) => {
    const active_task_count = (openTasks || []).filter((task: any) =>
      task.title?.toLowerCase().includes(person.name.toLowerCase())
    ).length;

    return {
      id: person.id,
      name: person.name,
      role: person.role,
      strategic_weight: person.strategic_weight,
      created_at: person.created_at,
      active_task_count,
    };
  });

  // Apply search filter
  if (search) {
    const searchLower = search.toLowerCase();
    result = result.filter(
      (p) =>
        p.name.toLowerCase().includes(searchLower) ||
        (p.role && p.role.toLowerCase().includes(searchLower))
    );
  }

  // Apply tier filter
  if (tier && tier !== "all") {
    result = result.filter((p) => {
      const weight = p.strategic_weight || 0;
      switch (tier) {
        case "critical": return weight >= 9;
        case "high": return weight >= 7 && weight <= 8;
        case "medium": return weight >= 4 && weight <= 6;
        case "low": return weight >= 1 && weight <= 3;
        default: return true;
      }
    });
  }

  // Apply sorting
  result.sort((a, b) => {
    switch (sort) {
      case "strategic_weight":
        return (b.strategic_weight || 0) - (a.strategic_weight || 0);
      case "name":
        return a.name.localeCompare(b.name);
      case "recently_added":
        return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
      default:
        return (b.strategic_weight || 0) - (a.strategic_weight || 0);
    }
  });

  return NextResponse.json(result);
}
