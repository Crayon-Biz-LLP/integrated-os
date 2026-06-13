import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { CallPendingItem, WhatsAppPendingMessage, GraphPendingEdge, GraphMergeProposal } from "@/lib/decisions/types";
import { DecisionsShell } from "./decisions-shell";

export const dynamic = 'force-dynamic';

export default async function DecisionsPage() {
  const supabase = await createServerSupabaseClient();

  const [callRes, whatsappRes, graphRes, mergeRes] = await Promise.all([
    supabase
      .from("messages")
      .select("*")
      .eq("channel", "call")
      .is("danny_decision", null)
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("messages")
      .select("*")
      .eq("channel", "whatsapp")
      .is("danny_decision", null)
      .eq("classification", "actionable")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("pending_graph_edges")
      .select("*")
      .eq("status", "pending")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("pending_graph_nodes")
      .select("*")
      .eq("status", "merge_proposed")
      .order("created_at", { ascending: false })
      .limit(50),
  ]);

  const rawCallItems = callRes.data ?? [];
  const callItems = rawCallItems.map((row: any) => ({
    ...row,
    action_type: row.metadata?.action_type ?? 'task',
    people_mentioned: row.metadata?.people_mentioned ?? '[]',
  })) as unknown as CallPendingItem[];

  const rawWhatsappItems = whatsappRes.data ?? [];
  const whatsappItems = rawWhatsappItems.map((row: any) => ({
    ...row,
    sender_phone: row.sender_id,
    message_text: row.body,
    linked_person_name: row.metadata?.linked_person_name,
  })) as unknown as WhatsAppPendingMessage[];

  const graphItems = (graphRes.data ?? []) as GraphPendingEdge[];
  const mergeProposals = (mergeRes.data ?? []) as GraphMergeProposal[];

  const memIds = [...new Set(
    graphItems
      .map(i => i.source_text?.match(/^memories:(\d+)$/))
      .filter(Boolean)
      .map(m => parseInt(m![1]))
  )];
  if (memIds.length > 0) {
    const memRes = await supabase.from("memories").select("id, content").in("id", memIds);
    if (memRes.error) console.error('Failed to resolve memory sources:', memRes.error);
    const memMap = new Map((memRes.data ?? []).map(m => [m.id, m.content]));
    for (const item of graphItems) {
      const match = item.source_text?.match(/^memories:(\d+)$/);
      if (match) {
        const content = memMap.get(parseInt(match[1]));
        if (content) item.source_text = content;
      }
    }
  }

  return (
    <DecisionsShell
      initialCallItems={callItems}
      initialWhatsappItems={whatsappItems}
      initialGraphItems={graphItems}
      initialMergeProposals={mergeProposals}
    />
  );
}
