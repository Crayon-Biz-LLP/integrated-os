import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

interface OrgRow {
  id: string;
  is_active: boolean;
}

interface TaskRow {
  id: number;
  organization_id: string | null;
}

export async function GET() {
  const supabase = await createServerSupabaseClient();

  const { data: orgs, error } = await supabase
    .from("organizations")
    .select("id, is_active")
    .limit(500);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const { data: tasks, error: tasksError } = await supabase
    .from("tasks")
    .select("id, organization_id")
    .eq("is_current", true)
    .in("status", ["todo", "in_progress", "blocked"])
    .not("organization_id", "is", null)
    .limit(500);

  if (tasksError) {
    return NextResponse.json({ error: tasksError.message }, { status: 500 });
  }

  const orgsList = (orgs ?? []) as OrgRow[];
  const tasksList = (tasks ?? []) as TaskRow[];

  const totalActive = orgsList.filter((o) => o.is_active === true).length;
  const totalInactive = orgsList.filter((o) => o.is_active === false).length;

  const totalOpenTasks = tasksList.length;

  const activeOrgIds = new Set(
    orgsList.filter((o) => o.is_active === true).map((o) => o.id)
  );

  const idleOrgs = Array.from(activeOrgIds).filter((id) => {
    const count = tasksList.filter((t) => t.organization_id === id).length;
    return count === 0;
  }).length;

  return NextResponse.json({
    totalActive,
    totalInactive,
    totalOpenTasks,
    idleOrgs,
  });
}