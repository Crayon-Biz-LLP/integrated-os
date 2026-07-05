import { createServerSupabaseClient } from "@/lib/supabase-server";
import { TelemetryShell } from "./telemetry-shell";
import type { TelemetryPattern } from '@/lib/telemetry/types';

export const dynamic = 'force-dynamic';

export default async function TelemetryPage() {
  const supabase = await createServerSupabaseClient();

  const [patternRes, activityRes, countRes] = await Promise.all([
    supabase
      .from('subsystem_patterns')
      .select('subsystem, feature_json, total_count, correct_count, confidence, last_seen')
      .gte('total_count', 3)
      .order('confidence', { ascending: false })
      .limit(20),
    supabase
      .from('subsystem_telemetry')
      .select('*')
      .gte('created_at', new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString())
      .order('created_at', { ascending: false })
      .limit(50),
    supabase
      .from('subsystem_telemetry')
      .select('subsystem'),
  ]);

  // Build patterns
  const patterns = (patternRes.data || []).map((row: any) => {
    const total = row.total_count;
    const correct = row.correct_count;
    const conf = total > 0 ? correct / total : 0;
    let recommendation: TelemetryPattern['recommendation'] = 'review';
    if (conf >= 0.90 && total >= 10) {
      recommendation = correct > total / 2 ? 'auto_approve' : 'auto_reject';
    } else if (conf >= 0.70) {
      recommendation = 'suggest';
    }
    const features = row.feature_json || {};
    const featureParts = Object.entries(features).filter(([_, v]) => v).map(([k, v]) => `${k}=${v}`);
    const rule = `${featureParts.join(', ')}: ${correct}/${total} (${(conf * 100).toFixed(0)}%)`;
    return {
      subsystem: row.subsystem,
      features,
      total_count: total,
      correct_count: correct,
      confidence: conf,
      recommendation,
      rule,
    };
  });

  // Build recent activity
  const recentActivity = (activityRes.data || []).map((row: any) => ({
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

  // Build subsystem counts
  const subsystemCounts: Record<string, number> = {};
  for (const row of countRes.data || []) {
    subsystemCounts[row.subsystem] = (subsystemCounts[row.subsystem] || 0) + 1;
  }

  return (
    <TelemetryShell
      initialPatterns={patterns}
      initialActivity={recentActivity}
      initialSubsystemCounts={subsystemCounts}
      initialTotalObservations={(countRes.data || []).length}
    />
  );
}
