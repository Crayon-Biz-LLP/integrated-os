'use client';

import { Button } from '@/components/ui/button';
import { LayoutGrid, List } from 'lucide-react';

interface ResourcesViewToggleProps {
  view: 'mission' | 'library';
  setView: (v: 'mission' | 'library') => void;
}

export function ResourcesViewToggle({ view, setView }: ResourcesViewToggleProps) {
  return (
    <div className="flex items-center gap-1">
      <Button
        variant={view === 'mission' ? 'default' : 'outline'}
        size="sm"
        onClick={() => setView('mission')}
        className="gap-2"
      >
        <LayoutGrid className="h-4 w-4" />
        Mission View
      </Button>
      <Button
        variant={view === 'library' ? 'default' : 'outline'}
        size="sm"
        onClick={() => setView('library')}
        className="gap-2"
      >
        <List className="h-4 w-4" />
        Library View
      </Button>
    </div>
  );
}
