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

export function TasksTable({ tasks, onTaskClick, onChangeProjectClick }: TasksTableProps) {
  return (
    <div className="rounded-lg border overflow-hidden">
      <Table>
        <TableHeader className="bg-muted/20">
          <TableRow>
            <TableHead className="w-[40%]">Task</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Priority</TableHead>
            <TableHead>Project</TableHead>
            <TableHead>Due</TableHead>
            <TableHead className="w-10"></TableHead>
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
                className="cursor-pointer hover:bg-muted/30"
                onClick={() => onTaskClick(task)}
              >
                <TableCell className="font-medium">
                  <span className={isOverdue(task) ? 'text-red-600 font-semibold' : ''}>
                    {stripMarkdown(task.title)}
                  </span>
                </TableCell>
                <TableCell>
                  <Badge variant={statusVariants[task.status]} className="capitalize">
                    {task.status.replace('_', ' ')}
                  </Badge>
                </TableCell>
                <TableCell>
                  <span className={`text-xs font-medium ${priorityColors[task.priority]}`}>
                    {task.priority}
                  </span>
                </TableCell>
                <TableCell>
                  <span className="text-sm text-muted-foreground">{task.project_name}</span>
                </TableCell>
                <TableCell>
                  <span className={isOverdue(task) ? 'text-red-600 text-sm font-medium' : 'text-sm text-muted-foreground'}>
                    {formatDate(getDueDate(task))}
                  </span>
                </TableCell>
                <TableCell>
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
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}