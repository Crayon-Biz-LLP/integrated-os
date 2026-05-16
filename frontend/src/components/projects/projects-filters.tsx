'use client';

import { Search, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { ProjectFilters as ProjectFiltersType } from '@/lib/projects/types';

interface ProjectsFiltersProps {
  filters: ProjectFiltersType;
  onFiltersChange: (filters: ProjectFiltersType) => void;
}

const orgTags = [
  { value: 'all', label: 'All' },
  { value: 'SOLVSTRAT', label: 'SOLVSTRAT' },
  { value: 'ASHRAYA', label: 'ASHRAYA' },
  { value: 'PERSONAL', label: 'PERSONAL' },
  { value: 'PRODUCT_LABS', label: 'PRODUCT_LABS' },
  { value: 'INBOX', label: 'INBOX' },
  { value: 'ADMIN', label: 'ADMIN' },
];

const contexts = [
  { value: 'all', label: 'All' },
  { value: 'work', label: 'Work' },
  { value: 'personal', label: 'Personal' },
  { value: 'admin', label: 'Admin' },
];

const statuses = [
  { value: 'active', label: 'Active' },
  { value: 'archived', label: 'Archived' },
  { value: 'all', label: 'All' },
];

export function ProjectsFilters({ filters, onFiltersChange }: ProjectsFiltersProps) {
  const handleFilterChange = <K extends keyof ProjectFiltersType>(
    key: K,
    value: ProjectFiltersType[K]
  ) => {
    onFiltersChange({ ...filters, [key]: value });
  };

  const hasActiveFilters =
    filters.search ||
    filters.orgTag !== 'all' ||
    filters.context !== 'all' ||
    filters.status !== 'all';

  const clearFilters = () => {
    onFiltersChange({
      search: '',
      orgTag: 'all',
      context: 'all',
      status: 'all',
    });
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search projects..."
          value={filters.search || ''}
          onChange={(e) => handleFilterChange('search', e.target.value)}
          className="w-full rounded-lg border border-border bg-background px-4 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-150"
        />
        </div>

        <select
          value={filters.orgTag || 'all'}
          onChange={(e) => handleFilterChange('orgTag', e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          {orgTags.map((tag) => (
            <option key={tag.value} value={tag.value}>
              {tag.label}
            </option>
          ))}
        </select>

        <select
          value={filters.context || 'all'}
          onChange={(e) => handleFilterChange('context', e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          {contexts.map((ctx) => (
            <option key={ctx.value} value={ctx.value}>
              {ctx.label}
            </option>
          ))}
        </select>

        <select
          value={filters.status || 'all'}
          onChange={(e) => handleFilterChange('status', e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          {statuses.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
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