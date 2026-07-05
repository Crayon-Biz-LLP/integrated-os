import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }

  try {
    const supabase = await createServerSupabaseClient();

    // Fetch all patterns with ≥2 observations
    const { data: patternRows, error: patErr } = await supabase
      .from('subsystem_patterns')
      .select('*')
      .gte('total_count', 2)
      .order('confidence', { ascending: false })
      .limit(100);

    if (patErr) {
      return NextResponse.json({ error: patErr.message }, { status: 500 });
    }

    const patterns = (patternRows || []).map((row: any) => {
      const total = row.total_count || 0;
      const correct = row.correct_count || 0;
      const conf = total > 0 ? correct / total : 0;

      let recommendation: string = 'review';
      let health: string = 'learning';
      if (total >= 10 && conf >= 0.90) {
        recommendation = 'auto_approve';
        health = 'trusted';
      } else if (total >= 5) {
        recommendation = conf >= 0.70 ? 'suggest' : 'review';
        health = conf >= 0.70 ? 'reliable' : 'unreliable';
      } else {
        recommendation = 'review';
        health = 'learning';
      }

      const correctedCount = row.corrected_count || 0;
      const errorRate = total > 0 ? correctedCount / total : 0;
      if (errorRate > 0.5) {
        health = 'demoted';
      }

      // Decay status — unreinforced >30d patterns lose confidence
      let decayStatus: string = 'active';
      if (row.last_seen) {
        const daysSinceLastSeen = (Date.now() - new Date(row.last_seen).getTime()) / (1000 * 60 * 60 * 24);
        if (daysSinceLastSeen > 60) decayStatus = 'stale';
        else if (daysSinceLastSeen > 30) decayStatus = 'decaying';
      }

      return {
        id: row.id,
        subsystem: row.subsystem,
        feature_hash: row.feature_hash,
        features: row.feature_json || {},
        total_count: total,
        correct_count: correct,
        corrected_count: correctedCount,
        confidence: Math.round(conf * 100),
        recommendation,
        health,
        decay_status: decayStatus,
        last_seen: row.last_seen,
        first_seen: row.first_seen || null,
      };
    });

    // Compute subsystem-level rollups
    const subsystemRollups: Record<string, { total: number; avg_confidence: number; trusted: number; learning: number; demoted: number }> = {};
    for (const p of patterns) {
      if (!subsystemRollups[p.subsystem]) {
        subsystemRollups[p.subsystem] = { total: 0, avg_confidence: 0, trusted: 0, learning: 0, demoted: 0 };
      }
      const rollup = subsystemRollups[p.subsystem];
      rollup.total += 1;
      rollup.avg_confidence += p.confidence;
      if (p.health === 'trusted' || p.health === 'reliable') rollup.trusted += 1;
      if (p.health === 'learning') rollup.learning += 1;
      if (p.health === 'demoted') rollup.demoted += 1;
    }
    for (const key of Object.keys(subsystemRollups)) {
      const r = subsystemRollups[key];
      r.avg_confidence = Math.round(r.avg_confidence / r.total);
    }

    return NextResponse.json({
      patterns,
      subsystem_rollups: subsystemRollups,
      total_patterns: patterns.length,
    });
  } catch (err: any) {
    console.error("Patterns API error:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
