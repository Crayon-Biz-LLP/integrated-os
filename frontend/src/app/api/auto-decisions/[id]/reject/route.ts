import { NextRequest, NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }

  try {
    const { id } = await params;
    const decisionId = Number(id);

    if (isNaN(decisionId)) {
      return NextResponse.json({ error: "Invalid ID" }, { status: 400 });
    }

    const supabase = await createServerSupabaseClient();

    // Fetch the decision to get context for the observation
    const { data: decision, error: fetchError } = await supabase
      .from("decisions")
      .select("*")
      .eq("id", decisionId)
      .eq("auto_decided", true)
      .single();

    if (fetchError || !decision) {
      return NextResponse.json({ error: fetchError?.message || "Decision not found" }, { status: 404 });
    }

    // Mark the decision as reversed
    const { error: updateError } = await supabase
      .from("decisions")
      .update({
        status: "reversed",
        verified_at: new Date().toISOString(),
      })
      .eq("id", decisionId)
      .eq("auto_decided", true)
      .is("verified_at", null);

    if (updateError) {
      console.error("Supabase error rejecting auto-decision:", updateError);
      return NextResponse.json({ error: updateError.message }, { status: 500 });
    }

    // ── Check reversibility before attempting undo ──
    const undoResults: string[] = [];
    const isReversible = decision.reversible !== false;
    
    if (!isReversible) {
      undoResults.push('Decision marked as irreversible — no undo attempted.');
    } else {
      try {
        const entityType = decision.entity_type || '';
        const entityId = decision.entity_id || '';
        const decisionType = decision.decision_type || '';

      // Channel approval: revert the message so it shows as pending again
      if ((decisionType === 'channel_approval' || decisionType.includes('approval')) && entityType === 'message' && entityId) {
        const { data: msg } = await supabase
          .from('messages')
          .select('id, danny_decision')
          .eq('id', Number(entityId))
          .maybeSingle();
        if (msg && msg.danny_decision === 'approved') {
          await supabase
            .from('messages')
            .update({ danny_decision: null, decided_at: null })
            .eq('id', Number(entityId));
          undoResults.push(`Reverted message #${entityId} to pending`);
        }
      }

      // Graph node approval: revert pending_nodes back to pending status
      if (decisionType === 'graph_node_approval' && entityType === 'pending_graph_node' && entityId) {
        const { data: node } = await supabase
          .from('pending_nodes')
          .select('id, status')
          .eq('id', Number(entityId))
          .maybeSingle();
        if (node && ['approved', 'pending'].includes(node.status)) {
          await supabase
            .from('pending_nodes')
            .update({ status: 'pending' })
            .eq('id', Number(entityId));
          undoResults.push(`Reverted graph node #${entityId} to pending`);
        }
      }

      // Graph edge approval: revert pending_graph_edges back to pending status
      if (decisionType === 'graph_edge_approval' && entityType === 'pending_graph_edge' && entityId) {
        const { data: edge } = await supabase
          .from('pending_graph_edges')
          .select('id, status')
          .eq('id', Number(entityId))
          .maybeSingle();
        if (edge && ['approved', 'pending'].includes(edge.status)) {
          await supabase
            .from('pending_graph_edges')
            .update({ status: 'pending' })
            .eq('id', Number(entityId));
          undoResults.push(`Reverted graph edge #${entityId} to pending`);
        }
      }

      // Task auto-expiry: reactivate the task
      if (decisionType === 'task_auto_expiry' && entityType === 'task' && entityId) {
        const { data: task } = await supabase
          .from('tasks')
          .select('id, status')
          .eq('id', Number(entityId))
          .maybeSingle();
        if (task && ['cancelled', 'done'].includes(task.status)) {
          await supabase
            .from('tasks')
            .update({ status: 'todo', completed_at: null })
            .eq('id', Number(entityId));
          undoResults.push(`Reactivated task #${entityId}`);
        }
      }

      // Priority decay: revert to urgent (we can infer urgent was the original)
      if (decisionType === 'priority_decay' && entityType === 'task' && entityId) {
        const { data: task } = await supabase
          .from('tasks')
          .select('id, priority')
          .eq('id', Number(entityId))
          .maybeSingle();
        if (task && task.priority === 'high') {
          await supabase
            .from('tasks')
            .update({ priority: 'urgent' })
            .eq('id', Number(entityId));
          undoResults.push(`Reverted priority for task #${entityId} urgent→high`);
        }
      }

      // Edge auto-creation: revert by setting danny_decision on the message back
      if (decisionType === 'edge_auto_creation' && entityId) {
        // Try interpreting entity_id as a pending_graph_edges ID
        const { data: edge } = await supabase
          .from('pending_graph_edges')
          .select('id, status')
          .eq('id', Number(entityId))
          .maybeSingle();
        if (edge && ['approved', 'pending'].includes(edge.status)) {
          await supabase
            .from('pending_graph_edges')
            .update({ status: 'pending' })
            .eq('id', Number(entityId));
          undoResults.push(`Reverted edge #${entityId} to pending`);
        }
      }
      } catch (undoErr) {
        console.error("Failed to undo action (non-critical):", undoErr);
        undoResults.push(`Undo partially failed: ${(undoErr as Error).message}`);
      }
    }

    // Emit a 'corrected' observation to train Rhodey negatively
    // Map the decision source to the correct pattern subsystem so the
    // correction lands in the right bucket for compute_pattern_confidence()
    const source = decision.source || "";
    const subsystemMap: Record<string, string> = {
      decision_pulse: "entity_extraction",
      pulse_engine: "task_management",
      auto_approve_cascade: "entity_extraction",
    };
    let subsystem = "auto_decisions";
    for (const [key, val] of Object.entries(subsystemMap)) {
      if (source.includes(key)) {
        subsystem = val;
        break;
      }
    }
    // "email_decision_pulse" → "email_pipeline", "call_decision_pulse" → "call_pipeline", etc.
    const channelMatch = source.match(/^(email|call|whatsapp|teams)/);
    if (channelMatch) {
      subsystem = `${channelMatch[1]}_pipeline`;
    }

    const observation = {
      subsystem,
      event_type: "user_correction",
      features: {
        decision_type: decision.decision_type,
        auto_decided: true,
        rejected: true,
      },
      predicted: decision.decision_type,
      actual: "reversed",
      outcome: "corrected",
      source: "auto_decisions_ui",
    };

    try {
      await supabase.from("subsystem_telemetry").insert([observation]);
    } catch (obsErr) {
      console.error("Failed to emit observation (non-critical):", obsErr);
    }

    return NextResponse.json({ success: true, status: "reversed", undo: undoResults });
  } catch (err: any) {
    console.error("Unexpected error in reject route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
