'use client';

import { Resource } from '@/lib/resources/types';
import { ResourceCard } from './resource-card';

interface ResourcesLibraryGridProps {
  resources: Resource[];
  onResourceClick: (resource: Resource) => void;
}

export function ResourcesLibraryGrid({ resources, onResourceClick }: ResourcesLibraryGridProps) {
  if (resources.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        <p>No resources found</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
      {resources.map((resource) => (
        <ResourceCard
          key={resource.id}
          resource={resource}
          onClick={onResourceClick}
          showMissionBadge={true}
        />
      ))}
    </div>
  );
}
