'use client';

import { useState, useMemo, useCallback } from 'react';
import { ResourcesHeader } from '@/components/resources/resources-header';
import { ResourcesStats } from '@/components/resources/resources-stats';
import { ResourcesFilters } from '@/components/resources/resources-filters';
import { ResourcesViewToggle } from '@/components/resources/resources-view-toggle';
import { ResourcesClusterGroups } from '@/components/resources/resources-cluster-groups';
import { ResourcesLibraryGrid } from '@/components/resources/resources-library-grid';
import { ResourceDetailSheet } from '@/components/resources/resource-detail-sheet';
import type { Resource, ResourceCluster, ResourceStats, ResourceFilters as FiltersType } from '@/lib/resources/types';
import { updateResourceCluster, fetchResource, fetchRelatedResources } from '@/lib/resources/api';

function filterResources(resources: Resource[], filters: FiltersType, clusters: ResourceCluster[]): Resource[] {
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

  if (filters.cluster && filters.cluster !== 'all') {
    if (filters.cluster === 'unmapped') {
      result = result.filter((r) => r.cluster_id === null);
    } else {
      result = result.filter((r) => String(r.cluster_id) === filters.cluster);
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
    case 'cluster':
      result.sort((a, b) => (a.cluster_id || 0) - (b.cluster_id || 0));
      break;
    default:
      result.sort((a, b) => new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime());
  }

  return result;
}

export function ResourcesShell({
  initialResources,
  initialClusters,
  initialStats,
}: {
  initialResources: Resource[];
  initialClusters: ResourceCluster[];
  initialStats: ResourceStats;
}) {
  const [resources] = useState(initialResources);
  const [selectedResource, setSelectedResource] = useState<Resource | null>(null);
  const [relatedResources, setRelatedResources] = useState<Resource[]>([]);
  const [detailOpen, setDetailOpen] = useState(false);

  const [filters, setFilters] = useState<FiltersType>({
    search: '',
    cluster: 'all',
    category: 'all',
    sort: 'newest',
    view: 'cluster',
  });

  const categories = useMemo(
    () => Array.from(new Set(resources.map(r => r.category).filter(Boolean))) as string[],
    [resources]
  );

  const filteredResources = useMemo(
    () => filterResources(resources, filters, initialClusters),
    [resources, filters, initialClusters]
  );

  const handleResourceClick = useCallback(async (resource: Resource) => {
    setSelectedResource(resource);
    setDetailOpen(true);

    if (resource.cluster_id) {
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

  const handleClusterChange = useCallback(async (resourceId: number, clusterId: number | null) => {
    try {
      await updateResourceCluster(resourceId, clusterId);
      if (selectedResource?.id === resourceId) {
        const updated = await fetchResource(resourceId);
        setSelectedResource(updated);
        if (updated.cluster_id) {
          const related = await fetchRelatedResources(resourceId);
          setRelatedResources(related);
        } else {
          setRelatedResources([]);
        }
      }
    } catch (err: any) {
      console.error('Failed to update cluster:', err);
      alert('Failed to update cluster: ' + (err.message || 'Unknown error'));
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
            cluster={filters.cluster || 'all'}
            setCluster={(v) => setFilters(f => ({ ...f, cluster: v }))}
            category={filters.category || 'all'}
            setCategory={(v) => setFilters(f => ({ ...f, category: v }))}
            sort={filters.sort || 'newest'}
            setSort={(v) => setFilters(f => ({ ...f, sort: v }))}
            clusters={initialClusters}
            categories={categories}
          />
          <ResourcesViewToggle
            view={filters.view || 'cluster'}
            setView={(v) => setFilters(f => ({ ...f, view: v }))}
          />
        </div>

        {filters.view === 'cluster' ? (
          <ResourcesClusterGroups
            resources={filteredResources}
            clusters={initialClusters}
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
        clusters={initialClusters}
        onClusterChange={handleClusterChange}
        relatedResources={relatedResources}
      />
    </div>
  );
}
