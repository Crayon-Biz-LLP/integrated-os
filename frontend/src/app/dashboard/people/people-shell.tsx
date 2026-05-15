'use client';

import { useState, useMemo } from 'react';
import type { Person, PeopleFilters as PeopleFiltersType, PeopleStats as PeopleStatsType } from '@/lib/people/types';
import { PeopleHeader } from '@/components/people/people-header';
import { PeopleStats } from '@/components/people/people-stats';
import { PeopleFilters as PeopleFiltersComponent } from '@/components/people/people-filters';
import { PeopleGrid } from '@/components/people/people-grid';
import { PersonDetailSheet } from '@/components/people/person-detail-sheet';

const defaultFilters: PeopleFiltersType = {
  search: '',
  tier: 'all',
  sort: 'strategic_weight',
};

function filterPeople(people: Person[], filters: PeopleFiltersType): Person[] {
  let result = [...people];

  if (filters.search) {
    const q = filters.search.toLowerCase();
    result = result.filter(
      (p) => p.name.toLowerCase().includes(q) || (p.role && p.role.toLowerCase().includes(q))
    );
  }

  if (filters.tier && filters.tier !== 'all') {
    result = result.filter((p) => {
      const weight = p.strategic_weight || 0;
      switch (filters.tier) {
        case 'critical': return weight >= 9;
        case 'high': return weight >= 7 && weight <= 8;
        case 'medium': return weight >= 4 && weight <= 6;
        case 'low': return weight >= 1 && weight <= 3;
        default: return true;
      }
    });
  }

  result.sort((a, b) => {
    switch (filters.sort) {
      case 'strategic_weight':
        return (b.strategic_weight || 0) - (a.strategic_weight || 0);
      case 'name':
        return a.name.localeCompare(b.name);
      case 'recently_added':
        return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
      default:
        return (b.strategic_weight || 0) - (a.strategic_weight || 0);
    }
  });

  return result;
}

export function PeopleShell({
  initialPeople,
  initialStats,
}: {
  initialPeople: Person[];
  initialStats: PeopleStatsType;
}) {
  const [filters, setFilters] = useState<PeopleFiltersType>(defaultFilters);
  const [selectedPerson, setSelectedPerson] = useState<Person | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const filteredPeople = useMemo(
    () => filterPeople(initialPeople, filters),
    [initialPeople, filters]
  );

  const handlePersonClick = (person: Person) => {
    setSelectedPerson(person);
    setDetailOpen(true);
  };

  const handlePersonUpdated = (updated: Person) => {
    setSelectedPerson(updated);
  };

  return (
    <div className="space-y-6 p-4 md:p-6">
      <PeopleHeader />
      <PeopleStats stats={initialStats} loading={false} />
      <PeopleFiltersComponent filters={filters} onFiltersChange={setFilters} />
      <PeopleGrid
        people={filteredPeople}
        loading={false}
        onPersonClick={handlePersonClick}
      />
      <PersonDetailSheet
        person={selectedPerson}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        onPersonUpdated={handlePersonUpdated}
      />
    </div>
  );
}
