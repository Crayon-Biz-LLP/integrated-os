'use client';

import { useEffect, useState } from 'react';
import { Search, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { TaskFilters as TaskFiltersType, Project } from '@/lib/tasks/types';
import { fetchProjects } from '@/lib/tasks/api';

interface TasksFiltersProps {
  filters: TaskFiltersType;
  onFiltersChange: (filters: TaskFiltersType) => void;
}

export function TasksFilters({ filters, onFiltersChange }: TasksFiltersProps) {
  const [projects, setProjects] = useState<Project[]>([]);

  useEffect(() => {
    fetchProjects().then(setProjects);
  }, []);

  const handleFilterChange = <K extends keyof TaskFiltersType>(
    key: K,
    value: TaskFiltersType[K]
  ) => {
    onFiltersChange({ ...filters, [key]: value });
  };

  const hasActiveFilters =
    filters.search ||
    filters.status !== 'all' ||
    filters.priority !== 'all' ||
    filters.projectId !== 'all' ||
    filters.dueWindow !== 'all';

  const clearFilters = () => {
    onFiltersChange({
      search: '',
      status: 'all',
      priority: 'all',
      projectId: 'all',
      dueWindow: 'all',
    });
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search tasks..."
            value={filters.search || ''}
            onChange={(e) => handleFilterChange('search', e.target.value)}
            className="pl-9"
          />
        </div>

        <select
          value={filters.status || 'all'}
          onChange={(e) => handleFilterChange('status', e.target.value)}
          className="h-8 rounded-lg border bg-background px-2.5 text-sm"
        >
          <option value="all">All Status</option>
          <option value="todo">Todo</option>
          <option value="in_progress">In Progress</option>
          <option value="done">Done</option>
          <option value="blocked">Blocked</option>
          <option value="cancelled">Cancelled</option>
        </select>

        <select
          value={filters.priority || 'all'}
          onChange={(e) => handleFilterChange('priority', e.target.value)}
          className="h-8 rounded-lg border bg-background px-2.5 text-sm"
        >
          <option value="all">All Priority</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="urgent">Urgent</option>
        </select>

        <select
          value={filters.projectId || 'all'}
          onChange={(e) => handleFilterChange('projectId', e.target.value)}
          className="h-8 rounded-lg border bg-background px-2.5 text-sm"
        >
          <option value="all">All Projects</option>
          {projects.map((p) => (
            <option key={p.id} value={String(p.id)}>
              {p.name}
            </option>
          ))}
        </select>

        <select
          value={filters.dueWindow || 'all'}
          onChange={(e) => handleFilterChange('dueWindow', e.target.value)}
          className="h-8 rounded-lg border bg-background px-2.5 text-sm"
        >
          <option value="all">All Dates</option>
          <option value="today">Due Today</option>
          <option value="upcoming">Upcoming</option>
          <option value="overdue">Overdue</option>
        </select>

        {hasActiveFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters}>
            <X className="h-4 w-4 mr-1" />
            Clear
          </Button>
        )}
      </div>
    </div>
  );
}