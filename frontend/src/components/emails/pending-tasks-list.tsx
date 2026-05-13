'use client';

import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import type { EmailPendingTask } from '@/lib/emails/types';
import { decideTask } from '@/lib/emails/api';
import { toast } from 'sonner';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { Check, X, AlertTriangle } from 'lucide-react';

interface PendingTasksListProps {
  tasks: EmailPendingTask[];
  loading: boolean;
}

export function PendingTasksList({ tasks: initialTasks, loading }: PendingTasksListProps) {
  const [tasks, setTasks] = useState<EmailPendingTask[]>(initialTasks);

  useEffect(() => {
    setTasks(initialTasks);
  }, [initialTasks]);

  const handleDecision = async (id: number, decision: 'yes' | 'no') => {
    const task = tasks.find((t) => t.id === id);
    setTasks((prev) => prev.filter((t) => t.id !== id));
    try {
      await decideTask(id, decision);
    } catch (error) {
      console.error('Failed to decide task:', error);
      if (task) setTasks((prev) => [...prev, task]);
      toast.error('Failed to save decision. Task has been restored.');
    }
  };

  const isExpiringSoon = (createdAt: string) => {
    try {
      const diff = Date.now() - parseISO(createdAt).getTime();
      return diff > 5 * 24 * 60 * 60 * 1000;
    } catch {
      return false;
    }
  };

  if (loading) {
    return (
      <div className="space-y-4">
        {[...Array(3)].map((_, i) => (
          <Skeleton key={i} className="h-32 rounded-lg" />
        ))}
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        No pending task decisions. You're all caught up.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {tasks.map((task) => (
        <Card key={task.id}>
          <CardHeader className="pb-2">
            <div className="flex items-center justify-between">
              <CardTitle className="text-base font-semibold">{task.suggested_title}</CardTitle>
              <div className="flex items-center gap-2">
                {task.is_human_sender && (
                  <Badge variant="outline" className="bg-amber-500/20 text-amber-400 border-amber-500/30 text-xs">
                    👤 Human
                  </Badge>
                )}
                {isExpiringSoon(task.created_at) && (
                  <Badge variant="outline" className="bg-orange-500/20 text-orange-400 border-orange-500/30 text-xs">
                    ⚠️ Expires soon
                  </Badge>
                )}
              </div>
            </div>
            <div className="text-sm text-muted-foreground">
              {task.suggested_project ? (
                <Badge variant="outline" className="text-xs">{task.suggested_project}</Badge>
              ) : (
                <span>—</span>
              )}
            </div>
          </CardHeader>
          <CardContent>
            <div className="text-sm text-muted-foreground mb-4">
              From: {task.email?.sender || task.email?.sender_email} · Re: {task.email?.subject}
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                className="bg-green-600 hover:bg-green-700"
                onClick={() => handleDecision(task.id, 'yes')}
              >
                <Check className="h-4 w-4 mr-1" />
                Approve
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-red-400 hover:bg-red-500/20"
                onClick={() => handleDecision(task.id, 'no')}
              >
                <X className="h-4 w-4 mr-1" />
                Drop
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
