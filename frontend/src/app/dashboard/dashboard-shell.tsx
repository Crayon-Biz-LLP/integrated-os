'use client';

import { useState, useEffect, useMemo } from 'react';
import type { Task, TaskStats } from '@/lib/tasks/types';
import type { EmailPendingTask, EmailStats } from '@/lib/emails/types';
import type { CalendarEvent } from '@/lib/calendar/types';
import { StatsCards } from '@/components/dashboard/stats-cards';
import { WhatToDoNow } from '@/components/dashboard/what-to-do-now';
import { QuickChat } from '@/components/dashboard/quick-chat';
import { PulseBriefings } from '@/components/dashboard/pulse-briefings';
import { RecentTasks } from '@/components/dashboard/recent-tasks';
import { Button } from '@/components/ui/button';

const API_BASE = process.env.NEXT_PUBLIC_BACKEND_URL || '';

export function DashboardShell({
  initialOpenTasks,
  initialTaskStats,
  initialPendingEmails,
  initialEmailStats,
}: {
  initialOpenTasks: Task[];
  initialTaskStats: TaskStats;
  initialPendingEmails: EmailPendingTask[];
  initialEmailStats: EmailStats;
}) {
  const [calendarEvents, setCalendarEvents] = useState<CalendarEvent[]>([]);

  useEffect(() => {
    fetch(`${API_BASE}/api/calendar-events?date=today`)
      .then((res) => res.json())
      .then((data) => setCalendarEvents(data.events || []))
      .catch(() => {});
  }, []);

  const today = useMemo(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  }, []);

  const overdueTasks = useMemo(
    () => initialOpenTasks.filter((t) => {
      if (t.status === 'done' || t.status === 'cancelled') return false;
      const due = new Date(t.reminder_at || t.deadline || '');
      return due < today;
    }),
    [initialOpenTasks, today]
  );

  const dueTodayTasks = useMemo(
    () => initialOpenTasks.filter((t) => {
      if (t.status === 'done' || t.status === 'cancelled') return false;
      const due = new Date(t.reminder_at || t.deadline || '');
      return due >= today && due < new Date(today.getTime() + 86400000);
    }),
    [initialOpenTasks, today]
  );

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold tracking-tight">🧭 Command Center</h1>
        <div className="flex gap-2">
          <Button variant="outline" size="sm">? Query</Button>
          <Button variant="outline" size="sm">N: Note</Button>
          <Button variant="outline" size="sm">+ Task</Button>
        </div>
      </div>

      <StatsCards taskStats={initialTaskStats} emailStats={initialEmailStats} />

      <WhatToDoNow
        overdueTasks={overdueTasks}
        dueTodayTasks={dueTodayTasks}
        pendingEmails={initialPendingEmails}
        calendarEvents={calendarEvents}
      />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <QuickChat />
        <PulseBriefings />
      </div>

      <RecentTasks />
    </div>
  );
}
