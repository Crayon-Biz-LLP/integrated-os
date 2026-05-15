import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Person, PeopleStats } from "@/lib/people/types";
import { PeopleShell } from "./people-shell";

export const dynamic = 'force-dynamic';

function computePeopleStats(people: Person[], openTasks: Array<{ id: number; title: string }>): PeopleStats {
  const now = new Date();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);

  const total = people.length;
  const highPriority = people.filter((p) => (p.strategic_weight || 0) >= 8).length;

  const peopleWithActiveTasks = new Set(
    people
      .filter((p) =>
        openTasks.some((t) => t.title.toLowerCase().includes(p.name.toLowerCase()))
      )
      .map((p) => p.id)
  );
  const withActiveTasks = peopleWithActiveTasks.size;

  const recentlyAdded = people.filter((p) => {
    if (!p.created_at) return false;
    return new Date(p.created_at) > thirtyDaysAgo;
  }).length;

  return { total, highPriority, withActiveTasks, recentlyAdded };
}

export default async function PeoplePage() {
  const supabase = await createServerSupabaseClient();

  const [peopleRes, openTasksRes] = await Promise.all([
    supabase
      .from("people")
      .select("id, name, role, strategic_weight, created_at")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("tasks")
      .select("id, title")
      .eq("is_current", true)
      .filter("status", "not.in", "(done,cancelled)")
      .limit(500),
  ]);

  const people = (peopleRes.data ?? []) as Person[];
  const openTasks = (openTasksRes.data ?? []) as Array<{ id: number; title: string }>;

  const peopleWithTaskCount: Person[] = people.map((person) => {
    const active_task_count = openTasks.filter((task) =>
      task.title?.toLowerCase().includes(person.name.toLowerCase())
    ).length;
    return { ...person, active_task_count };
  });

  const stats = computePeopleStats(peopleWithTaskCount, openTasks);

  return <PeopleShell initialPeople={peopleWithTaskCount} initialStats={stats} />;
}
