import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

interface ProjectRow {
  id: number;
  name: string;
  status: string;
  context: string;
  description: string | null;
  created_at: string | null;
  org_tag: string | null;
  is_active: boolean;
  parent_project_id: number | null;
  keywords: string[] | null;
  parent: { id: number; name: string } | null;
}

interface EnrichedProject extends ProjectRow {
  parent_project_name: string | null;
  open_task_count: number;
  keywords: string[];
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const search = searchParams.get("search");
  const orgTag = searchParams.get("orgTag");
  const context = searchParams.get("context");
  const status = searchParams.get("status");

  const { data: fallbackData, error: fallbackError } = await supabase
    .from("projects")
    .select(`
      *,
      parent:projects!projects_parent_project_id_fkey(
        id,
        name
      )
    `)
    .order("org_tag", { ascending: true })
    .order("name", { ascending: true });

  if (fallbackError) {
    return NextResponse.json({ error: fallbackError.message }, { status: 500 });
  }

  const { data: taskCounts } = await supabase
    .from("tasks")
    .select("project_id")
    .not("status", "in", '("done","cancelled")');

  const taskCountMap: Record<number, number> = {};
  (taskCounts ?? []).forEach((t) => {
    if (t.project_id) {
      taskCountMap[t.project_id] = (taskCountMap[t.project_id] || 0) + 1;
    }
  });

  let projects: EnrichedProject[] = (fallbackData ?? []).map((p: ProjectRow) => ({
    ...p,
    parent_project_name: p.parent?.name ?? null,
    open_task_count: taskCountMap[p.id] || 0,
    keywords: p.keywords ?? [],
  }));

  if (search) {
    projects = projects.filter((p) =>
      p.name.toLowerCase().includes(search.toLowerCase())
    );
  }
  if (orgTag && orgTag !== "all") {
    projects = projects.filter((p) => p.org_tag === orgTag);
  }
  if (context && context !== "all") {
    projects = projects.filter((p) => p.context === context);
  }
  if (status && status !== "all") {
    projects = projects.filter((p) => p.status === status);
  }

  return NextResponse.json(projects);
}