import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Cluster, ClusterStats } from "@/lib/clusters/types";
import { ClustersShell } from "./clusters-shell";

export const dynamic = 'force-dynamic';

export default async function ClustersPage() {
  const supabase = await createServerSupabaseClient();

  const [clustersRes] = await Promise.all([
    supabase
      .from("clusters")
      .select("*")
      .order("created_at", { ascending: false })
      .limit(100),
  ]);

  const clusters = (clustersRes.data ?? []) as unknown as Cluster[];

  const stats: ClusterStats = {
    total: clusters.length,
    active: clusters.filter((m) => m.status === "active").length,
    completed: clusters.filter((m) => m.status === "completed").length,
    archived: clusters.filter((m) => m.status === "archived").length,
  };

  return <ClustersShell initialClusters={clusters} initialStats={stats} />;
}
