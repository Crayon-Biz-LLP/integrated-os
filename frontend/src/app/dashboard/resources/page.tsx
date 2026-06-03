import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Resource, ResourceCluster, ResourceStats } from "@/lib/resources/types";
import { ResourcesShell } from "./resources-shell";

export const dynamic = 'force-dynamic';

function computeResourceStats(resources: Array<{ id: number; cluster_id: number | null; created_at: string | null }>): ResourceStats {
  const now = new Date();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);

  const totalResources = resources.length;
  const resourcesWithClusters = resources.filter((r) => r.cluster_id !== null);
  const activeClustersWithResources = new Set(resourcesWithClusters.map((r) => r.cluster_id)).size;
  const unmappedResources = resources.filter((r) => r.cluster_id === null).length;
  const recentResources = resources.filter((r) => {
    if (!r.created_at) return false;
    return new Date(r.created_at) >= thirtyDaysAgo;
  }).length;

  return { totalResources, activeClustersWithResources, unmappedResources, recentResources };
}

export default async function ResourcesPage() {
  const supabase = await createServerSupabaseClient();

  const [resourcesRes, statsRes, clustersRes] = await Promise.all([
    supabase
      .from("resources")
      .select(`
        id, url, title, summary, strategic_note, category,
        cluster_id, created_at, enriched_at,
        clusters!cluster_id(id, title, status, description)
      `)
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("resources")
      .select("id, cluster_id, created_at")
      .limit(500),
    supabase
      .from("clusters")
      .select("id, title, description, status")
      .eq("status", "active")
      .order("title", { ascending: true })
      .limit(100),
  ]);

  const resources: Resource[] = ((resourcesRes.data ?? []) as any[]).map((r: any) => {
    const clusterData = Array.isArray(r.clusters) ? r.clusters[0] : r.clusters;
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
      cluster_id: r.cluster_id,
      created_at: r.created_at,
      enriched_at: r.enriched_at,
      hostname,
      cluster_title: clusterData?.title ?? null,
      cluster_status: clusterData?.status ?? null,
      cluster_description: clusterData?.description ?? null,
    };
  });

  const stats = computeResourceStats(statsRes.data ?? []);

  const clusters: ResourceCluster[] = ((clustersRes.data ?? []) as any[]).map((m: any) => {
    const resourceCount = resources.filter((r) => r.cluster_id === m.id).length;
    return {
      id: m.id,
      title: m.title,
      description: m.description,
      status: m.status,
      resource_count: resourceCount,
    };
  });

  return <ResourcesShell initialResources={resources} initialClusters={clusters} initialStats={stats} />;
}
