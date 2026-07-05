'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { AlertCircle, CheckCircle, RefreshCw, Clock, TrendingDown, ThumbsUp, ThumbsDown, Brain } from 'lucide-react';
import { formatDistanceToNow, parseISO } from 'date-fns';

interface Pattern {
  id: number;
  subsystem: string;
  feature_hash: string;
  features: Record<string, any>;
  total_count: number;
  correct_count: number;
  corrected_count: number;
  confidence: number;
  recommendation: string;
  health: string;
  decay_status: string;
  last_seen: string;
  first_seen: string | null;
}

interface SubsystemRollup {
  total: number;
  avg_confidence: number;
  trusted: number;
  learning: number;
  demoted: number;
}

function HealthBadge({ health }: { health: string }) {
  const config: Record<string, { label: string; color: string; icon: React.ReactNode }> = {
    trusted: { label: 'Trusted', color: 'bg-green-500/10 text-green-500 border-green-500/30', icon: <CheckCircle className="h-3 w-3" /> },
    reliable: { label: 'Reliable', color: 'bg-blue-500/10 text-blue-500 border-blue-500/30', icon: <ThumbsUp className="h-3 w-3" /> },
    learning: { label: 'Learning', color: 'bg-amber-500/10 text-amber-500 border-amber-500/30', icon: <Brain className="h-3 w-3" /> },
    unreliable: { label: 'Unreliable', color: 'bg-orange-500/10 text-orange-500 border-orange-500/30', icon: <AlertCircle className="h-3 w-3" /> },
    demoted: { label: 'Demoted', color: 'bg-red-500/10 text-red-500 border-red-500/30', icon: <ThumbsDown className="h-3 w-3" /> },
  };
  const c = config[health] || config.learning;
  return (
    <Badge variant="outline" className={`text-xs flex items-center gap-1 ${c.color}`}>
      {c.icon}
      {c.label}
    </Badge>
  );
}

function DecayBadge({ decay }: { decay: string }) {
  const config: Record<string, { label: string; color: string }> = {
    active: { label: 'Active', color: 'bg-emerald-500/10 text-emerald-500' },
    decaying: { label: 'Decaying', color: 'bg-amber-500/10 text-amber-500' },
    stale: { label: 'Stale', color: 'bg-gray-500/10 text-gray-500' },
  };
  const c = config[decay] || config.active;
  return (
    <Badge variant="outline" className={`text-xs ${c.color}`}>
      {decay === 'decaying' && <TrendingDown className="h-3 w-3 mr-1" />}
      {decay === 'stale' && <Clock className="h-3 w-3 mr-1" />}
      {c.label}
    </Badge>
  );
}

function SubsystemLabel({ subsystem }: { subsystem: string }) {
  const displayNames: Record<string, string> = {
    email_pipeline: 'Email',
    call_pipeline: 'Call',
    whatsapp_pipeline: 'WhatsApp',
    teams_pipeline: 'Teams',
    entity_extraction: 'Entity Extraction',
    graph_edges: 'Graph Edges',
    classification: 'Classification',
    channel_decision: 'Channel Decision',
    task_management: 'Task Management',
    graph_edge: 'Graph Edge',
    auto_decisions: 'Auto Decisions',
  };
  return <span className="text-xs font-medium">{displayNames[subsystem] || subsystem}</span>;
}

function FeatureTag({ k, v }: { k: string; v: any }) {
  const label = k.replace(/_/g, ' ');
  return (
    <span className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-0.5 text-xs font-mono">
      <span className="text-muted-foreground">{label}:</span>
      <span className="font-semibold">{String(v).slice(0, 30)}</span>
    </span>
  );
}

export function PatternHealthList({ initialPatterns, initialRollups }: { initialPatterns: Pattern[]; initialRollups: Record<string, SubsystemRollup> }) {
  const [patterns] = useState<Pattern[]>(initialPatterns);
  const [rollups] = useState(initialRollups);
  const [activeTab, setActiveTab] = useState('patterns');

  const subsystems = [...new Set(patterns.map(p => p.subsystem))].sort();

  // Stats
  const trustedCount = patterns.filter(p => p.health === 'trusted' || p.health === 'reliable').length;
  const learningCount = patterns.filter(p => p.health === 'learning').length;
  const demotedCount = patterns.filter(p => p.health === 'demoted').length;
  const decayedCount = patterns.filter(p => p.decay_status === 'decaying' || p.decay_status === 'stale').length;
  const avgConf = patterns.length > 0 ? Math.round(patterns.reduce((s, p) => s + p.confidence, 0) / patterns.length) : 0;

  if (patterns.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        <Brain className="h-8 w-8 mx-auto mb-2 text-muted-foreground/50" />
        <p>No patterns learned yet. They appear after Rhodey has made enough observations to detect decision patterns.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Card>
          <CardContent className="pt-4 text-center">
            <p className="text-2xl font-bold">{patterns.length}</p>
            <p className="text-xs text-muted-foreground">Total Patterns</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 text-center">
            <p className="text-2xl font-bold text-green-500">{trustedCount}</p>
            <p className="text-xs text-muted-foreground">Trusted/Reliable</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 text-center">
            <p className="text-2xl font-bold text-amber-500">{learningCount}</p>
            <p className="text-xs text-muted-foreground">Still Learning</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 text-center">
            <p className="text-2xl font-bold text-red-500">{demotedCount}</p>
            <p className="text-xs text-muted-foreground">Demoted</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4 text-center">
            <p className="text-2xl font-bold text-muted-foreground">{decayedCount}</p>
            <p className="text-xs text-muted-foreground">Decaying/Stale</p>
          </CardContent>
        </Card>
      </div>

      {/* Subsystem Rollups */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {Object.entries(rollups).sort(([, a], [, b]) => b.total - a.total).map(([ss, r]) => (
          <Card key={ss} className="border-l-4 border-l-primary/30">
            <CardContent className="pt-3 pb-3">
              <div className="flex items-center justify-between mb-1">
                <SubsystemLabel subsystem={ss} />
                <span className="text-xs text-muted-foreground">{r.total} patterns</span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span className="text-green-500">{r.trusted} trusted</span>
                <span className="text-amber-500">{r.learning} learning</span>
                {r.demoted > 0 && <span className="text-red-500">{r.demoted} demoted</span>}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Pattern Table */}
      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          <TabsTrigger value="patterns">All Patterns</TabsTrigger>
          <TabsTrigger value="trusted">Trusted ({trustedCount})</TabsTrigger>
          <TabsTrigger value="demoted">Demoted ({demotedCount})</TabsTrigger>
          {subsystems.slice(0, 5).map(ss => (
            <TabsTrigger key={ss} value={ss}>
              <SubsystemLabel subsystem={ss} />
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="patterns" className="mt-4">
          <PatternTable patterns={patterns} />
        </TabsContent>
        <TabsContent value="trusted" className="mt-4">
          <PatternTable patterns={patterns.filter(p => p.health === 'trusted' || p.health === 'reliable')} />
        </TabsContent>
        <TabsContent value="demoted" className="mt-4">
          <PatternTable patterns={patterns.filter(p => p.health === 'demoted')} />
        </TabsContent>
        {subsystems.slice(0, 5).map(ss => (
          <TabsContent key={ss} value={ss} className="mt-4">
            <PatternTable patterns={patterns.filter(p => p.subsystem === ss)} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

function PatternTable({ patterns }: { patterns: Pattern[] }) {
  return (
    <div className="rounded-md border overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b bg-muted/50">
            <th className="text-left p-3 font-medium text-muted-foreground">Subsystem</th>
            <th className="text-left p-3 font-medium text-muted-foreground">Features</th>
            <th className="text-center p-3 font-medium text-muted-foreground">Count</th>
            <th className="text-center p-3 font-medium text-muted-foreground">Correct</th>
            <th className="text-center p-3 font-medium text-muted-foreground">Confidence</th>
            <th className="text-center p-3 font-medium text-muted-foreground">Health</th>
            <th className="text-center p-3 font-medium text-muted-foreground">Decay</th>
            <th className="text-right p-3 font-medium text-muted-foreground">Last Seen</th>
          </tr>
        </thead>
        <tbody>
          {patterns.map((p) => (
            <tr key={p.id} className="border-b last:border-0 hover:bg-muted/30 transition-colors">
              <td className="p-3">
                <SubsystemLabel subsystem={p.subsystem} />
              </td>
              <td className="p-3">
                <div className="flex flex-wrap gap-1">
                  {Object.entries(p.features).filter(([, v]) => v).slice(0, 3).map(([k, v]) => (
                    <FeatureTag key={k} k={k} v={v} />
                  ))}
                </div>
              </td>
              <td className="p-3 text-center tabular-nums">{p.total_count}</td>
              <td className="p-3 text-center tabular-nums">{p.correct_count}</td>
              <td className="p-3 text-center">
                <span className={`tabular-nums font-medium ${
                  p.confidence >= 80 ? 'text-green-500' : p.confidence >= 60 ? 'text-amber-500' : 'text-red-500'
                }`}>
                  {p.confidence}%
                </span>
              </td>
              <td className="p-3 text-center">
                <HealthBadge health={p.health} />
              </td>
              <td className="p-3 text-center">
                <DecayBadge decay={p.decay_status} />
              </td>
              <td className="p-3 text-right text-muted-foreground text-xs">
                {p.last_seen ? formatDistanceToNow(parseISO(p.last_seen), { addSuffix: true }) : 'Never'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
