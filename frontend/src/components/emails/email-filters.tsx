'use client';

import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Search } from 'lucide-react';
import type { EmailFilters, EmailClassification, EmailSource } from '@/lib/emails/types';

interface EmailFiltersProps {
  filters: EmailFilters;
  onFiltersChange: (filters: EmailFilters) => void;
}

export function EmailFilters({ filters, onFiltersChange }: EmailFiltersProps) {
  const updateFilter = <K extends keyof EmailFilters>(key: K, value: EmailFilters[K]) => {
    onFiltersChange({ ...filters, [key]: value });
  };

  return (
    <div className="flex flex-col md:flex-row gap-4 mb-6">
      <div className="relative flex-1">
        <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          placeholder="Search subject, sender..."
          value={filters.search}
          onChange={(e) => updateFilter('search', e.target.value)}
          className="w-full rounded-lg border border-border bg-background px-4 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-150"
        />
      </div>
      <Select
        value={filters.classification}
        onValueChange={(v) => updateFilter('classification', v as EmailClassification | 'all')}
      >
        <SelectTrigger className="w-[180px] rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground">
          <SelectValue placeholder="Classification" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All Classifications</SelectItem>
          <SelectItem value="actionable">Actionable</SelectItem>
          <SelectItem value="fyi">FYI</SelectItem>
          <SelectItem value="ignored">Ignored</SelectItem>
        </SelectContent>
      </Select>
      <Select
        value={filters.source}
        onValueChange={(v) => updateFilter('source', v as EmailSource | 'all')}
      >
        <SelectTrigger className="w-[180px] rounded-lg border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-2 focus:ring-primary/20 transition-all text-foreground">
          <SelectValue placeholder="Source" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All Sources</SelectItem>
          <SelectItem value="gmail">Gmail</SelectItem>
          <SelectItem value="outlook">Outlook</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}
