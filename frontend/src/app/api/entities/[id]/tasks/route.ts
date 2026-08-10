import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

/**
 * Active tasks mentioning a live entity (people + orgs).
 *
 * Relocated from /api/people/[id]/tasks during the People→Entities
 * consolidation (the People tab was removed; task viewing moved into the
 * Entities edit dialog). The path keeps the node id for a future owner-based
 * query, but matching is by label like the old route.
 *
 * NOTE: requires migration 75 (tasks.organization_id -> graph_nodes). The
 * org join is hardcoded to graph_nodes(label) — run db/75 before deploying.
 */
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  await params; // node id reserved for a future owner-based query
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
      organization_id,
      graph_nodes (
        label
      )
    `)
    .ilike("title", `%${name}%`)
    .eq("is_current", true)
    .filter("status", "not.in", "(done,cancelled)")
    .order("created_at", { ascending: false })
    .limit(100);

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

  // PostgREST embeds the to-one graph_nodes relation as an OBJECT (verified
  // live), but supabase-js's select-string inference types embedded relations
  // as arrays — so the row shape is typed explicitly here.
  type TaskRow = {
    id: number;
    title: string;
    status: string;
    priority: string | null;
    reminder_at: string | null;
    deadline: string | null;
    created_at: string | null;
    organization_id: string | null;
    graph_nodes: { label: string | null } | null;
  };

  const tasks = ((data ?? []) as unknown as TaskRow[])
    .map((t) => ({
      id: t.id,
      title: t.title,
      status: t.status,
      priority: t.priority,
      reminder_at: t.reminder_at,
      deadline: t.deadline,
      created_at: t.created_at,
      organization_id: t.organization_id,
      organization_name: t.graph_nodes?.label || null,
    }))
    .sort((a, b) => {
      const priorityDiff = (priorityOrder[a.priority || ""] || 6) - (priorityOrder[b.priority || ""] || 6);
      if (priorityDiff !== 0) return priorityDiff;
      return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
    });

  return NextResponse.json(tasks);
}
