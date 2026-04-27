'use client';

import { Task } from '@/lib/tasks/types';
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

const priorityColors: Record<string, string> = {
  low: 'text-muted-foreground',
  medium: 'text-amber-600',
  high: 'text-orange-600',
  urgent: 'text-red-600',
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
          <SheetTitle className="text-base">Task Details</SheetTitle>
        </SheetHeader>

        <div className="mt-6 space-y-4">
          <div>
            <h3 className="text-lg font-semibold leading-tight">{task.title}</h3>
          </div>

          <Separator />

          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="text-muted-foreground mb-1">Status</p>
              <Badge variant={statusVariants[task.status]} className="capitalize">
                {task.status.replace('_', ' ')}
              </Badge>
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Priority</p>
              <span className={`font-medium ${priorityColors[task.priority]}`}>
                {task.priority}
              </span>
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Project</p>
              <span className="text-foreground">{task.project_name}</span>
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Due Date</p>
              <span className="text-foreground">{formatDateTime(task.reminder_at || task.deadline)}</span>
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Created</p>
              <span className="text-foreground text-xs">{formatDateTime(task.created_at)}</span>
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Completed</p>
              <span className="text-foreground text-xs">{formatDateTime(task.completed_at)}</span>
            </div>
          </div>

          {task.is_revenue_critical && (
            <div className="text-xs text-amber-600 font-medium">
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