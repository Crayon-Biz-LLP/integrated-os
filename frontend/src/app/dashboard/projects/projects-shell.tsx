'use client';

import { useState, useMemo } from 'react';
import type { Project, ProjectFilters as ProjectFiltersType, ProjectStats } from '@/lib/projects/types';
import { ProjectsHeader } from '@/components/projects/projects-header';
import { ProjectsStats } from '@/components/projects/projects-stats';
import { ProjectsFilters } from '@/components/projects/projects-filters';
import { ProjectsGrid } from '@/components/projects/projects-grid';
import { ProjectDetailSheet } from '@/components/projects/project-detail-sheet';

const defaultFilters: ProjectFiltersType = {
  search: '',
  orgTag: 'all',
  context: 'all',
  status: 'active',
};

function filterProjects(projects: Project[], filters: ProjectFiltersType): Project[] {
  return projects.filter((p) => {
    if (filters.search && !p.name.toLowerCase().includes(filters.search.toLowerCase())) return false;
    if (filters.orgTag && filters.orgTag !== 'all' && p.org_tag !== filters.orgTag) return false;
    if (filters.context && filters.context !== 'all' && p.context !== filters.context) return false;
    if (filters.status && filters.status !== 'all' && p.status !== filters.status) return false;
    return true;
  });
}

export function ProjectsShell({
  initialProjects,
  initialStats,
}: {
  initialProjects: Project[];
  initialStats: ProjectStats;
}) {
  const [filters, setFilters] = useState<ProjectFiltersType>(defaultFilters);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [detailSheetOpen, setDetailSheetOpen] = useState(false);

  const filteredProjects = useMemo(
    () => filterProjects(initialProjects, filters),
    [initialProjects, filters]
  );

  const handleProjectClick = (project: Project) => {
    setSelectedProject(project);
    setDetailSheetOpen(true);
  };

  const handleStatusChange = (updatedProject: Project) => {
    setSelectedProject(updatedProject);
  };

  return (
    <div className="p-4 md:p-6">
      <ProjectsHeader />

      <ProjectsStats stats={initialStats} loading={false} />

      <div className="mt-6">
        <ProjectsFilters filters={filters} onFiltersChange={setFilters} />
      </div>

      <div className="mt-4">
        <ProjectsGrid
          projects={filteredProjects}
          onProjectClick={handleProjectClick}
        />
      </div>

      <ProjectDetailSheet
        project={selectedProject}
        open={detailSheetOpen}
        onOpenChange={setDetailSheetOpen}
        onStatusChange={handleStatusChange}
      />
    </div>
  );
}
