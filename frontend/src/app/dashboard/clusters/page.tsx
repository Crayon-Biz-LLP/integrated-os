import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Resource, ResourceCluster } from "@/lib/resources/types";
import { ClustersShell } from "./clusters-shell";

export const dynamic = 'force-dynamic';

export default async function ClustersPage() {
  const supabase = await createServerSupabaseClient();

  const [clustersRes, resourcesRes] = await Promise.all([
    supabase
      .from("clusters")
      .select("id, title, description, status")
      .order("title", { ascending: true })
      .limit(100),
    supabase
      .from("resources")
      .select(`
        id, url, title, summary, strategic_note, category,
        cluster_id, created_at, enriched_at,
        clusters!cluster_id(id, title, status, description)
      `)
      .order("created_at", { ascending: false })
      .limit(500),
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

  return <ClustersShell initialResources={resources} initialClusters={clusters} />;
}