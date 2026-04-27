'use client';

import { Resource, ResourceMission } from '@/lib/resources/types';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { Button } from '@/components/ui/button';
import { ExternalLink, FolderOpen } from 'lucide-react';
import { cn } from '@/lib/utils';

const categoryColors: Record<string, string> = {
  TECHTOOL: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  COMPETITOR: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  LEADPOTENTIAL: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  MARKETTREND: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  CHURCH: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  PERSONAL: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400',
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

function getHostname(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return null;
  }
}

interface ResourceDetailSheetProps {
  resource: Resource | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  missions: ResourceMission[];
  onMissionChange: (resourceId: number, missionId: number | null) => void;
  relatedResources?: Resource[];
}

export function ResourceDetailSheet({ 
  resource, 
  open, 
  onOpenChange, 
  missions,
  onMissionChange,
  relatedResources 
}: ResourceDetailSheetProps) {
  if (!resource) return null;

  const categoryColor = resource.category 
    ? (categoryColors[resource.category] || 'bg-gray-100 text-gray-700') 
    : '';

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="text-base">Resource Details</SheetTitle>
        </SheetHeader>

        <div className="mt-6 space-y-4">
          <div>
            <h3 className="text-lg font-semibold leading-tight">
              {resource.title || getHostname(resource.url) || 'Untitled'}
            </h3>
          </div>

          <Separator />

          <div className="space-y-3 text-sm">
            {resource.category && (
              <div>
                <p className="text-muted-foreground mb-1">Category</p>
                <Badge className={`text-xs ${categoryColor}`}>
                  {resource.category}
                </Badge>
              </div>
            )}

            <div>
              <p className="text-muted-foreground mb-1">Mission</p>
              <Badge variant="outline" className="text-xs">
                {resource.mission_title || 'Unmapped'}
              </Badge>
            </div>

            {resource.url && (
              <div>
                <p className="text-muted-foreground mb-1">URL</p>
                <a
                  href={resource.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-600 hover:underline flex items-center gap-1"
                >
                  {getHostname(resource.url)}
                  <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            )}

            {resource.summary && (
              <div>
                <p className="text-muted-foreground mb-1">Summary</p>
                <p className="text-sm">{resource.summary}</p>
              </div>
            )}

            {resource.strategic_note && (
              <div>
                <p className="text-muted-foreground mb-1">Strategic Note</p>
                <p className="text-sm text-muted-foreground italic">{resource.strategic_note}</p>
              </div>
            )}

            <div className="grid grid-cols-2 gap-3">
              <div>
                <p className="text-muted-foreground mb-1">Created</p>
                <span className="text-xs">{formatDateTime(resource.created_at)}</span>
              </div>
              {resource.enriched_at && (
                <div>
                  <p className="text-muted-foreground mb-1">Enriched</p>
                  <span className="text-xs">{formatDateTime(resource.enriched_at)}</span>
                </div>
              )}
            </div>
          </div>

          <Separator />

          <div>
            <p className="text-muted-foreground mb-2 text-sm">Assign Mission</p>
            <select
              value={resource.mission_id ? String(resource.mission_id) : 'unmapped'}
              onChange={(e) => onMissionChange(resource.id, e.target.value === 'unmapped' ? null : Number(e.target.value))}
              className="h-9 w-full rounded-lg border bg-background px-2.5 text-sm"
            >
              <option value="unmapped">Unmapped</option>
              {missions.map((m) => (
                <option key={m.id} value={String(m.id)}>
                  {m.title}
                </option>
              ))}
            </select>
          </div>

          {resource.mission_id && (
            <>
              <Separator />
              <div>
                <p className="text-muted-foreground mb-2 text-sm">Mission Context</p>
                <p className="text-sm font-medium">{resource.mission_title}</p>
                {resource.mission_description && (
                  <p className="text-xs text-muted-foreground mt-1">{resource.mission_description}</p>
                )}
                <Badge variant="outline" className="text-[10px] mt-2">
                  Status: {resource.mission_status || 'unknown'}
                </Badge>
              </div>

              {relatedResources && relatedResources.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <p className="text-muted-foreground mb-2 text-sm">
                      Related Resources ({relatedResources.length})
                    </p>
                    <div className="space-y-2">
                      {relatedResources.slice(0, 5).map((r) => (
                        <div key={r.id} className="text-xs p-2 rounded border">
                          <p className="font-medium line-clamp-1">{r.title || getHostname(r.url) || 'Untitled'}</p>
                          {r.category && (
                            <Badge className={`text-[9px] mt-1 ${categoryColors[r.category] || ''}`}>
                              {r.category}
                            </Badge>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </>
          )}

          {!resource.mission_id && (
            <>
              <Separator />
              <p className="text-xs text-muted-foreground italic">
                This resource is not currently attached to an active mission
              </p>
            </>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
