'use client';

import { Person } from '@/lib/people/types';
import { PersonCard } from './person-card';

interface PeopleGridProps {
  people: Person[];
  loading: boolean;
  onPersonClick: (person: Person) => void;
}

export function PeopleGrid({ people, loading, onPersonClick }: PeopleGridProps) {
  if (loading) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <div key={i} className="h-24 rounded-lg border bg-muted/20 animate-pulse" />
        ))}
      </div>
    );
  }

  if (people.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center">
        <p className="text-sm text-muted-foreground">No people found</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
      {people.map((person) => (
        <PersonCard key={person.id} person={person} onClick={onPersonClick} />
      ))}
    </div>
  );
}
