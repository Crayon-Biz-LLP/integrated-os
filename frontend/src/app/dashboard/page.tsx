'use client';

import { useState, useEffect } from 'react';
import { StatsCards } from '@/components/dashboard/stats-cards';
import { WhatToDoNow } from '@/components/dashboard/what-to-do-now';
import { QuickChat } from '@/components/dashboard/quick-chat';
import { PulseBriefings } from '@/components/dashboard/pulse-briefings';
import { RecentTasks } from '@/components/dashboard/recent-tasks';
import { fetchTasks, fetchTaskStats } from '@/lib/tasks/api';
import type { Task, TaskStats } from '@/lib/tasks/types';
import { fetchPendingTasks } from '@/lib/emails/api';
import type { EmailPendingTask, EmailStats } from '@/lib/emails/types';
import { fetchEmailStats } from '@/lib/emails/api';
import { CalendarEvent, fetchCalendarEvents } from '@/lib/calendar/api';
import { Button } from '@/components/ui/button';

export default function DashboardPage() {
  const [taskStats, setTaskStats] = useState<TaskStats | null>(null);
  const [emailStats, setEmailStats] = useState<EmailStats | null>(null);
  const [overdueTasks, setOverdueTasks] = useState<Task[]>([]);
  const [dueTodayTasks, setDueTodayTasks] = useState<Task[]>([]);
  const [pendingEmails, setPendingEmails] = useState<EmailPendingTask[]>([]);
  const [calendarEvents, setCalendarEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadData = async () => {
      try {
        const [tasks, stats, emails, calEvents] = await Promise.all([
          fetchTasks({ status: 'open' }),
          fetchTaskStats(),
          fetchPendingTasks(),
          fetchCalendarEvents(),
        ]);

        setTaskStats(stats);
        setPendingEmails(emails);

        // Filter tasks
        const now = new Date();
        const today = new Date(); today.setHours(0,0,0,0);

        setOverdueTasks(tasks.filter(t => {
          if (t.status === 'done' || t.status === 'cancelled') return false;
          const due = new Date(t.reminder_at || t.deadline || '');
          return due < today;
        }));

        setDueTodayTasks(tasks.filter(t => {
          if (t.status === 'done' || t.status === 'cancelled') return false;
          const due = new Date(t.reminder_at || t.deadline || '');
          return due >= today && due < new Date(today.getTime() + 86400000);
        }));

        setCalendarEvents(calEvents);
      } catch (error) {
        console.error('Failed to load dashboard data:', error);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, []);

  if (loading) {
    return (
      <div className="p-8 space-y-6">
        <div className="h-8 w-64 bg-muted animate-pulse rounded" />
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[1,2,3,4].map(i => (
            <div key={i} className="h-20 rounded-lg border bg-muted/20 animate-pulse" />
          ))}
        </div>
        <div className="h-64 rounded-lg border bg-muted/20 animate-pulse" />
      </div>
    );
  }

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

      {taskStats && emailStats && (
        <StatsCards taskStats={taskStats} emailStats={emailStats} />
      )}

      <WhatToDoNow 
        overdueTasks={overdueTasks}
        dueTodayTasks={dueTodayTasks}
        pendingEmails={pendingEmails}
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
