'use client';

import { Skeleton } from '@/components/ui/skeleton';
import type { EmailStats } from '@/lib/emails/types';
import { cn } from '@/lib/utils';
import { Inbox, Tag, Mail, AlertTriangle, FileEdit } from 'lucide-react';

const STAT_CONFIG = [
  { key: 'total', label: 'Total Emails', icon: Inbox, color: 'text-foreground' },
  { key: 'actionable', label: 'Actionable', icon: Tag, color: 'text-primary' },
  { key: 'fyi', label: 'FYI', icon: Mail, color: 'text-blue-600' },
  { key: 'pending_tasks', label: 'Pending Decisions', icon: AlertTriangle, color: 'text-amber-500' },
  { key: 'pending_drafts', label: 'Drafts Awaiting', icon: FileEdit, color: 'text-purple-600' },
] as const;

export function EmailStats({ stats, loading }: { stats?: EmailStats | null; loading?: boolean }) {
  const isLoading = loading ?? stats === null;

  if (isLoading) {
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
        <div key={key} className="card-premium p-5 flex flex-col gap-1">
          <div className="flex flex-row items-center justify-between space-y-0 pb-2">
            <p className="section-label">{label}</p>
            <Icon className={cn('h-4 w-4', color)} />
          </div>
          <div className={`stat-number ${color}`}>{stats?.[key] || 0}</div>
        </div>
      ))}
    </div>
  );
}
