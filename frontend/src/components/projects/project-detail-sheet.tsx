'use client';

import { useState, useEffect } from 'react';
import { Project, ProjectTask } from '@/lib/projects/types';
import { updateProjectStatus, fetchProjectTasks } from '@/lib/projects/api';
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
import { Archive, RotateCcw, FolderOpen, Tag, Calendar, DollarSign } from 'lucide-react';
import { useRouter } from 'next/navigation';

interface ProjectDetailSheetProps {
  project: Project | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onStatusChange: (project: Project) => void;
}

const orgTagColors: Record<string, string> = {
  SOLVSTRAT: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  CHURCH: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  PERSONAL: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  PRODUCT_LABS: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  INBOX: 'bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400',
  ADMIN: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
};

const contextLabels: Record<string, string> = {
  work: 'Work',
  personal: 'Personal',
  admin: 'Admin',
};

const priorityColors: Record<string, string> = {
  urgent: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  high: 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
  medium: 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400',
  low: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  important: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
};

const taskStatusColors: Record<string, string> = {
  todo: 'bg-gray-100 text-gray-700',
  in_progress: 'bg-blue-100 text-blue-700',
  blocked: 'bg-red-100 text-red-700',
};

function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

function getTaskDueDate(task: ProjectTask): string {
  if (task.reminder_at) return formatDateTime(task.reminder_at);
  if (task.deadline) return formatDateTime(task.deadline);
  return '-';
}

function formatSinceDate(dateStr: string | null): string {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', {
    month: 'short',
    year: 'numeric',
  });
}

export function ProjectDetailSheet({
  project,
  open,
  onOpenChange,
  onStatusChange,
}: ProjectDetailSheetProps) {
  const router = useRouter();
  const [updating, setUpdating] = useState(false);
  const [tasks, setTasks] = useState<ProjectTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);

  useEffect(() => {
    if (open && project) {
      setTasksLoading(true);
      fetchProjectTasks(project.id)
        .then((data) => setTasks(data))
        .catch(() => setTasks([]))
        .finally(() => setTasksLoading(false));
    }
  }, [open, project]);

  if (!project) return null;

  const isArchived = project.status === 'archived';
  const orgTagBadge = project.org_tag ? orgTagColors[project.org_tag] : '';

  const handleToggleStatus = async () => {
    setUpdating(true);
    try {
      const newStatus = isArchived ? 'active' : 'archived';
      const updated = await updateProjectStatus(project.id, newStatus);
      onStatusChange({ ...project, ...updated });
    } catch (error) {
      console.error('Failed to update project status:', error);
    } finally {
      setUpdating(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="text-base">Project Details</SheetTitle>
        </SheetHeader>

        <div className="mt-6 space-y-4">
          <div>
            <h3 className="text-lg font-semibold leading-tight">{project.name}</h3>
            {project.parent_project_name && (
              <p className="text-sm text-muted-foreground mt-1 flex items-center gap-1">
                <FolderOpen className="h-3 w-3" />
                Sub-project of {project.parent_project_name}
              </p>
            )}
          </div>

          <Separator />

          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <p className="text-muted-foreground mb-1">Status</p>
              <Badge variant={isArchived ? 'outline' : 'default'} className="capitalize">
                {project.status}
              </Badge>
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Context</p>
              <span className="text-foreground">{contextLabels[project.context] || project.context}</span>
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Area</p>
              {project.org_tag ? (
                <span className={`text-xs px-2 py-0.5 rounded-md font-medium ${orgTagBadge}`}>
                  {project.org_tag}
                </span>
              ) : (
                <span className="text-muted-foreground">-</span>
              )}
            </div>

            <div>
              <p className="text-muted-foreground mb-1">Open Tasks</p>
              <span className={tasks.length > 0 ? 'font-medium' : 'text-muted-foreground'}>
                {tasks.length}
              </span>
            </div>

            <div className="col-span-2">
              <p className="text-muted-foreground mb-1 flex items-center gap-1">
                <Calendar className="h-3 w-3" />
                Created
              </p>
              <span className="text-foreground text-xs">{formatSinceDate(project.created_at)}</span>
            </div>
          </div>

          {project.description && (
            <>
              <Separator />
              <div>
                <p className="text-sm text-muted-foreground mb-1">Description</p>
                <p className="text-sm text-foreground">{project.description}</p>
              </div>
            </>
          )}

          {project.keywords && project.keywords.length > 0 && (
            <>
              <Separator />
              <div>
                <p className="text-sm text-muted-foreground mb-2 flex items-center gap-1">
                  <Tag className="h-3 w-3" />
                  Keywords
                </p>
                <div className="flex flex-wrap gap-2">
                  {project.keywords.map((keyword, i) => (
                    <Badge key={i} variant="secondary" className="text-xs">
                      {keyword}
                    </Badge>
                  ))}
                </div>
              </div>
            </>
          )}

          <Separator />

          <div>
            <p className="text-sm font-medium mb-3">Open Tasks</p>
            {tasksLoading ? (
              <div className="space-y-2">
                {[1, 2, 3].map((i) => (
                  <div key={i} className="h-12 rounded border bg-muted/20 animate-pulse" />
                ))}
              </div>
            ) : tasks.length === 0 ? (
              <p className="text-sm text-muted-foreground">No open tasks — this project is idle</p>
            ) : (
              <div className="space-y-2">
                {tasks.map((task) => (
                  <div
                    key={task.id}
                    className="flex items-start justify-between gap-2 rounded border p-2 text-sm"
                  >
                    <div className="flex-1 min-w-0">
                      <p className="font-medium truncate">{stripMarkdown(task.title)}</p>
                      <div className="flex flex-wrap items-center gap-1 mt-1">
                        <span className={`text-xs px-1.5 py-0.5 rounded ${taskStatusColors[task.status]}`}>
                          {task.status.replace('_', ' ')}
                        </span>
                        <span className={`text-xs px-1.5 py-0.5 rounded ${priorityColors[task.priority] || ''}`}>
                          {task.priority}
                        </span>
                        {task.is_revenue_critical && (
                          <span className="text-xs px-1.5 py-0.5 rounded bg-green-100 text-green-700 flex items-center gap-0.5">
                            <DollarSign className="h-3 w-3" />
                            Revenue
                          </span>
                        )}
                      </div>
                    </div>
                    <span className="text-xs text-muted-foreground shrink-0">
                      {getTaskDueDate(task)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <Separator />

          <div className="flex flex-col gap-2">
            <Button
              variant={isArchived ? 'default' : 'outline'}
              size="sm"
              onClick={handleToggleStatus}
              disabled={updating}
              className="gap-2"
            >
              {isArchived ? (
                <>
                  <RotateCcw className="h-4 w-4" />
                  Restore Project
                </>
              ) : (
                <>
                  <Archive className="h-4 w-4" />
                  Archive Project
                </>
              )}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}