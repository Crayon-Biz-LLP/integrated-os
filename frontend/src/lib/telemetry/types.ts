export interface TelemetryPattern {
  subsystem: string;
  features: Record<string, any>;
  total_count: number;
  correct_count: number;
  confidence: number;
  recommendation: 'auto_approve' | 'auto_reject' | 'suggest' | 'review';
  rule: string;
}

export interface TelemetryObservation {
  id: number;
  subsystem: string;
  event_type: string;
  features: Record<string, any>;
  predicted: any;
  actual: any;
  outcome: string;
  confidence: number | null;
  source: string;
  created_at: string;
}

export interface TelemetryDrift {
  subsystem: string;
  signal: string;
  delta: number;
}

export interface TelemetryStats {
  patterns: TelemetryPattern[];
  recent_activity: TelemetryObservation[];
  drift: TelemetryDrift[];
  subsystem_counts: Record<string, number>;
  total_observations: number;
}

export interface TelemetryResponse {
  stats: TelemetryStats;
  error?: string;
}
