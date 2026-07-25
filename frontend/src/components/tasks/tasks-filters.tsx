'use client';

import { Search, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { useEffect, useState } from 'react';
import type { TaskFilters as TaskFiltersType } from '@/lib/tasks/types';
import { fetchOrganizations } from '@/lib/tasks/api';

interface TasksFiltersProps {
  filters: TaskFiltersType;
  onFiltersChange: (filters: TaskFiltersType) => void;
}

export function TasksFilters({ filters, onFiltersChange }: TasksFiltersProps) {
  const [orgs, setOrgs] = useState<{ id: string; name: string }[]>([]);

  useEffect(() => {
    fetchOrganizations().then(setOrgs).catch(() => {});
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
    filters.orgId !== 'all' ||
    filters.dueWindow !== 'all';

  const clearFilters = () => {
    onFiltersChange({
      search: '',
      status: 'all',
      priority: 'all',
      orgId: 'all',
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
            className="w-full rounded-lg border border-border bg-background px-4 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-150"
          />
        </div>

        <select
          value={filters.status || 'all'}
          onChange={(e) => handleFilterChange('status', e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
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
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          <option value="all">All Priority</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="urgent">Urgent</option>
        </select>

        <select
          value={filters.orgId || 'all'}
          onChange={(e) => handleFilterChange('orgId', e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          <option value="all">All Organizations</option>
          {orgs.map((org) => (
            <option key={org.id} value={org.id}>
              {org.name}
            </option>
          ))}
        </select>

        <select
          value={filters.dueWindow || 'all'}
          onChange={(e) => handleFilterChange('dueWindow', e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
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