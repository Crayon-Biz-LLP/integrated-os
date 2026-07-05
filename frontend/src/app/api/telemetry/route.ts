import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

export async function GET() {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
    const supabase = await createServerSupabaseClient();

    // Fetch top patterns across all subsystems (including feature_hash for drift matching)
    const { data: patternRows } = await supabase
      .from('subsystem_patterns')
      .select('subsystem, feature_json, feature_hash, total_count, correct_count, confidence, last_seen')
      .gte('total_count', 3)
      .order('confidence', { ascending: false })
      .limit(20);

    const patterns = (patternRows || []).map((row: any) => {
      const total = row.total_count;
      const correct = row.correct_count;
      const conf = total > 0 ? correct / total : 0;
      let recommendation: string = 'review';
      if (conf >= 0.90 && total >= 10) {
        recommendation = correct > total / 2 ? 'auto_approve' : 'auto_reject';
      } else if (conf >= 0.70) {
        recommendation = 'suggest';
      }

      const features = row.feature_json || {};
      const featureParts = Object.entries(features)
        .filter(([_, v]) => v)
        .map(([k, v]) => `${k}=${v}`);
      const rule = `${featureParts.join(', ')}: ${correct}/${total} (${(conf * 100).toFixed(0)}%)`;

      return {
        subsystem: row.subsystem,
        feature_hash: row.feature_hash,
        features,
        total_count: total,
        correct_count: correct,
        confidence: conf,
        recommendation,
        rule,
      };
    });

    // Fetch recent auto-approve activity (last 7 days)
    const sevenDaysAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
    const { data: activityRows } = await supabase
      .from('subsystem_telemetry')
      .select('*')
      .gte('created_at', sevenDaysAgo)
      .order('created_at', { ascending: false })
      .limit(50);

    const recentActivity = (activityRows || []).map((row: any) => ({
      id: row.id,
      subsystem: row.subsystem,
      event_type: row.event_type,
      features: row.features,
      predicted: row.predicted,
      actual: row.actual,
      outcome: row.outcome,
      confidence: row.confidence,
      source: row.source,
      created_at: row.created_at,
    }));

    // Subsystem observation counts
    const { data: countRows } = await supabase
      .from('subsystem_telemetry')
      .select('subsystem');
    const subsystemCounts: Record<string, number> = {};
    for (const row of countRows || []) {
      subsystemCounts[row.subsystem] = (subsystemCounts[row.subsystem] || 0) + 1;
    }

    // Compute drift by comparing current pattern confidence to stored baselines
    // Uses feature_hash column (Python MD5) directly — no JS re-hashing needed
    const drift: Array<{ subsystem: string; signal: string; delta: number }> = [];
    const { data: baselineRows } = await supabase
      .from('core_config')
      .select('key, content')
      .ilike('key', 'pattern_baseline:%');
    if (baselineRows) {
      for (const row of baselineRows) {
        const subsystem = row.key.replace('pattern_baseline:', '');
        try {
          const baseline = JSON.parse(row.content || '{}');
          for (const pattern of patterns) {
            if (pattern.subsystem !== subsystem) continue;
            // Match by stored feature_hash — same MD5 hash used by Python
            const fhash = pattern.feature_hash;
            if (!fhash) continue;
            const prev = baseline[fhash];
            if (prev) {
              const prevConf = prev.confidence || 0;
              const currConf = pattern.confidence;
              const delta = currConf - prevConf;
              if (Math.abs(delta) > 0.20) {
                drift.push({
                  subsystem,
                  signal: `${pattern.rule} (was ${(prevConf * 100).toFixed(0)}% last week)`,
                  delta: Math.round(delta * 100),
                });
              }
            }
          }
        } catch {
          // skip malformed baselines
        }
      }
    }

    const stats = {
      patterns,
      recent_activity: recentActivity,
      drift,
      subsystem_counts: subsystemCounts,
      total_observations: (countRows || []).length,
    };

    return NextResponse.json({ stats });
  } catch (err: any) {
    console.error("Telemetry API error:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
