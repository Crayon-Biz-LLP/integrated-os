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

    const now = new Date().toISOString();

    // Update verified_at timestamp
    const { error: updateError } = await supabase
      .from("decisions")
      .update({ verified_at: now })
      .eq("id", decisionId)
      .eq("auto_decided", true)
      .is("verified_at", null);

    if (updateError) {
      console.error("Supabase error verifying auto-decision:", updateError);
      return NextResponse.json({ error: updateError.message }, { status: 500 });
    }

    // Emit a 'confirmed' observation to train Rhodey positively
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
      event_type: "user_verification",
      features: {
        decision_type: decision.decision_type,
        auto_decided: true,
        verified: true,
      },
      predicted: decision.decision_type,
      actual: decision.decision_type,
      outcome: "confirmed",
      source: "auto_decisions_ui",
    };

    try {
      await supabase.from("subsystem_telemetry").insert([observation]);
    } catch (obsErr) {
      console.error("Failed to emit observation (non-critical):", obsErr);
    }

    return NextResponse.json({ success: true, verified_at: now });
  } catch (err: any) {
    console.error("Unexpected error in verify route:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
