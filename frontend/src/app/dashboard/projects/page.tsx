import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Project, ProjectStats } from "@/lib/projects/types";
import { ProjectsShell } from "./projects-shell";

export const dynamic = 'force-dynamic';

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

interface TaskCountRow {
  project_id: number | null;
}

function computeProjectStats(projects: ProjectRow[], tasks: TaskCountRow[]): ProjectStats {
  const taskCountMap: Record<number, number> = {};
  tasks.forEach((t) => {
    if (t.project_id) {
      taskCountMap[t.project_id] = (taskCountMap[t.project_id] || 0) + 1;
    }
  });

  const totalActive = projects.filter(
    (p) => p.is_active === true && p.status === "active"
  ).length;

  const totalArchived = projects.filter(
    (p) => p.status === "archived"
  ).length;

  const totalOpenTasks = tasks.length;

  const activeProjectIds = new Set(
    projects
      .filter((p) => p.is_active === true && p.status === "active")
      .map((p) => p.id)
  );

  const idleProjects = Array.from(activeProjectIds).filter((id) => {
    const count = tasks.filter((t) => t.project_id === id).length;
    return count === 0;
  }).length;

  return { totalActive, totalArchived, totalOpenTasks, idleProjects };
}

export default async function Page() {
  const supabase = await createServerSupabaseClient();

  const [projectsRes, taskCountsRes] = await Promise.all([
    supabase
      .from("projects")
      .select("*")
      .order("org_tag", { ascending: true })
      .order("name", { ascending: true })
      .limit(100),
    supabase
      .from("tasks")
      .select("project_id")
      .eq("is_current", true)
      .in("status", ["todo", "in_progress", "blocked"])
      .limit(500),
  ]);

  const projectsData = (projectsRes.data ?? []) as ProjectRow[];
  const taskCountsData = (taskCountsRes.data ?? []) as TaskCountRow[];

  const parentIds = new Set<number>();
  projectsData.forEach((p) => {
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
      parentData.forEach((p: any) => {
        parentNames[p.id] = p.name;
      });
    }
  }

  const taskCountMap: Record<number, number> = {};
  taskCountsData.forEach((t) => {
    if (t.project_id) {
      taskCountMap[t.project_id] = (taskCountMap[t.project_id] || 0) + 1;
    }
  });

  const projects: Project[] = projectsData.map((p) => ({
    id: p.id,
    name: p.name,
    status: p.status,
    context: p.context,
    description: p.description,
    created_at: p.created_at,
    org_tag: p.org_tag,
    is_active: p.is_active,
    parent_project_id: p.parent_project_id,
    parent_project_name: p.parent_project_id ? parentNames[p.parent_project_id] ?? null : null,
    keywords: p.keywords ?? [],
    open_task_count: taskCountMap[p.id] || 0,
  }));

  const stats = computeProjectStats(projectsData, taskCountsData);

  return <ProjectsShell initialProjects={projects} initialStats={stats} />;
}