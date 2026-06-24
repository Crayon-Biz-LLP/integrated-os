import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

interface ProjectRow {
  id: number;
  name: string;
  status: string;
  context: string;
  description: string | null;
  created_at: string | null;
  is_active: boolean;
  parent_project_id: number | null;
  keywords: string[] | null;
  organization_id?: string | null;
  is_org_proxy?: boolean;
}

interface EnrichedProject {
  id: number;
  name: string;
  status: string;
  context: string;
  description: string | null;
  created_at: string | null;
  is_active: boolean;
  parent_project_id: number | null;
  parent_project_name: string | null;
  keywords: string[];
  open_task_count: number;
  organization_id?: string | null;
  organization_name?: string | null;
  is_org_proxy?: boolean;
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const supabase = await createServerSupabaseClient();
  const isOrgRoutingEnabled = true;

  const search = searchParams.get("search");
  const context = searchParams.get("context");
  const status = searchParams.get("status");

  let query = supabase.from("projects").select("*");
  if (isOrgRoutingEnabled) {
    query = query.eq("is_org_proxy", false);
  }

  const { data: projectsData, error: projectsError } = await query
    .order("name", { ascending: true })
    .limit(100);

  if (projectsError) {
    return NextResponse.json({ error: projectsError.message }, { status: 500 });
  }

  // Fetch organizations if enabled
  let orgNames: Record<string, string> = {};
  if (isOrgRoutingEnabled) {
    const { data: orgsData } = await supabase.from("organizations").select("id, name");
    if (orgsData) {
      orgsData.forEach((o) => {
        orgNames[o.id] = o.name;
      });
    }
  }

  const { data: taskCounts } = await supabase
    .from("tasks")
    .select("project_id")
    .eq("is_current", true)
    .in("status", ["todo", "in_progress", "blocked"])
    .limit(500);

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
        .in("id", Array.from(parentIds))
        .limit(100);
      
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
      organization_name: p.organization_id && orgNames[p.organization_id] ? orgNames[p.organization_id] : null,
    }));

    if (search) {
      projects = projects.filter((p) =>
        p.name.toLowerCase().includes(search.toLowerCase())
      );
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