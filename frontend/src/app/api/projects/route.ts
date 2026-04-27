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
}

interface EnrichedProject {
  id: number;
  name: string;
  status: string;
  context: string;
  description: string | null;
  created_at: string | null;
  org_tag: string | null;
  is_active: boolean;
  parent_project_id: number | null;
  parent_project_name: string | null;
  keywords: string[];
  open_task_count: number;
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();

  const search = searchParams.get("search");
  const orgTag = searchParams.get("orgTag");
  const context = searchParams.get("context");
  const status = searchParams.get("status");

  const { data: projectsData, error: projectsError } = await supabase
    .from("projects")
    .select("*")
    .order("org_tag", { ascending: true })
    .order("name", { ascending: true });

  if (projectsError) {
    return NextResponse.json({ error: projectsError.message }, { status: 500 });
  }

  const { data: taskCounts } = await supabase
    .from("tasks")
    .select("project_id")
    .in("status", ["todo", "in_progress", "blocked"]);

  if (taskCounts) {
    const taskCountMap: Record<number, number> = {};
    taskCounts.forEach((t) => {
      if (t.project_id) {
        taskCountMap[t.project_id] = (taskCountMap[t.project_id] || 0) + 1;
      }
    });

    const projectsMap = new Map<number, ProjectRow>();
    (projectsData ?? []).forEach((p) => projectsMap.set(p.id, p));

    const parentIds = new Set<number>();
    (projectsData ?? []).forEach((p) => {
      if (p.parent_project_id) parentIds.add(p.parent_project_id);
    });

    let parentNames: Record<number, string> = {};
    if (parentIds.size > 0) {
      const { data: parentData } = await supabase
        .from("projects")
        .select("id, name")
        .in("id", Array.from(parentIds));
      
      if (parentData) {
        parentData.forEach((p) => {
          parentNames[p.id] = p.name;
        });
      }
    }

    let projects: EnrichedProject[] = (projectsData ?? []).map((p) => ({
      ...p,
      parent_project_name: p.parent_project_id ? parentNames[p.parent_project_id] ?? null : null,
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

  return NextResponse.json([]);
}