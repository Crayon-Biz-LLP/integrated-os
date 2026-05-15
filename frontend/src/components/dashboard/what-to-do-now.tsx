'use client';

import { Task } from '@/lib/tasks/types';
import { CalendarEvent } from '@/lib/calendar/types';
import { EmailPendingTask } from '@/lib/emails/types';
import { markTaskDone } from '@/lib/tasks/api';
import { approveShortcode, rejectShortcode } from '@/lib/emails/api';
import { Button } from '@/components/ui/button';

interface WhatToDoNowProps {
  overdueTasks: Task[];
  dueTodayTasks: Task[];
  pendingEmails: EmailPendingTask[];
  calendarEvents: CalendarEvent[];
}

function formatTime(dateTimeStr: string): string {
  const date = new Date(dateTimeStr);
  return date.toLocaleTimeString('en-US', { 
    hour: 'numeric', 
    minute: '2-digit',
    hour12: true 
  });
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const targetDate = new Date(date);
  targetDate.setHours(0, 0, 0, 0);

  const diffDays = Math.floor((targetDate.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));

  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Tomorrow';
  if (diffDays === -1) return 'Yesterday';
  if (diffDays < -1) return `${Math.abs(diffDays)}d ago`;
  if (diffDays <= 7) return `In ${diffDays}d`;
  
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function getDueDate(task: Task): string | null {
  return task.reminder_at || task.deadline;
}

export function WhatToDoNow({ 
  overdueTasks, 
  dueTodayTasks, 
  pendingEmails, 
  calendarEvents 
}: WhatToDoNowProps) {
  
  const hasOverdue = overdueTasks.length > 0;
  const hasDueToday = dueTodayTasks.length > 0;
  const hasPendingEmails = pendingEmails.length > 0;
  const hasCalendarEvents = calendarEvents.length > 0;

  if (!hasOverdue && !hasDueToday && !hasPendingEmails && !hasCalendarEvents) {
    return (
      <div className="card-premium p-6">
        <h2 className="text-xl font-semibold mb-4">🎯 What to Do Now</h2>
        <p className="text-sm text-muted-foreground">Nothing urgent right now. Enjoy the calm! ☺️</p>
      </div>
    );
  }

  return (
    <div className="card-premium p-6 space-y-6">
      <h2 className="text-xl font-semibold">🎯 What to Do Now</h2>
      
      {/* Overdue Tasks */}
      {hasOverdue && (
        <section>
          <h3 className="section-label text-destructive mb-3">⚠️ Overdue</h3>
          <div className="space-y-2">
            {overdueTasks.map((task) => (
              <div key={task.id} className="flex items-center justify-between p-3 bg-destructive/5 rounded-lg border border-destructive/20">
                <div className="flex-1">
                  <p className="text-sm font-medium text-destructive">{task.title}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {formatDate(getDueDate(task))}
                    {task.project_name && ` • ${task.project_name}`}
                  </p>
                </div>
                <Button 
                  size="sm" 
                  variant="outline"
                  onClick={async () => {
                    await markTaskDone(task.id);
                    window.location.reload();
                  }}
                >
                  ✓ Done
                </Button>
              </div>
            ))}
          </div>
        </section>
      )}
      
      {/* Due Today */}
      {hasDueToday && (
        <section>
          <h3 className="section-label mb-3">📅 Due Today</h3>
          <div className="space-y-2">
            {dueTodayTasks.map((task) => (
              <div key={task.id} className="flex items-center justify-between p-3 bg-amber-500/5 rounded-lg border border-amber-500/20">
                <div className="flex-1">
                  <p className="text-sm font-medium">{task.title}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {task.project_name && `${task.project_name} • `}
                    {task.priority && <span className="capitalize">{task.priority}</span>}
                  </p>
                </div>
                <Button 
                  size="sm" 
                  variant="outline"
                  onClick={async () => {
                    await markTaskDone(task.id);
                    window.location.reload();
                  }}
                >
                  ✓ Done
                </Button>
              </div>
            ))}
          </div>
        </section>
      )}
      
      {/* Pending Email Decisions */}
      {hasPendingEmails && (
        <section>
          <h3 className="section-label mb-3">📧 Pending Email Decisions</h3>
          <div className="space-y-2">
            {pendingEmails.map((email) => (
              <div key={email.id} className="flex items-center justify-between p-3 bg-blue-500/5 rounded-lg border border-blue-500/20">
                <div className="flex-1">
                  <p className="text-sm font-medium">[{email.id}] {email.suggested_title}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {email.email?.subject && `Re: ${email.email.subject}`}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button 
                    size="sm" 
                    variant="outline"
                    onClick={async () => {
                      await approveShortcode(email.id);
                      window.location.reload();
                    }}
                  >
                    ✓
                  </Button>
                  <Button 
                    size="sm" 
                    variant="outline"
                    onClick={async () => {
                      await rejectShortcode(email.id);
                      window.location.reload();
                    }}
                  >
                    ✗
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
      
      {/* Calendar Events */}
      {hasCalendarEvents && (
        <section>
          <h3 className="section-label mb-3">📅 Today's Calendar</h3>
          <div className="space-y-2">
            {calendarEvents.map((event) => (
              <div key={event.id} className="p-3 bg-muted/30 rounded-lg">
                <p className="text-sm font-medium">{event.summary}</p>
                <p className="text-xs text-muted-foreground mt-1">
                  {formatTime(event.start.dateTime)}
                  {event.end && ` - ${formatTime(event.end.dateTime)}`}
                </p>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
