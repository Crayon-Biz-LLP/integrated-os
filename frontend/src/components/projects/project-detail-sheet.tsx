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
import { Archive, RotateCcw, FolderOpen, Tag, Calendar, DollarSign, FileText } from 'lucide-react';
import { useRouter } from 'next/navigation';

interface ProjectDetailSheetProps {
  project: Project | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onStatusChange: (project: Project) => void;
}

const orgTagColors: Record<string, string> = {
  SOLVSTRAT: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  ASHRAYA: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
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
    urgent: 'bg-destructive/10 text-destructive border border-destructive/20 px-2 py-0.5 rounded-full font-medium',
    high: 'bg-amber-500/10 text-amber-600 border border-amber-500/20 px-2 py-0.5 rounded-full font-medium',
    medium: 'bg-yellow-500/10 text-yellow-600 border border-yellow-500/20 px-2 py-0.5 rounded-full font-medium',
    low: 'bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-full font-medium',
    important: 'bg-blue-500/10 text-blue-600 border border-blue-500/20 px-2 py-0.5 rounded-full font-medium',
  };

  const taskStatusColors: Record<string, string> = {
    todo: 'text-xs bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded-full font-medium',
    in_progress: 'text-xs bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded-full font-medium',
    done: 'text-xs bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-full font-medium',
    blocked: 'text-xs bg-destructive/10 text-destructive border border-destructive/20 px-2 py-0.5 rounded-full font-medium',
    cancelled: 'text-xs bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-full font-medium',
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
          <SheetTitle className="text-lg font-semibold tracking-tight">Project Details</SheetTitle>
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
              <p className="section-label mb-1">Status</p>
              <span className={isArchived ? "text-xs bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-full font-medium" : "text-xs bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded-full font-medium"}>
                {project.status}
              </span>
            </div>

            <div>
              <p className="section-label mb-1">Context</p>
              <span className="text-sm text-foreground">{contextLabels[project.context] || project.context}</span>
            </div>

            <div>
              <p className="section-label mb-1">Area</p>
              {project.org_tag ? (
                <span className="text-xs bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded font-semibold tracking-wide uppercase">
                  {project.org_tag}
                </span>
              ) : (
                <span className="text-sm text-foreground">-</span>
              )}
            </div>

            <div>
              <p className="section-label mb-1">Open Tasks</p>
              <span className={`text-xs text-muted-foreground/60 font-mono ${tasks.length > 0 ? 'text-primary font-medium' : ''}`}>
                {tasks.length}
              </span>
            </div>

            <div className="col-span-2">
              <p className="section-label mb-1 flex items-center gap-1">
                <Calendar className="h-3 w-3" />
                Created
              </p>
              <span className="text-sm text-foreground">{formatSinceDate(project.created_at)}</span>
            </div>
          </div>

            {project.description && (
              <>
                <Separator />
                <div>
                  <p className="section-label mb-2 flex items-center gap-1">
                    <FileText className="h-3 w-3" />
                    Description
                  </p>
                  <p className="text-sm text-muted-foreground/80 leading-relaxed">{project.description}</p>
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
                  <p className="section-label mb-2">Open Tasks</p>
              {tasksLoading ? (
                <div className="space-y-2">
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="h-12 rounded border bg-muted/20 animate-pulse" />
                  ))}
                </div>
              ) : tasks.length === 0 ? (
                <p className="text-sm text-muted-foreground">No open tasks — project is idle</p>
              ) : (
                <div className="space-y-0">
                  {tasks.map((task) => (
                    <div
                      key={task.id}
                      className="text-sm border-b border-border/40 py-2 hover:bg-muted/30 transition-colors"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex-1 min-w-0">
                          <p className="font-medium truncate">{stripMarkdown(task.title)}</p>
                          <div className="flex items-center gap-1 mt-0.5 flex-wrap">
                            <span className={`${taskStatusColors[task.status] || 'text-xs bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-full font-medium'}`}>
                              {task.status.replace('_', ' ')}
                            </span>
                            <span className={`text-xs ${priorityColors[task.priority] || 'text-xs bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-full font-medium'}`}>
                              {task.priority}
                            </span>
                            {task.is_revenue_critical && (
                              <span className="text-xs bg-destructive/10 text-destructive border border-destructive/20 px-2 py-0.5 rounded-full font-medium flex items-center gap-0.5">
                                <DollarSign className="h-3 w-3" />
                                Revenue
                              </span>
                            )}
                          </div>
                        </div>
                        <span className="text-xs text-muted-foreground/60 font-mono shrink-0">
                          {getTaskDueDate(task)}
                        </span>
                      </div>
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