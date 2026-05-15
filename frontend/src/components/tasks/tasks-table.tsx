'use client';

import { Task } from '@/lib/tasks/types';
import { stripMarkdown } from '@/lib/utils/strip-markdown';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { MoreHorizontal } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface TasksTableProps {
  tasks: Task[];
  onTaskClick: (task: Task) => void;
  onChangeProjectClick: (task: Task) => void;
  onTaskDone?: (task: Task) => Promise<void>;
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

export function TasksTable({ tasks, onTaskClick, onChangeProjectClick, onTaskDone }: TasksTableProps) {
  return (
    <div className="card-premium overflow-hidden">
      <Table>
        <TableHeader className="bg-muted/40 border-b border-border">
          <TableRow>
            <TableHead className="section-label py-3 px-4 text-left w-[35%]">Task</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Status</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Priority</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Project</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Due</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tasks.length === 0 ? (
            <TableRow>
              <TableCell colSpan={6} className="h-24 text-center text-muted-foreground">
                No tasks found
              </TableCell>
            </TableRow>
          ) : (
            tasks.map((task) => (
              <TableRow
                key={task.id}
                className="border-b border-border/50 transition-all duration-150 hover:bg-primary/3 cursor-pointer"
                onClick={() => onTaskClick(task)}
              >
                <TableCell className="font-medium">
                  <span className={isOverdue(task) ? 'text-red-600 font-semibold' : ''}>
                    {stripMarkdown(task.title)}
                  </span>
                </TableCell>
                <TableCell>
                  <span className={statusClasses[task.status] || 'text-xs capitalize'}>
                    {task.status.replace('_', ' ')}
                  </span>
                </TableCell>
                <TableCell>
                  <span className={`text-xs font-medium ${priorityColors[task.priority]}`}>
                    {task.priority}
                  </span>
                </TableCell>
                <TableCell>
                  <span className="text-sm text-muted-foreground/70">{task.project_name}</span>
                </TableCell>
                <TableCell>
                  <span className={isOverdue(task) ? 'text-destructive text-xs font-medium' : 'text-xs text-muted-foreground/60 font-mono'}>
                    {formatDate(getDueDate(task))}
                  </span>
                </TableCell>
                <TableCell>
                  <div className="flex gap-1">
                    {task.status !== 'done' && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={async (e) => {
                          e.stopPropagation();
                          if (onTaskDone) {
                            await onTaskDone(task);
                          } else {
                            const { markTaskDone } = await import('@/lib/tasks/api');
                            await markTaskDone(task.id);
                            window.location.reload();
                          }
                        }}
                      >
                        ✓ Done
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        onChangeProjectClick(task);
                      }}
                    >
                      <MoreHorizontal className="h-4 w-4" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}