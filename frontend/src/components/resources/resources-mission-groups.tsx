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
              <h3 className="text-sm font-semibold">{mission.title}</h3>
              {mission.description && (
                <span className="text-xs text-muted-foreground">
                  — {mission.description}
                </span>
              )}
              <Badge variant="secondary" className="text-[10px] ml-auto">
                {missionResources.length} resource{missionResources.length !== 1 ? 's' : ''}
              </Badge>
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
            <h3 className="text-sm font-semibold text-muted-foreground">Unmapped</h3>
            <Badge variant="outline" className="text-[10px] ml-auto">
              {unmappedResources.length} resource{unmappedResources.length !== 1 ? 's' : ''}
            </Badge>
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
