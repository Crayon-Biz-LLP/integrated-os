'use client';

import { Search, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { PeopleFilters as PeopleFiltersType } from '@/lib/people/types';

interface PeopleFiltersProps {
  filters: PeopleFiltersType;
  onFiltersChange: (filters: PeopleFiltersType) => void;
}

const tierOptions = [
  { value: 'all', label: 'All Tiers' },
  { value: 'critical', label: 'Critical (9-10)' },
  { value: 'high', label: 'High (7-8)' },
  { value: 'medium', label: 'Medium (4-6)' },
  { value: 'low', label: 'Low (1-3)' },
];

const sortOptions = [
  { value: 'strategic_weight', label: 'Strategic Weight' },
  { value: 'name', label: 'Name' },
  { value: 'recently_added', label: 'Recently Added' },
];

export function PeopleFilters({ filters, onFiltersChange }: PeopleFiltersProps) {
  const handleFilterChange = <K extends keyof PeopleFiltersType>(
    key: K,
    value: PeopleFiltersType[K]
  ) => {
    onFiltersChange({ ...filters, [key]: value });
  };

  const hasActiveFilters = filters.search || (filters.tier && filters.tier !== 'all') || (filters.sort && filters.sort !== 'strategic_weight');

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search by name or role..."
            value={filters.search || ''}
            onChange={(e) => handleFilterChange('search', e.target.value)}
            className="pl-9"
          />
        </div>
        <select
          value={filters.tier || 'all'}
          onChange={(e) => handleFilterChange('tier', e.target.value)}
          className="h-8 rounded-lg border bg-background px-2.5 text-sm"
        >
          {tierOptions.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
        <select
          value={filters.sort || 'strategic_weight'}
          onChange={(e) => handleFilterChange('sort', e.target.value)}
          className="h-8 rounded-lg border bg-background px-2.5 text-sm"
        >
          {sortOptions.map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
        {hasActiveFilters && (
          <button
            onClick={() => onFiltersChange({ search: '', tier: 'all', sort: 'strategic_weight' })}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <X className="h-3 w-3" />
            Clear
          </button>
        )}
      </div>
    </div>
  );
}
