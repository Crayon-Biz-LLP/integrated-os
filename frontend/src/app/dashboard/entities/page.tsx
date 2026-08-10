import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { GraphPendingNode } from "@/lib/decisions/types";
import { EntityTableList } from "@/components/decisions/entity-table-list";

export const dynamic = 'force-dynamic';

/**
 * Entities — the knowledge-graph directory.
 *
 * Moved out of the Decisions tab so Decisions stays purely a *pending* review
 * surface. This page always opens on the LIVE scope: the fully approved
 * entities currently in the graph (people, organizations, concepts, ...).
 *
 * Live nodes are fetched client-side by EntityTableList; rejected nodes are
 * preloaded here so the Rejected scope (un-reject) works too.
 */
export default async function EntitiesPage() {
  const supabase = await createServerSupabaseClient();

  const [rejectedRes] = await Promise.all([
    supabase
      .from("pending_nodes")
      .select("*")
      .eq("status", "rejected")
      .order("created_at", { ascending: false })
      .limit(100),
  ]);

  const rejectedNodes = ((rejectedRes.data ?? []))
    .map((n) => ({ ...n, type: n.node_type })) as GraphPendingNode[];

  return (
    <div className="p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Entities</h1>
      <p className="text-sm text-muted-foreground/70 mt-0.5">
        The live knowledge graph — people, organizations, and concepts Rhodey knows about.
        Manage approved nodes here (rename, merge, delete). New candidates are approved in Decisions.
      </p>
      <div className="mt-6">
        <EntityTableList items={[]} rejectedItems={rejectedNodes} defaultScope="live" showPendingScope={false} />
      </div>
    </div>
  );
}
