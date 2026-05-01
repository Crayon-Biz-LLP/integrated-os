'use client';

import { Users } from 'lucide-react';

export function PeopleHeader() {
  return (
    <div className="flex items-center gap-3">
      <Users className="h-6 w-6 text-muted-foreground" />
      <div>
        <h1 className="text-2xl font-bold tracking-tight">People</h1>
        <p className="text-sm text-muted-foreground/70 mt-0.5">Relationships across work and life</p>
      </div>
    </div>
  );
}
