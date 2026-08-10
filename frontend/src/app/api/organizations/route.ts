import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET(_req: NextRequest) {
  const supabase = await createServerSupabaseClient();
  const isOrgRoutingEnabled = true;

  if (!isOrgRoutingEnabled) {
    return NextResponse.json([]);
  }

  // Consolidation (migration 75): organizations live as graph nodes; the node
  // UUID is the org id. Enrichment (is_active, org_type, description, parent)
  // lives on node metadata.enrichment.
  const { data: nodes, error: nodesError } = await supabase
    .from("graph_nodes")
    .select("id, label, metadata, db_record_id, created_at")
    .eq("type", "organization")
    .eq("is_current", true)
    .limit(500);

  if (nodesError) {
    return NextResponse.json({ error: nodesError.message }, { status: 500 });
  }

  const orgs = (nodes || []).map((n) => {
    const meta = n.metadata || {};
    const enrich = meta.enrichment || {};
    return {
      id: n.id,
      name: n.label,
      is_active: enrich.is_active ?? true,
      org_type: enrich.org_type ?? null,
      description: enrich.description ?? null,
      parent_organization_id: enrich.parent_organization_id ?? null,
      created_at: n.created_at,
    };
  });

  orgs.sort((a, b) => a.name.localeCompare(b.name));

  return NextResponse.json(orgs);
}
