'use client';

import { Project } from '@/lib/projects/types';
import { Badge } from '@/components/ui/badge';
import { Calendar } from 'lucide-react';

interface ProjectCardProps {
  project: Project;
  onClick: (project: Project) => void;
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

function getRelativeTime(dateStr: string | null): string {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return `${diffDays}d ago`;
  if (diffDays < 30) return `${Math.floor(diffDays / 7)}w ago`;
  if (diffDays < 365) return `${Math.floor(diffDays / 30)}mo ago`;
  return `${Math.floor(diffDays / 365)}y ago`;
}

export function ProjectCard({ project, onClick }: ProjectCardProps) {
  const isArchived = project.status === 'archived';
  const orgTagBadge = project.org_tag ? orgTagColors[project.org_tag] : '';
  const contextLabel = contextLabels[project.context] || project.context;

  return (
    <div
      className={`
        rounded-lg border bg-card p-4 cursor-pointer transition-all
        hover:border-muted-foreground/30 hover:bg-muted/20
        ${isArchived ? 'opacity-60' : ''}
      `}
      onClick={() => onClick(project)}
    >
      <div className="flex flex-col gap-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-sm leading-tight truncate">
              {project.name}
            </h3>
            {project.parent_project_name && (
              <p className="text-xs text-muted-foreground mt-0.5">
                Sub-project of {project.parent_project_name}
              </p>
            )}
          </div>
          {isArchived && (
            <Badge variant="outline" className="text-xs shrink-0">
              Archived
            </Badge>
          )}
        </div>

        {project.description && (
          <p className="text-xs text-muted-foreground line-clamp-2">
            {project.description}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2 mt-1">
          {project.org_tag && (
            <span
              className={`text-xs px-2 py-0.5 rounded-md font-medium ${orgTagBadge}`}
            >
              {project.org_tag}
            </span>
          )}
          <span className="text-xs text-muted-foreground">
            {contextLabel}
          </span>
        </div>

        <div className="flex items-center justify-between mt-2 pt-2 border-t border-border/50">
          <span className="text-xs">
            {project.open_task_count > 0 ? (
              <span className="text-foreground font-medium">
                {project.open_task_count} open task{project.open_task_count !== 1 ? 's' : ''}
              </span>
            ) : (
              <span className="text-muted-foreground">No open tasks</span>
            )}
          </span>
          {project.created_at && (
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              <Calendar className="h-3 w-3" />
              {getRelativeTime(project.created_at)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}