'use client';

import { Resource } from '@/lib/resources/types';
import { Badge } from '@/components/ui/badge';
import { ExternalLink } from 'lucide-react';
import { cn } from '@/lib/utils';

interface ResourceCardProps {
  resource: Resource;
  onClick: (resource: Resource) => void;
  showMissionBadge?: boolean;
}

const categoryColors: Record<string, string> = {
  TECHTOOL: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  COMPETITOR: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  LEADPOTENTIAL: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  MARKETTREND: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  CHURCH: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  PERSONAL: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
};

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function getDisplayTitle(resource: Resource): string {
  return resource.title || resource.hostname || resource.url || 'Untitled';
}

export function ResourceCard({ resource, onClick, showMissionBadge = false }: ResourceCardProps) {
  const isUnmapped = !resource.mission_id;
  const categoryColor = resource.category ? (categoryColors[resource.category] || 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300') : '';

  return (
    <div
      className={cn(
        "rounded-lg border bg-card p-3 cursor-pointer transition-all hover:border-muted-foreground/30 hover:bg-muted/20",
        isUnmapped && "opacity-70"
      )}
      onClick={() => onClick(resource)}
    >
      <div className="flex flex-col gap-1.5">
        <div className="flex items-start justify-between gap-2">
          <h3 className="text-sm font-medium leading-tight line-clamp-2 flex-1">
            {getDisplayTitle(resource)}
          </h3>
          {resource.url && (
            <a
              href={resource.url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="text-muted-foreground hover:text-foreground shrink-0"
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
        </div>

        {resource.category && (
          <Badge variant="secondary" className={`text-[10px] py-0 px-1.5 w-fit ${categoryColor}`}>
            {resource.category}
          </Badge>
        )}

        {resource.summary && (
          <p className="text-xs text-muted-foreground line-clamp-2">
            {resource.summary}
          </p>
        )}

        {resource.strategic_note && (
          <p className="text-xs text-muted-foreground/70 line-clamp-1 italic">
            {resource.strategic_note}
          </p>
        )}

        <div className="flex items-center justify-between mt-1 pt-1.5 border-t border-border/50">
          {showMissionBadge && (
            <Badge variant="outline" className="text-[10px]">
              {resource.mission_title || 'Unmapped'}
            </Badge>
          )}
          {resource.hostname && !showMissionBadge && (
            <span className="text-[10px] text-muted-foreground">{resource.hostname}</span>
          )}
          {resource.created_at && (
            <span className="text-[10px] text-muted-foreground ml-auto">
              {formatDate(resource.created_at)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
