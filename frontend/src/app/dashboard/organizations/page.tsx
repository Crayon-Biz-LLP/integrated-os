import { createServerSupabaseClient } from "@/lib/supabase-server";
import { OrganizationsShell } from "./organizations-shell";

export const dynamic = 'force-dynamic';

export interface OrganizationRow {
  id: string;
  name: string;
  is_active: boolean;
  created_at: string | null;
}

export default async function Page() {
  const supabase = await createServerSupabaseClient();

  const [orgsRes, taskCountsRes] = await Promise.all([
    supabase
      .from("organizations")
      .select("id, name, is_active, created_at")
      .order("name", { ascending: true })
      .limit(100),
    supabase
      .from("tasks")
      .select("organization_id")
      .eq("is_current", true)
      .in("status", ["todo", "in_progress", "blocked"])
      .not("organization_id", "is", null)
      .limit(500),
  ]);

  const orgsData = (orgsRes.data ?? []) as OrganizationRow[];
  const taskCountsData = (taskCountsRes.data ?? []) as { organization_id: string | null }[];

  const taskCountMap: Record<string, number> = {};
  taskCountsData.forEach((t) => {
    if (t.organization_id) {
      taskCountMap[t.organization_id] = (taskCountMap[t.organization_id] || 0) + 1;
    }
  });

  const orgs = orgsData.map((org) => ({
    id: org.id,
    name: org.name,
    is_active: org.is_active,
    created_at: org.created_at,
    open_task_count: taskCountMap[org.id] || 0,
  }));

  return <OrganizationsShell initialOrgs={orgs} />;
}