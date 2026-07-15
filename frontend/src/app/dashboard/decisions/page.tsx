import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { CallPendingItem, WhatsAppPendingMessage, GraphPendingEdge, GraphPendingNode, GraphMergeProposal, AutoDecisionItem } from "@/lib/decisions/types";
import { DecisionsShell } from "./decisions-shell";

export const dynamic = 'force-dynamic';

export default async function DecisionsPage() {
  const supabase = await createServerSupabaseClient();

  const [callRes, whatsappRes, graphRes, nodeRes, mergeRes, rejectedRes, autoDecisionsRes] = await Promise.all([
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
      .in("status", ["pending", "awaiting_clarification"])
      .order("created_at", { ascending: false })
      .limit(2000),
    supabase
      .from("pending_nodes")
      .select("*")
      .in("status", ["pending", "flagged", "awaiting_clarification"])
      .order("created_at", { ascending: false })
      .limit(1000),
    supabase
      .from("merge_proposals")
      .select("*")
      .eq("status", "proposed")
      .order("proposed_at", { ascending: false })
      .limit(50),
    supabase
      .from("pending_nodes")
      .select("*")
      .eq("status", "rejected")
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("decisions")
      .select("*")
      .eq("auto_decided", true)
      .is("verified_at", null)
      .order("decided_at", { ascending: false })
      .limit(100),
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
  const graphNodes = (nodeRes.data ?? []) as GraphPendingNode[];
  const rejectedNodes = (rejectedRes.data ?? []) as GraphPendingNode[];
  const mergeProposals = (mergeRes.data ?? []) as GraphMergeProposal[];
  const autoDecisions = (autoDecisionsRes.data ?? []) as AutoDecisionItem[];

  const edgeClarificationIds = graphItems.filter(i => i.status === 'awaiting_clarification').map(i => i.id.toString());
  const nodeClarificationIds = graphNodes.filter(n => n.status === 'awaiting_clarification').map(n => n.id.toString());

  const clarMap = new Map();
  if (edgeClarificationIds.length > 0) {
    const res = await supabase.from('clarification_feedback').select('*').eq('source_table', 'pending_graph_edges').in('source_id', edgeClarificationIds);
    if (res.data) res.data.forEach(c => clarMap.set(`pending_graph_edges:${c.source_id}`, c));
  }
  if (nodeClarificationIds.length > 0) {
    const res = await supabase.from('clarification_feedback').select('*').eq('source_table', 'pending_nodes').in('source_id', nodeClarificationIds);
    if (res.data) res.data.forEach(c => clarMap.set(`pending_nodes:${c.source_id}`, c));
  }

  for (const item of graphItems) {
    if (item.status === 'awaiting_clarification') {
      const clar = clarMap.get(`pending_graph_edges:${item.id}`);
      if (clar) (item as any).clarification = clar;
    }
  }
  for (const node of graphNodes) {
    if (node.status === 'awaiting_clarification') {
      const clar = clarMap.get(`pending_nodes:${node.id}`);
      if (clar) (node as any).clarification = clar;
    }
  }

  const memIds = [...new Set([
    ...graphItems.map(i => i.source_text?.match(/^memories:(\d+)$/)).filter(Boolean).map(m => parseInt(m![1])),
    ...graphNodes.map(n => n.source_text?.match(/^memories:(\d+)$/)).filter(Boolean).map(m => parseInt(m![1])),
    ...rejectedNodes.map(n => n.source_text?.match(/^memories:(\d+)$/)).filter(Boolean).map(m => parseInt(m![1]))
  ])];
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
    for (const node of graphNodes) {
      const match = node.source_text?.match(/^memories:(\d+)$/);
      if (match) {
        const content = memMap.get(parseInt(match[1]));
        if (content) node.source_text = content;
      }
    }
    for (const node of rejectedNodes) {
      const match = node.source_text?.match(/^memories:(\d+)$/);
      if (match) {
        const content = memMap.get(parseInt(match[1]));
        if (content) node.source_text = content;
      }
    }
  }

  const candidateIds = [...new Set(mergeProposals.map(m => m.merge_candidate_id).filter(Boolean))];
  if (candidateIds.length > 0) {
    const candidateRes = await supabase.from("graph_nodes").select("id, label").in("id", candidateIds);
    if (candidateRes.error) console.error('Failed to resolve merge candidate sources:', candidateRes.error);
    const candidateMap = new Map((candidateRes.data ?? []).map(n => [n.id, n.label]));
    for (const proposal of mergeProposals) {
      const label = candidateMap.get(proposal.merge_candidate_id);
      if (label) proposal.merge_candidate_label = label;
    }
  }

  return (
    <DecisionsShell
      initialCallItems={callItems}
      initialWhatsappItems={whatsappItems}
      initialGraphItems={graphItems}
      initialGraphNodes={graphNodes}
      initialMergeProposals={mergeProposals}
      initialRejectedNodes={rejectedNodes}
      initialAutoDecisions={autoDecisions}
    />
  );
}
