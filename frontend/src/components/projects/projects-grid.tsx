'use client';

import { Project } from '@/lib/projects/types';
import { ProjectCard } from './project-card';
import { Separator } from '@/components/ui/separator';

interface ProjectsGridProps {
  projects: Project[];
  onProjectClick: (project: Project) => void;
}

const groupOrder = ['SOLVSTRAT', 'PRODUCT_LABS', 'CHURCH', 'PERSONAL', 'ADMIN', 'INBOX', null];

const groupLabels: Record<string, string> = {
  SOLVSTRAT: 'SOLVSTRAT',
  PRODUCT_LABS: 'PRODUCT_LABS',
  CHURCH: 'CHURCH',
  PERSONAL: 'PERSONAL',
  ADMIN: 'ADMIN',
  INBOX: 'INBOX',
  'null': 'No Area',
};

export function ProjectsGrid({ projects, onProjectClick }: ProjectsGridProps) {
  const groups: Record<string, Project[]> = {};
  const ungrouped: Project[] = [];

  projects.forEach((project) => {
    const key = project.org_tag ?? 'null';
    if (key === 'null') {
      ungrouped.push(project);
    } else {
      if (!groups[key]) groups[key] = [];
      groups[key].push(project);
    }
  });

  const sortedGroups = Object.entries(groups)
    .sort(([a], [b]) => {
      const indexA = groupOrder.indexOf(a);
      const indexB = groupOrder.indexOf(b);
      return (indexA === -1 ? 999 : indexA) - (indexB === -1 ? 999 : indexB);
    });

  if (ungrouped.length > 0) {
    sortedGroups.push(['null', ungrouped]);
  }

  if (sortedGroups.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        No projects found
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {sortedGroups.map(([groupKey, groupProjects]) => (
        <div key={groupKey}>
          <div className="flex items-center gap-3 mb-4">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              {groupLabels[groupKey] || groupKey}
            </h2>
            <Separator className="flex-1" />
            <span className="text-xs text-muted-foreground">
              {groupProjects.length} project{groupProjects.length !== 1 ? 's' : ''}
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {groupProjects.map((project) => (
              <ProjectCard
                key={project.id}
                project={project}
                onClick={onProjectClick}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}