'use client';

import { useEffect, useState, useCallback } from 'react';
import { ResourcesHeader } from '@/components/resources/resources-header';
import { ResourcesStats } from '@/components/resources/resources-stats';
import { ResourcesFilters } from '@/components/resources/resources-filters';
import { ResourcesViewToggle } from '@/components/resources/resources-view-toggle';
import { ResourcesMissionGroups } from '@/components/resources/resources-mission-groups';
import { ResourcesLibraryGrid } from '@/components/resources/resources-library-grid';
import { ResourceDetailSheet } from '@/components/resources/resource-detail-sheet';
import { Resource, ResourceMission, ResourceFilters as FiltersType } from '@/lib/resources/types';
import { 
  fetchResources, 
  fetchResourceMissions, 
  fetchResource,
  fetchRelatedResources,
  updateResourceMission 
} from '@/lib/resources/api';

export default function ResourcesPage() {
  const [resources, setResources] = useState<Resource[]>([]);
  const [missions, setMissions] = useState<ResourceMission[]>([]);
  const [loading, setLoading] = useState(true);
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

  const categories = Array.from(
    new Set(resources.map(r => r.category).filter(Boolean))
  ) as string[];

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [resourcesData, missionsData] = await Promise.all([
        fetchResources(filters),
        fetchResourceMissions(),
      ]);
      setResources(resourcesData);
      setMissions(missionsData);
    } catch (error) {
      console.error('Failed to load resources:', error);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleResourceClick = async (resource: Resource) => {
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
  };

  const handleMissionChange = async (resourceId: number, missionId: number | null) => {
    try {
      await updateResourceMission(resourceId, missionId);
      await loadData();
      
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
    } catch (error) {
      console.error('Failed to update mission:', error);
    }
  };

  return (
    <div className="flex flex-col gap-6 p-8">
      <ResourcesHeader />
      <ResourcesStats />

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
            missions={missions}
            categories={categories}
          />
          <ResourcesViewToggle
            view={filters.view || 'mission'}
            setView={(v) => setFilters(f => ({ ...f, view: v }))}
          />
        </div>

        {loading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {[1,2,3,4,5,6].map(i => (
              <div key={i} className="h-32 rounded-lg border bg-muted/20 animate-pulse" />
            ))}
          </div>
        ) : (
          <>
            {filters.view === 'mission' ? (
              <ResourcesMissionGroups
                resources={resources}
                missions={missions}
                onResourceClick={handleResourceClick}
              />
            ) : (
              <ResourcesLibraryGrid
                resources={resources}
                onResourceClick={handleResourceClick}
              />
            )}
          </>
        )}
      </div>

      <ResourceDetailSheet
        resource={selectedResource}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        missions={missions}
        onMissionChange={handleMissionChange}
        relatedResources={relatedResources}
      />
    </div>
  );
}
