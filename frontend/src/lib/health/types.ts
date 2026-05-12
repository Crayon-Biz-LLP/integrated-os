export interface HealthStats {
  rawDumps: Record<string, number>;
  failedQueue: {
    total: number;
    unresolved: number;
    recentItems: FailedQueueItem[];
  };
  auditLogs: {
    recentErrors: AuditLogEntry[];
  };
  memories: {
    total: number;
    recentWeek: number;
  };
  tasks: {
    open: number;
    closed: number;
  };
}

export interface FailedQueueItem {
  id: number;
  source_table: string;
  operation: string;
  error_message: string;
  retry_count: number;
  created_at: string;
}

export interface AuditLogEntry {
  created_at: string;
  service: string;
  level: string;
  message: string;
}

export interface HealthStatsResponse {
  stats: HealthStats;
  error?: string;
}
