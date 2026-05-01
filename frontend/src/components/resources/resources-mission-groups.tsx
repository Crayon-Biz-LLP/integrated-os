'use client';

import { Resource, ResourceMission } from '@/lib/resources/types';
import { ResourceCard } from './resource-card';
import { Badge } from '@/components/ui/badge';
import { FolderOpen } from 'lucide-react';

interface ResourcesMissionGroupsProps {
  resources: Resource[];
  missions: ResourceMission[];
  onResourceClick: (resource: Resource) => void;
}

export function ResourcesMissionGroups({ resources, missions, onResourceClick }: ResourcesMissionGroupsProps) {
  const unmappedResources = resources.filter(r => !r.mission_id);
  
  const missionResourcesMap: Record<number, Resource[]> = {};
  for (const r of resources) {
    if (r.mission_id) {
      if (!missionResourcesMap[r.mission_id]) {
        missionResourcesMap[r.mission_id] = [];
      }
      missionResourcesMap[r.mission_id].push(r);
    }
  }

  const missionsWithResources = missions
    .filter(m => missionResourcesMap[m.id]?.length > 0)
    .sort((a, b) => (missionResourcesMap[b.id]?.length || 0) - (missionResourcesMap[a.id]?.length || 0));

  if (missionsWithResources.length === 0 && unmappedResources.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        <FolderOpen className="h-12 w-12 mx-auto mb-4 opacity-20" />
        <p>No resources found</p>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {missionsWithResources.map((mission) => {
        const missionResources = missionResourcesMap[mission.id] || [];
        return (
          <div key={mission.id}>
            <div className="flex items-center gap-2 mb-3">
              <h3 className="section-label pt-6 pb-1">{mission.title}</h3>
              {mission.description && (
                <span className="text-xs text-muted-foreground/60 italic mb-3">
                  — {mission.description}
                </span>
              )}
              <span className="text-xs bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded font-semibold tracking-wide uppercase ml-auto">
                {missionResources.length} resource{missionResources.length !== 1 ? 's' : ''}
              </span>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
              {missionResources.map((resource) => (
                <ResourceCard
                  key={resource.id}
                  resource={resource}
                  onClick={onResourceClick}
                />
              ))}
            </div>
          </div>
        );
      })}

      {unmappedResources.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3">
            <h3 className="section-label pt-6 pb-1 text-muted-foreground">Unmapped</h3>
            <span className="text-xs bg-primary/10 text-primary border border-primary/20 px-2 py-0.5 rounded font-semibold tracking-wide uppercase ml-auto">
              {unmappedResources.length} resource{unmappedResources.length !== 1 ? 's' : ''}
            </span>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {unmappedResources.map((resource) => (
              <ResourceCard
                key={resource.id}
                resource={resource}
                onClick={onResourceClick}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
