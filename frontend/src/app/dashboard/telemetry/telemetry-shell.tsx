'use client';

import { useMemo } from 'react';
import useSWR from 'swr';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Activity,
  BarChart3,
  BrainCircuit,
  CheckCircle2,
  Cpu,
  Eye,
  TrendingUp,
  Zap,
} from 'lucide-react';
import { TrendingUpDown, AlertTriangle, ArrowUp, ArrowDown } from 'lucide-react';
import { fetcher } from '@/lib/fetcher';
import type { TelemetryPattern, TelemetryObservation, TelemetryDrift, TelemetryResponse } from '@/lib/telemetry/types';

interface TelemetryShellProps {
  initialPatterns: TelemetryPattern[];
  initialActivity: TelemetryObservation[];
  initialSubsystemCounts: Record<string, number>;
  initialTotalObservations: number;
}

function confidenceColor(conf: number): string {
  if (conf >= 0.9) return 'bg-emerald-500';
  if (conf >= 0.7) return 'bg-amber-500';
  if (conf >= 0.4) return 'bg-orange-500';
  return 'bg-red-500';
}

function confidenceTextColor(conf: number): string {
  if (conf >= 0.9) return 'text-emerald-500';
  if (conf >= 0.7) return 'text-amber-500';
  if (conf >= 0.4) return 'text-orange-500';
  return 'text-red-500';
}

function recommendationBadgeVariant(rec: string): 'default' | 'secondary' | 'outline' | 'ghost' | 'actionable' | 'fyi' | 'ignored' | 'pending' | 'blocked' | 'done' {
  switch (rec) {
    case 'auto_approve': return 'actionable';
    case 'auto_reject': return 'blocked';
    case 'suggest': return 'fyi';
    default: return 'ignored';
  }
}

function recommendationLabel(rec: string): string {
  switch (rec) {
    case 'auto_approve': return 'Auto-Approve';
    case 'auto_reject': return 'Auto-Reject';
    case 'suggest': return 'Suggest';
    default: return 'Review';
  }
}

function outcomeBadgeVariant(outcome: string): 'default' | 'secondary' | 'outline' | 'ghost' | 'actionable' | 'fyi' | 'ignored' | 'pending' | 'blocked' | 'done' {
  switch (outcome) {
    case 'correct': return 'actionable';
    case 'confirmed': return 'done';
    case 'corrected': return 'fyi';
    case 'rejected': return 'blocked';
    case 'failed': return 'blocked';
    default: return 'ignored';
  }
}

export function TelemetryShell({
  initialPatterns,
  initialActivity,
  initialSubsystemCounts,
  initialTotalObservations,
}: TelemetryShellProps) {
  // SWR auto-refresh — polls API route every 30s, uses SSR data as fallback
  const { data: liveData, error } = useSWR<TelemetryResponse>(
    '/api/telemetry',
    fetcher,
    {
      refreshInterval: 30_000,
      fallbackData: {
        stats: {
          patterns: initialPatterns,
          recent_activity: initialActivity,
          drift: [],
          subsystem_counts: initialSubsystemCounts,
          total_observations: initialTotalObservations,
        },
      },
    }
  );

  const stats = liveData?.stats;
  const patterns = stats?.patterns ?? initialPatterns;
  const activity = stats?.recent_activity ?? initialActivity;
  const subsystemCounts = stats?.subsystem_counts ?? initialSubsystemCounts;
  const totalObservations = stats?.total_observations ?? initialTotalObservations;

  const drift = stats?.drift ?? [];

  const autoApprovePatterns = useMemo(
    () => patterns.filter((p) => p.recommendation === 'auto_approve'),
    [patterns]
  );

  // Decision-pulse specific data
  const decisionPulsePatterns = useMemo(
    () => patterns.filter((p) => p.subsystem === 'decision_pulse'),
    [patterns]
  );
  const decisionPulseActivity = useMemo(
    () => activity.filter((o) => o.subsystem === 'decision_pulse'),
    [activity]
  );
  const pulseApproved = useMemo(
    () => decisionPulseActivity.filter((o) => o.outcome === 'correct' || o.outcome === 'confirmed').length,
    [decisionPulseActivity]
  );
  const pulseRejected = useMemo(
    () => decisionPulseActivity.filter((o) => o.outcome === 'rejected' || o.outcome === 'corrected').length,
    [decisionPulseActivity]
  );

  const subsystemEntries = useMemo(
    () =>
      Object.entries(subsystemCounts)
        .sort(([, a], [, b]) => b - a),
    [subsystemCounts]
  );

  const totalPatterns = patterns.length;

  const statCards = [
    {
      label: 'Total Observations',
      value: totalObservations.toLocaleString(),
      icon: BarChart3,
      color: 'text-blue-500',
      bg: 'bg-blue-500/10',
    },
    {
      label: 'Learned Patterns',
      value: totalPatterns,
      icon: BrainCircuit,
      color: 'text-violet-500',
      bg: 'bg-violet-500/10',
    },
    {
      label: 'Auto-Approve Rules',
      value: autoApprovePatterns.length,
      icon: Zap,
      color: 'text-emerald-500',
      bg: 'bg-emerald-500/10',
    },
    {
      label: 'Subsystems Tracked',
      value: subsystemEntries.length,
      icon: Cpu,
      color: 'text-amber-500',
      bg: 'bg-amber-500/10',
    },
  ];

  return (
    <div className="flex-1 space-y-6 p-4 md:p-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Telemetry</h1>
          <p className="text-sm text-muted-foreground/70 mt-0.5">
            Pattern learning, auto-approve activity, and subsystem insights
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Eye className="h-4 w-4 text-muted-foreground/40" />
          <span className="text-xs text-muted-foreground/40">
            {error ? 'Offline' : 'Live — auto-refreshes'}
          </span>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {statCards.map((card) => {
          const Icon = card.icon;
          return (
            <Card key={card.label}>
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">
                  {card.label}
                </CardTitle>
                <div className={`rounded-lg p-2 ${card.bg}`}>
                  <Icon className={`h-4 w-4 ${card.color}`} />
                </div>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{card.value}</div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Main Grid: Patterns + Activity */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Top Patterns */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <TrendingUp className="h-4 w-4 text-primary" />
              Top Patterns
            </CardTitle>
          </CardHeader>
          <CardContent>
            {totalPatterns === 0 ? (
              <p className="text-sm text-muted-foreground/60 py-8 text-center">
                No patterns learned yet. Patterns appear after subsystems have
                enough observations.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Subsystem</TableHead>
                    <TableHead>Rule</TableHead>
                    <TableHead>Confidence</TableHead>
                    <TableHead>Status</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {patterns.slice(0, 10).map((pattern, i) => (
                    <TableRow key={`${pattern.subsystem}-${i}`}>
                      <TableCell className="font-medium text-xs">
                        {pattern.subsystem}
                      </TableCell>
                      <TableCell className="text-xs max-w-[200px] truncate font-mono">
                        {pattern.rule}
                      </TableCell>
                      <TableCell className="w-32">
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-16 rounded-full bg-muted overflow-hidden">
                            <div
                              className={`h-full rounded-full transition-all ${confidenceColor(pattern.confidence)}`}
                              style={{ width: `${Math.round(pattern.confidence * 100)}%` }}
                            />
                          </div>
                          <span className={`text-xs font-medium tabular-nums ${confidenceTextColor(pattern.confidence)}`}>
                            {Math.round(pattern.confidence * 100)}%
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={recommendationBadgeVariant(pattern.recommendation)}>
                          {recommendationLabel(pattern.recommendation)}
                        </Badge>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* Recent Activity */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Eye className="h-4 w-4 text-primary" />
              Recent Activity
            </CardTitle>
          </CardHeader>
          <CardContent>
            {activity.length === 0 ? (
              <p className="text-sm text-muted-foreground/60 py-8 text-center">
                No recent telemetry activity in the last 7 days.
              </p>
            ) : (
              <div className="space-y-2 max-h-[400px] overflow-y-auto">
                {activity.slice(0, 20).map((obs) => (
                  <div
                    key={obs.id}
                    className="flex items-center justify-between rounded-lg border p-2.5 text-xs transition-colors hover:bg-muted/30"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <Badge variant={outcomeBadgeVariant(obs.outcome)} className="shrink-0">
                        {obs.outcome}
                      </Badge>
                      <span className="font-medium truncate">{obs.subsystem}</span>
                      <span className="text-muted-foreground/60 truncate hidden sm:inline">
                        {obs.event_type}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 shrink-0 ml-2">
                      {obs.confidence != null && (
                        <span className={`tabular-nums font-medium ${confidenceTextColor(obs.confidence)}`}>
                          {Math.round(obs.confidence * 100)}%
                        </span>
                      )}
                      <span className="text-muted-foreground/40 tabular-nums">
                        {new Date(obs.created_at).toLocaleDateString(undefined, {
                          month: 'short',
                          day: 'numeric',
                          hour: '2-digit',
                          minute: '2-digit',
                        })}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Drift Section */}
      {drift.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <TrendingUpDown className="h-4 w-4 text-orange-500" />
              Decision Pattern Drift
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground/60 mb-3">
                Patterns whose confidence changed significantly from last week's
                baseline. Large swings may indicate changing user behavior or
                new edge cases.
              </p>
              <div className="grid gap-3 md:grid-cols-2">
                {drift.map((d, i) => (
                  <div
                    key={`drift-${i}`}
                    className="flex items-start gap-3 rounded-lg border p-3"
                  >
                    <div
                      className={`mt-0.5 rounded-full p-1 ${
                        d.delta > 0
                          ? 'bg-emerald-500/10 text-emerald-500'
                          : 'bg-red-500/10 text-red-500'
                      }`}
                    >
                      {d.delta > 0 ? (
                        <ArrowUp className="h-3 w-3" />
                      ) : (
                        <ArrowDown className="h-3 w-3" />
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-medium">
                          {d.subsystem}
                        </span>
                        <span
                          className={`text-xs font-bold tabular-nums ${
                            d.delta > 0
                              ? 'text-emerald-500'
                              : 'text-red-500'
                          }`}
                        >
                          {d.delta > 0 ? '+' : ''}
                          {d.delta}%
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground/70 truncate">
                        {d.signal}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Middle: Decision Pulse History */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-lg">
            <Activity className="h-4 w-4 text-amber-500" />
            Decision Pulse History
          </CardTitle>
        </CardHeader>
        <CardContent>
          {decisionPulsePatterns.length === 0 && decisionPulseActivity.length === 0 ? (
            <p className="text-sm text-muted-foreground/60 py-8 text-center">
              No decision pulse activity yet. Pulse runs every 30 minutes via
              cron-job.org — pattern-confidence auto-approve items when confident.
            </p>
          ) : (
            <div className="grid gap-4 lg:grid-cols-3">
              {/* Summary mini-cards */}
              <div className="rounded-lg border p-3 space-y-1">
                <p className="text-xs text-muted-foreground/60">Patterns</p>
                <p className="text-2xl font-bold">{decisionPulsePatterns.length}</p>
                <p className="text-xs text-muted-foreground/40">
                  across entity_extraction + decision_pulse
                </p>
              </div>
              <div className="rounded-lg border p-3 space-y-1">
                <p className="text-xs text-muted-foreground/60">Auto-Approved</p>
                <p className="text-2xl font-bold text-emerald-500">{pulseApproved}</p>
                <p className="text-xs text-muted-foreground/40">
                  items approved by pattern confidence
                </p>
              </div>
              <div className="rounded-lg border p-3 space-y-1">
                <p className="text-xs text-muted-foreground/60">Rejected / Corrected</p>
                <p className="text-2xl font-bold text-red-500">{pulseRejected}</p>
                <p className="text-xs text-muted-foreground/40">
                  items user corrected or rejected
                </p>
              </div>

              {/* Pulse Patterns Table */}
              {decisionPulsePatterns.length > 0 && (
                <div className="lg:col-span-3">
                  <Separator className="my-2" />
                  <p className="text-xs font-medium text-muted-foreground/60 mb-2">
                    Decision Pulse Patterns
                  </p>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Subsystem</TableHead>
                        <TableHead>Rule</TableHead>
                        <TableHead>Confidence</TableHead>
                        <TableHead>Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {decisionPulsePatterns.map((pattern, i) => (
                        <TableRow key={`dp-${i}`}>
                          <TableCell className="font-medium text-xs">
                            {pattern.subsystem}
                          </TableCell>
                          <TableCell className="text-xs max-w-[220px] truncate font-mono">
                            {pattern.rule}
                          </TableCell>
                          <TableCell className="tabular-nums text-xs font-medium">
                            {Math.round(pattern.confidence * 100)}%
                          </TableCell>
                          <TableCell>
                            <Badge variant={recommendationBadgeVariant(pattern.recommendation)}>
                              {recommendationLabel(pattern.recommendation)}
                            </Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}

              {/* Recent Pulse Activity */}
              {decisionPulseActivity.length > 0 && (
                <div className="lg:col-span-3">
                  <Separator className="my-2" />
                  <p className="text-xs font-medium text-muted-foreground/60 mb-2">
                    Recent Pulse Activity
                  </p>
                  <div className="space-y-1.5 max-h-[240px] overflow-y-auto">
                    {decisionPulseActivity.slice(0, 15).map((obs) => (
                      <div
                        key={obs.id}
                        className="flex items-center justify-between rounded-lg border p-2 text-xs transition-colors hover:bg-muted/30"
                      >
                        <div className="flex items-center gap-2 min-w-0">
                          <Badge variant={outcomeBadgeVariant(obs.outcome)} className="shrink-0">
                            {obs.outcome}
                          </Badge>
                          <span className="text-muted-foreground/70 truncate">
                            {obs.event_type}
                          </span>
                        </div>
                        <span className="text-muted-foreground/30 tabular-nums shrink-0 ml-2">
                          {new Date(obs.created_at).toLocaleDateString(undefined, {
                            month: 'short',
                            day: 'numeric',
                            hour: '2-digit',
                            minute: '2-digit',
                          })}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Bottom Grid: Auto-Approve + Subsystem Breakdown */}
      <div className="grid gap-6 lg:grid-cols-2">
        {/* Auto-Approve Rules Detail */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <CheckCircle2 className="h-4 w-4 text-emerald-500" />
              Auto-Approve Rules
            </CardTitle>
          </CardHeader>
          <CardContent>
            {autoApprovePatterns.length === 0 ? (
              <p className="text-sm text-muted-foreground/60 py-8 text-center">
                No auto-approve rules yet. Patterns need ≥10 observations at
                ≥90% confidence to graduate.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Subsystem</TableHead>
                    <TableHead>Rule</TableHead>
                    <TableHead>Count</TableHead>
                    <TableHead>Confidence</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {autoApprovePatterns.map((pattern, i) => (
                    <TableRow key={`auto-${i}`}>
                      <TableCell className="font-medium text-xs">
                        {pattern.subsystem}
                      </TableCell>
                      <TableCell className="text-xs max-w-[220px] truncate font-mono">
                        {pattern.rule}
                      </TableCell>
                      <TableCell className="tabular-nums text-xs">
                        {pattern.correct_count}/{pattern.total_count}
                      </TableCell>
                      <TableCell className="tabular-nums text-xs font-medium text-emerald-500">
                        {Math.round(pattern.confidence * 100)}%
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* Subsystem Breakdown */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Cpu className="h-4 w-4 text-primary" />
              Subsystem Breakdown
            </CardTitle>
          </CardHeader>
          <CardContent>
            {subsystemEntries.length === 0 ? (
              <p className="text-sm text-muted-foreground/60 py-8 text-center">
                No subsystems have reported telemetry yet.
              </p>
            ) : (
              <div className="space-y-3">
                {subsystemEntries.map(([subsystem, count]) => {
                  const maxCount = subsystemEntries[0]?.[1] || 1;
                  const pct = Math.round((count / maxCount) * 100);
                  return (
                    <div key={subsystem} className="space-y-1">
                      <div className="flex items-center justify-between text-xs">
                        <span className="font-medium">{subsystem}</span>
                        <span className="tabular-nums text-muted-foreground">
                          {count.toLocaleString()}
                        </span>
                      </div>
                      <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
                        <div
                          className="h-full rounded-full bg-primary/60 transition-all"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
