'use client';

import { useState, useMemo, useCallback } from 'react';
import { ResourcesHeader } from '@/components/resources/resources-header';
import { ResourcesStats } from '@/components/resources/resources-stats';
import { ResourcesFilters } from '@/components/resources/resources-filters';
import { ResourcesViewToggle } from '@/components/resources/resources-view-toggle';
import { ResourcesMissionGroups } from '@/components/resources/resources-mission-groups';
import { ResourcesLibraryGrid } from '@/components/resources/resources-library-grid';
import { ResourceDetailSheet } from '@/components/resources/resource-detail-sheet';
import type { Resource, ResourceMission, ResourceStats, ResourceFilters as FiltersType } from '@/lib/resources/types';
import { updateResourceMission, fetchResource, fetchRelatedResources } from '@/lib/resources/api';

function filterResources(resources: Resource[], filters: FiltersType, missions: ResourceMission[]): Resource[] {
  let result = [...resources];

  if (filters.search) {
    const q = filters.search.toLowerCase();
    result = result.filter(
      (r) =>
        r.title?.toLowerCase().includes(q) ||
        r.summary?.toLowerCase().includes(q) ||
        r.strategic_note?.toLowerCase().includes(q) ||
        r.category?.toLowerCase().includes(q)
    );
  }

  if (filters.mission && filters.mission !== 'all') {
    if (filters.mission === 'unmapped') {
      result = result.filter((r) => r.mission_id === null);
    } else {
      result = result.filter((r) => String(r.mission_id) === filters.mission);
    }
  }

  if (filters.category && filters.category !== 'all') {
    result = result.filter((r) => r.category === filters.category);
  }

  switch (filters.sort) {
    case 'oldest':
      result.sort((a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime());
      break;
    case 'title':
      result.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
      break;
    case 'category':
      result.sort((a, b) => (a.category || '').localeCompare(b.category || ''));
      break;
    case 'mission':
      result.sort((a, b) => (a.mission_id || 0) - (b.mission_id || 0));
      break;
    default:
      result.sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());
  }

  return result;
}

export function ResourcesShell({
  initialResources,
  initialMissions,
  initialStats,
}: {
  initialResources: Resource[];
  initialMissions: ResourceMission[];
  initialStats: ResourceStats;
}) {
  const [resources] = useState(initialResources);
  const [selectedResource, setSelectedResource] = useState<Resource | null>(null);
  const [relatedResources, setRelatedResources] = useState<Resource[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);

  const [filters, setFilters] = useState<FiltersType>({
    search: '',
    mission: 'all',
    category: 'all',
    sort: 'newest',
    view: 'mission',
  });

  const categories = useMemo(
    () => Array.from(new Set(resources.map(r => r.category).filter(Boolean))) as string[],
    [resources]
  );

  const filteredResources = useMemo(
    () => filterResources(resources, filters, initialMissions),
    [resources, filters, initialMissions]
  );

  const handleResourceClick = useCallback(async (resource: Resource) => {
    setSelectedResource(resource);
    setDetailOpen(true);

    if (resource.mission_id) {
      try {
        const related = await fetchRelatedResources(resource.id);
        setRelatedResources(related);
      } catch {
        setRelatedResources([]);
      }
    } else {
      setRelatedResources([]);
    }
  }, []);

  const handleMissionChange = useCallback(async (resourceId: number, missionId: number | null) => {
    try {
      await updateResourceMission(resourceId, missionId);
      if (selectedResource?.id === resourceId) {
        const updated = await fetchResource(resourceId);
        setSelectedResource(updated);
        if (updated.mission_id) {
          const related = await fetchRelatedResources(resourceId);
          setRelatedResources(related);
        } else {
          setRelatedResources([]);
        }
      }
    } catch (err: any) {
      console.error('Failed to update mission:', err);
      alert('Failed to update mission: ' + (err.message || 'Unknown error'));
    }
  }, [selectedResource]);

  return (
    <div className="flex flex-col gap-6 p-8">
      <ResourcesHeader />
      <ResourcesStats stats={initialStats} loading={false} />

      <div className="flex flex-col gap-4">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
          <ResourcesFilters
            search={filters.search || ''}
            setSearch={(v) => setFilters(f => ({ ...f, search: v }))}
            mission={filters.mission || 'all'}
            setMission={(v) => setFilters(f => ({ ...f, mission: v }))}
            category={filters.category || 'all'}
            setCategory={(v) => setFilters(f => ({ ...f, category: v }))}
            sort={filters.sort || 'newest'}
            setSort={(v) => setFilters(f => ({ ...f, sort: v }))}
            missions={initialMissions}
            categories={categories}
          />
          <ResourcesViewToggle
            view={filters.view || 'mission'}
            setView={(v) => setFilters(f => ({ ...f, view: v }))}
          />
        </div>

        {filters.view === 'mission' ? (
          <ResourcesMissionGroups
            resources={filteredResources}
            missions={initialMissions}
            onResourceClick={handleResourceClick}
          />
        ) : (
          <ResourcesLibraryGrid
            resources={filteredResources}
            onResourceClick={handleResourceClick}
          />
        )}
      </div>

      <ResourceDetailSheet
        resource={selectedResource}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        missions={initialMissions}
        onMissionChange={handleMissionChange}
        relatedResources={relatedResources}
      />
    </div>
  );
}
