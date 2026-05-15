'use client';

import useSWR from 'swr';
import { fetcher } from '@/lib/fetcher';
import { Task } from '@/lib/tasks/types';
import { markTaskDone } from '@/lib/tasks/api';
import { useMemo } from 'react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';

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

function isOverdue(task: Task): boolean {
  const dueDate = getDueDate(task);
  if (!dueDate) return false;
  if (task.status === 'done' || task.status === 'cancelled') return false;
  
  const due = new Date(dueDate);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  
  return due < today;
}

const SWR_KEY = '/api/tasks?status=todo';

export function RecentTasks() {
  const { data, isLoading, mutate } = useSWR<Task[]>(SWR_KEY, fetcher, {
    refreshInterval: 30000,
  });

  const tasks = useMemo(() => {
    if (!data) return [];
    return data
      .filter((t) => t.status !== 'done' && t.status !== 'cancelled')
      .sort((a, b) => {
        const dateA = new Date(getDueDate(a) || '9999');
        const dateB = new Date(getDueDate(b) || '9999');
        return dateA.getTime() - dateB.getTime();
      })
      .slice(0, 5);
  }, [data]);

  const handleMarkDone = async (taskId: number) => {
    try {
      await markTaskDone(taskId);
      mutate();
    } catch (error) {
      console.error('Failed to mark done:', error);
    }
  };

  if (isLoading) {
    return (
      <div className="card-premium p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold">📋 Recent Tasks</h2>
        </div>
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-16 rounded-lg border bg-muted/20 animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="card-premium p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">📋 Recent Tasks</h2>
        <a 
          href="/dashboard/tasks"
          className="inline-flex items-center justify-center rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted"
        >
          View All →
        </a>
      </div>
      
      {tasks.length === 0 ? (
        <p className="text-sm text-muted-foreground">No open tasks.</p>
      ) : (
        <div className="space-y-3">
          {tasks.map((task) => (
            <div 
              key={task.id} 
              className={`flex items-center justify-between p-3 rounded-lg bg-muted/30 ${
                isOverdue(task) ? 'border-l-4 border-l-destructive' : ''
              }`}
            >
              <div className="flex-1">
                <p className={`text-sm ${isOverdue(task) ? 'text-destructive font-semibold' : ''}`}>
                  {task.title}
                </p>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-xs text-muted-foreground">
                    {formatDate(getDueDate(task))}
                  </span>
                  {task.project_name && (
                    <Badge variant="outline" className="text-[10px]">
                      {task.project_name}
                    </Badge>
                  )}
                </div>
              </div>
              {task.status !== 'done' && (
                <Button 
                  size="sm" 
                  variant="outline"
                  onClick={() => handleMarkDone(task.id)}
                >
                  ✓ Done
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
