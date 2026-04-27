'use client';

import { useEffect, useState } from 'react';
import { Project, ProjectFilters } from '@/lib/projects/types';
import { fetchProjects } from '@/lib/projects/api';
import { ProjectsHeader } from '@/components/projects/projects-header';
import { ProjectsStats } from '@/components/projects/projects-stats';
import { ProjectsFilters } from '@/components/projects/projects-filters';
import { ProjectsGrid } from '@/components/projects/projects-grid';
import { ProjectDetailSheet } from '@/components/projects/project-detail-sheet';

const defaultFilters: ProjectFilters = {
  search: '',
  orgTag: 'all',
  context: 'all',
  status: 'all',
};

export default function Page() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [filters, setFilters] = useState<ProjectFilters>(defaultFilters);

  const [selectedProject, setSelectedProject] = useState<Project | null>(null);
  const [detailSheetOpen, setDetailSheetOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    
    const load = async () => {
      setIsLoading(true);
      try {
        const data = await fetchProjects(filters);
        if (!cancelled) {
          setProjects(data);
        }
      } catch (error) {
        if (!cancelled) {
          console.error('Failed to fetch projects:', error);
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    };
    
    load();
    
    return () => {
      cancelled = true;
    };
  }, [filters]);

  const handleProjectClick = (project: Project) => {
    setSelectedProject(project);
    setDetailSheetOpen(true);
  };

  const handleStatusChange = (updatedProject: Project) => {
    setProjects((prev) =>
      prev.map((p) => (p.id === updatedProject.id ? updatedProject : p))
    );
    setSelectedProject(updatedProject);
  };

  return (
    <div className="p-4 md:p-6">
      <ProjectsHeader />

      <ProjectsStats />

      <div className="mt-6">
        <ProjectsFilters filters={filters} onFiltersChange={setFilters} />
      </div>

      <div className="mt-4">
        {isLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {[1, 2, 3, 4, 5, 6].map((i) => (
              <div
                key={i}
                className="h-36 rounded-lg border bg-muted/20 animate-pulse"
              />
            ))}
          </div>
        ) : (
          <ProjectsGrid
            projects={projects}
            onProjectClick={handleProjectClick}
          />
        )}
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