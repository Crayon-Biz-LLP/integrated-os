'use client';

import { useEffect, useState } from 'react';
import { Person, PeopleFilters } from '@/lib/people/types';
import { fetchPeople } from '@/lib/people/api';
import { PeopleHeader } from '@/components/people/people-header';
import { PeopleStats } from '@/components/people/people-stats';
import { PeopleFilters as PeopleFiltersComponent } from '@/components/people/people-filters';
import { PeopleGrid } from '@/components/people/people-grid';
import { PersonDetailSheet } from '@/components/people/person-detail-sheet';

const defaultFilters: PeopleFilters = {
  search: '',
  tier: 'all',
  sort: 'strategic_weight',
};

export default function PeoplePage() {
  const [people, setPeople] = useState<Person[]>([]);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<PeopleFilters>(defaultFilters);
  const [selectedPerson, setSelectedPerson] = useState<Person | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetchPeople(filters).then((data) => {
      setPeople(data);
      setLoading(false);
    });
  }, [filters]);

  const handlePersonClick = (person: Person) => {
    setSelectedPerson(person);
    setDetailOpen(true);
  };

  const handlePersonUpdated = (updated: Person) => {
    setPeople((prev) =>
      prev.map((p) => (p.id === updated.id ? { ...p, ...updated } : p))
    );
    setSelectedPerson(updated);
  };

  return (
    <div className="space-y-6 p-4 md:p-6">
      <PeopleHeader />
      <PeopleStats />
      <PeopleFiltersComponent filters={filters} onFiltersChange={setFilters} />
      <PeopleGrid
        people={people}
        loading={loading}
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
