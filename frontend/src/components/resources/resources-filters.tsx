'use client';

import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Search, SlidersHorizontal } from 'lucide-react';
import { ResourceMission } from '@/lib/resources/types';

interface ResourcesFiltersProps {
  search: string;
  setSearch: (v: string) => void;
  mission: string;
  setMission: (v: string) => void;
  category: string;
  setCategory: (v: string) => void;
  sort: string;
  setSort: (v: string) => void;
  missions: ResourceMission[];
  categories: string[];
}

export function ResourcesFilters({
  search,
  setSearch,
  mission,
  setMission,
  category,
  setCategory,
  sort,
  setSort,
  missions,
  categories,
}: ResourcesFiltersProps) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col sm:flex-row gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search resources..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full rounded-lg border border-border bg-background px-4 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-150 pl-9"
          />
        </div>
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          <option value="newest">Newest</option>
          <option value="oldest">Oldest</option>
          <option value="title">Title</option>
          <option value="category">Category</option>
          <option value="mission">Mission</option>
        </select>
      </div>

      <div className="flex flex-wrap gap-2 items-center">
        <select
          value={mission}
          onChange={(e) => setMission(e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          <option value="all">All Missions</option>
          <option value="unmapped">Unmapped</option>
          {missions.map((m) => (
            <option key={m.id} value={String(m.id)}>
              {m.title}
            </option>
          ))}
        </select>

        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground"
        >
          <option value="all">All Categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>

        {(mission !== 'all' || category !== 'all' || search) && (
          <Badge
            variant="outline"
            className="cursor-pointer h-8"
            onClick={() => {
              setMission('all');
              setCategory('all');
              setSearch('');
            }}
          >
            Clear filters
          </Badge>
        )}
      </div>
    </div>
  );
}
