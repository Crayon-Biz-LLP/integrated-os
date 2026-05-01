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
      <button
        onClick={() => setView('mission')}
        className={view === 'mission' 
          ? "bg-primary text-primary-foreground text-xs px-3 py-1.5 rounded-md font-medium transition-all" 
          : "text-muted-foreground text-xs px-3 py-1.5 rounded-md hover:bg-muted hover:text-foreground transition-all duration-150"
        }
      >
        <LayoutGrid className="h-4 w-4 inline mr-1" />
        Mission View
      </button>
      <button
        onClick={() => setView('library')}
        className={view === 'library' 
          ? "bg-primary text-primary-foreground text-xs px-3 py-1.5 rounded-md font-medium transition-all" 
          : "text-muted-foreground text-xs px-3 py-1.5 rounded-md hover:bg-muted hover:text-foreground transition-all duration-150"
        }
      >
        <List className="h-4 w-4 inline mr-1" />
        Library View
      </button>
    </div>
  );
}
