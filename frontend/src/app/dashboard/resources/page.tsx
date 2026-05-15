import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Resource, ResourceMission, ResourceStats } from "@/lib/resources/types";
import { ResourcesShell } from "./resources-shell";

export const dynamic = 'force-dynamic';

function computeResourceStats(resources: Array<{ id: number; mission_id: number | null; created_at: string | null }>): ResourceStats {
  const now = new Date();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);

  const totalResources = resources.length;
  const resourcesWithMissions = resources.filter((r) => r.mission_id !== null);
  const activeMissionsWithResources = new Set(resourcesWithMissions.map((r) => r.mission_id)).size;
  const unmappedResources = resources.filter((r) => r.mission_id === null).length;
  const recentResources = resources.filter((r) => {
    if (!r.created_at) return false;
    return new Date(r.created_at) >= thirtyDaysAgo;
  }).length;

  return { totalResources, activeMissionsWithResources, unmappedResources, recentResources };
}

export default async function ResourcesPage() {
  const supabase = await createServerSupabaseClient();

  const [resourcesRes, statsRes, missionsRes] = await Promise.all([
    supabase
      .from("resources")
      .select(`
        id, url, title, summary, strategic_note, category,
        mission_id, created_at, enriched_at,
        missions!mission_id(id, title, status, description)
      `)
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("resources")
      .select("id, mission_id, created_at")
      .limit(500),
    supabase
      .from("missions")
      .select("id, title, description, status")
      .eq("status", "active")
      .order("title", { ascending: true })
      .limit(100),
  ]);

  const resources: Resource[] = ((resourcesRes.data ?? []) as any[]).map((r: any) => {
    const missionData = Array.isArray(r.missions) ? r.missions[0] : r.missions;
    const hostname = r.url
      ? (() => { try { return new URL(r.url).hostname.replace(/^www\./, ''); } catch { return null; } })()
      : null;
    return {
      id: r.id,
      url: r.url,
      title: r.title,
      summary: r.summary,
      strategic_note: r.strategic_note,
      category: r.category,
      mission_id: r.mission_id,
      created_at: r.created_at,
      enriched_at: r.enriched_at,
      hostname,
      mission_title: missionData?.title ?? null,
      mission_status: missionData?.status ?? null,
      mission_description: missionData?.description ?? null,
    };
  });

  const stats = computeResourceStats(statsRes.data ?? []);

  const missions: ResourceMission[] = ((missionsRes.data ?? []) as any[]).map((m: any) => {
    const resourceCount = resources.filter((r) => r.mission_id === m.id).length;
    return {
      id: m.id,
      title: m.title,
      description: m.description,
      status: m.status,
      resource_count: resourceCount,
    };
  });

  return <ResourcesShell initialResources={resources} initialMissions={missions} initialStats={stats} />;
}
