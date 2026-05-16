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

function formatSinceDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', {
    month: 'short',
    year: 'numeric',
  });
}

export function ProjectCard({ project, onClick }: ProjectCardProps) {
  const isArchived = project.status === 'archived';
  const orgTagBadge = project.org_tag ? orgTagColors[project.org_tag] : '';
  const contextLabel = contextLabels[project.context] || project.context;
  const keywords = project.keywords || [];
  const displayKeywords = keywords.slice(0, 5);
  const extraKeywords = keywords.length > 5 ? keywords.length - 5 : 0;

  return (
    <div
      className="card-premium p-5 flex flex-col gap-3 cursor-pointer group"
      onClick={() => onClick(project)}
    >
      <div className="flex flex-col gap-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-base tracking-tight group-hover:text-primary transition-colors duration-150 truncate">
              {project.name}
            </h3>
            {project.parent_project_name && (
              <p className="text-xs text-muted-foreground mt-0.5">
                ↳ Parent: {project.parent_project_name}
              </p>
            )}
          </div>
        {isArchived && (
          <span className="text-xs bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-full font-medium">
            Archived
          </span>
        )}
        </div>

        {project.description && (
          <p className="text-sm text-muted-foreground/80 leading-relaxed line-clamp-2 mt-1">
            {project.description}
          </p>
        )}

        {displayKeywords.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 mt-1">
            {displayKeywords.map((keyword, i) => (
              <span key={i} className="text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-medium">
                {keyword}
              </span>
            ))}
            {extraKeywords > 0 && (
              <span className="text-xs text-muted-foreground/60">+{extraKeywords} more</span>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 mt-1">
          <span className="text-xs text-muted-foreground/70">
            {contextLabel}
          </span>
        </div>

        <div className="flex items-center justify-between mt-2 pt-2 border-t border-border/50">
          <span className={`text-xs text-muted-foreground/60 font-mono ${project.open_task_count > 0 ? 'text-primary font-medium' : ''}`}>
            {project.open_task_count > 0 ? (
              <span>
                {project.open_task_count} open task{project.open_task_count !== 1 ? 's' : ''}
              </span>
            ) : (
              <span>Idle</span>
            )}
          </span>
          {project.created_at && (
            <span className="text-xs text-muted-foreground flex items-center gap-1">
              <Calendar className="h-3 w-3" />
              Since {formatSinceDate(project.created_at)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}