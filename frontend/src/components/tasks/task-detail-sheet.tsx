'use client';

import { Task } from '@/lib/tasks/types';
import { stripMarkdown } from '@/lib/utils/strip-markdown';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Button } from '@/components/ui/button';
import { FolderOpen } from 'lucide-react';

interface TaskDetailSheetProps {
  task: Task | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChangeProjectClick: () => void;
}

const statusVariants: Record<string, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  todo: 'default',
  in_progress: 'secondary',
  done: 'outline',
  blocked: 'destructive',
  cancelled: 'outline',
};

const statusClasses: Record<string, string> = {
  todo: 'bg-primary text-primary-foreground text-xs px-2.5 py-0.5 rounded-full font-medium',
  done: 'bg-muted text-muted-foreground text-xs px-2.5 py-0.5 rounded-full font-medium border border-border',
};

const priorityColors: Record<string, string> = {
  low: 'text-muted-foreground text-xs',
  medium: 'text-muted-foreground text-xs',
  high: 'text-muted-foreground text-xs',
  urgent: 'text-destructive text-xs font-semibold',
};

function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', { 
    month: 'short', 
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

export function TaskDetailSheet({ task, open, onOpenChange, onChangeProjectClick }: TaskDetailSheetProps) {
  if (!task) return null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-lg">
        <SheetHeader>
          <SheetTitle className="text-lg font-semibold tracking-tight">Task Details</SheetTitle>
        </SheetHeader>

        <div className="mt-6 space-y-4">
          <div>
            <h3 className="text-lg font-semibold leading-tight">{stripMarkdown(task.title)}</h3>
          </div>

          <Separator />

          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="section-label mb-1">Status</p>
              <span className={statusClasses[task.status] || 'text-xs capitalize'}>
                {task.status.replace('_', ' ')}
              </span>
            </div>

            <div>
              <p className="section-label mb-1">Priority</p>
              <span className={`text-sm text-foreground ${priorityColors[task.priority]}`}>
                {task.priority}
              </span>
            </div>

            <div>
              <p className="section-label mb-1">Project</p>
              <span className="text-sm text-foreground">{task.project_name}</span>
            </div>

            <div>
              <p className="section-label mb-1">Due Date</p>
              <span className="text-sm text-foreground">{formatDateTime(task.reminder_at || task.deadline)}</span>
            </div>

            <div>
              <p className="section-label mb-1">Created</p>
              <span className="text-sm text-foreground">{formatDateTime(task.created_at)}</span>
            </div>

            <div>
              <p className="section-label mb-1">Completed</p>
              <span className="text-sm text-foreground">{formatDateTime(task.completed_at)}</span>
            </div>
          </div>

          {task.is_revenue_critical && (
            <div className="text-xs text-amber-500 font-medium">
              Revenue Critical
            </div>
          )}

          <Separator />

          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onChangeProjectClick}
              className="gap-2"
            >
              <FolderOpen className="h-4 w-4" />
              Change Project
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}