'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { fetchEmailStats } from '@/lib/emails/api';
import type { EmailStats } from '@/lib/emails/types';
import { cn } from '@/lib/utils';
import { Inbox, Tag, Mail, AlertTriangle, FileEdit } from 'lucide-react';

const STAT_CONFIG = [
  { key: 'total', label: 'Total Emails', icon: Inbox, color: 'text-zinc-400' },
  { key: 'actionable', label: 'Actionable', icon: Tag, color: 'text-amber-500' },
  { key: 'fyi', label: 'FYI', icon: Mail, color: 'text-blue-500' },
  { key: 'pending_tasks', label: 'Pending Decisions', icon: AlertTriangle, color: 'text-orange-500' },
  { key: 'pending_drafts', label: 'Drafts Awaiting', icon: FileEdit, color: 'text-purple-500' },
] as const;

export function EmailStats() {
  const [stats, setStats] = useState<EmailStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchEmailStats()
      .then(setStats)
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-5 gap-4 mb-6">
        {[...Array(5)].map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-lg" />
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-5 gap-4 mb-6">
      {STAT_CONFIG.map(({ key, label, icon: Icon, color }) => (
        <Card key={key}>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">{label}</CardTitle>
            <Icon className={cn('h-4 w-4', color)} />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{stats?.[key] || 0}</div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
