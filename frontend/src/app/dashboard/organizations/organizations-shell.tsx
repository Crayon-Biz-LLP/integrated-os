'use client';

import { useState, useMemo } from 'react';
import { Search, Building2, Calendar, CheckSquare } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';

interface OrgDisplay {
  id: string;
  name: string;
  is_active: boolean;
  created_at: string | null;
  open_task_count: number;
}

interface OrganizationsShellProps {
  initialOrgs: OrgDisplay[];
}

function formatSinceDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const date = new Date(dateStr);
  return date.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
}

export function OrganizationsShell({ initialOrgs }: OrganizationsShellProps) {
  const [search, setSearch] = useState('');

  const filteredOrgs = useMemo(() => {
    if (!search) return initialOrgs;
    const q = search.toLowerCase();
    return initialOrgs.filter((org) => org.name.toLowerCase().includes(q));
  }, [initialOrgs, search]);

  const totalOpenTasks = initialOrgs.reduce((sum, org) => sum + org.open_task_count, 0);

  return (
    <div className="p-4 md:p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">Organizations</h1>
        <p className="text-sm text-muted-foreground/70 mt-0.5">All clients and organizations with active work</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="card-premium p-4">
          <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
            <Building2 className="h-4 w-4" />
            Total Organizations
          </div>
          <p className="text-2xl font-bold tracking-tight">{initialOrgs.length}</p>
        </div>
        <div className="card-premium p-4">
          <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
            <CheckSquare className="h-4 w-4" />
            Open Tasks
          </div>
          <p className="text-2xl font-bold tracking-tight">{totalOpenTasks}</p>
        </div>
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          placeholder="Search organizations..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full rounded-lg border border-border bg-background px-4 py-2 pl-10 text-sm placeholder:text-muted-foreground/50"
        />
      </div>

      {/* Orgs list */}
      {filteredOrgs.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground">
          {search ? 'No organizations match your search.' : 'No organizations found.'}
        </div>
      ) : (
        <div className="space-y-3">
          {filteredOrgs.map((org) => (
            <div
              key={org.id}
              className="card-premium p-4 flex items-center justify-between group hover:bg-primary/3 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <h3 className="font-semibold text-base tracking-tight truncate">
                  {org.name}
                </h3>
                <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground/70">
                  {org.created_at && (
                    <span className="flex items-center gap-1">
                      <Calendar className="h-3 w-3" />
                      Since {formatSinceDate(org.created_at)}
                    </span>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-3 shrink-0 ml-4">
                <span className={`text-sm font-mono ${org.open_task_count > 0 ? 'text-primary font-semibold' : 'text-muted-foreground/60'}`}>
                  {org.open_task_count} open task{org.open_task_count !== 1 ? 's' : ''}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}