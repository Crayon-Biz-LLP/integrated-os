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
  TECHTOOL: 'text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase',
  COMPETITOR: 'text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase',
  LEADPOTENTIAL: 'text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase',
  MARKETTREND: 'text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase',
  ASHRAYA: 'text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase',
  PERSONAL: 'text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase',
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
  const categoryColor = resource.category ? (categoryColors[resource.category] || 'text-xs bg-muted/60 text-muted-foreground/70 px-2 py-0.5 rounded-md font-semibold tracking-wide uppercase') : '';

  return (
    <div
      className={cn(
        "card-premium p-4 flex flex-col gap-2 cursor-pointer group",
        isUnmapped && "opacity-70"
      )}
      onClick={() => onClick(resource)}
    >
      <div className="flex flex-col gap-1.5">
        <div className="flex items-start justify-between gap-2">
          <h3 className="font-semibold text-sm leading-snug tracking-tight group-hover:text-primary transition-colors duration-150 line-clamp-2 flex-1">
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
          <span className={`w-fit ${categoryColor}`}>
            {resource.category}
          </span>
        )}

        {resource.summary && (
          <p className="text-xs text-muted-foreground/70 line-clamp-2 leading-relaxed">
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
            <span className="text-xs bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded font-semibold tracking-wide uppercase">
              {resource.mission_title || 'Unmapped'}
            </span>
          )}
          {resource.hostname && !showMissionBadge && (
            <span className="text-xs text-muted-foreground/60 font-mono truncate">{resource.hostname}</span>
          )}
          {resource.created_at && (
            <span className="text-xs text-muted-foreground/50 font-mono ml-auto">
              {formatDate(resource.created_at)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
