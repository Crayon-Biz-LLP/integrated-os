'use client';

import { useState } from 'react';
import { Project } from '@/lib/projects/types';
import { updateProjectStatus } from '@/lib/projects/api';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Button } from '@/components/ui/button';
import { Archive, RotateCcw, ExternalLink, Tag, Calendar, FolderOpen } from 'lucide-react';
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

function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
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

  const handleViewTasks = () => {
    onOpenChange(false);
    router.push(`/dashboard/tasks?projectId=${project.id}`);
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
              <span className={project.open_task_count > 0 ? 'font-medium' : 'text-muted-foreground'}>
                {project.open_task_count}
              </span>
            </div>

            <div className="col-span-2">
              <p className="text-muted-foreground mb-1 flex items-center gap-1">
                <Calendar className="h-3 w-3" />
                Created
              </p>
              <span className="text-foreground text-xs">{formatDateTime(project.created_at)}</span>
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

          <div className="flex flex-col gap-2">
            {project.open_task_count > 0 && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleViewTasks}
                className="gap-2"
              >
                <ExternalLink className="h-4 w-4" />
                View {project.open_task_count} Open Task{project.open_task_count !== 1 ? 's' : ''}
              </Button>
            )}

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