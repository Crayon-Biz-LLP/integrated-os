'use client';

import { Person } from '@/lib/people/types';
import { Calendar } from 'lucide-react';

interface PersonCardProps {
  person: Person;
  onClick: (person: Person) => void;
}

function getWeightColor(weight: number | null): string {
  if (!weight) return 'text-muted-foreground';
  if (weight >= 9) return 'text-amber-600 dark:text-amber-500';
  if (weight >= 7) return 'text-blue-600 dark:text-blue-500';
  if (weight >= 4) return 'text-muted-foreground';
  return 'text-muted-foreground/60';
}

function getWeightBadge(weight: number | null): string {
  if (!weight) return '—';
  return `${weight}/10`;
}

function getCardClass(weight: number | null, activeTaskCount: number): string {
  const base = 'rounded-lg border bg-card p-4 cursor-pointer transition-all hover:border-muted-foreground/30 hover:bg-muted/20';
  if (!weight || weight < 4) return `${base} opacity-70`;
  if (weight >= 9) return `${base} border-amber-200/50 dark:border-amber-800/30 bg-amber-50/30 dark:bg-amber-950/10`;
  return base;
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—';
  try {
    const date = new Date(dateStr);
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return `${months[date.getMonth()]} ${date.getFullYear()}`;
  } catch {
    return '—';
  }
}

export function PersonCard({ person, onClick }: PersonCardProps) {
  const weightColor = getWeightColor(person.strategic_weight);
  const cardClass = getCardClass(person.strategic_weight, person.active_task_count);

  return (
    <div className={cardClass} onClick={() => onClick(person)}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold truncate">{person.name}</h3>
          {person.role && (
            <p className="text-xs text-muted-foreground mt-0.5 truncate">{person.role}</p>
          )}
        </div>
        <div className={`text-xs font-medium ${weightColor} shrink-0`}>
          {getWeightBadge(person.strategic_weight)}
        </div>
      </div>

      <div className="flex items-center gap-3 mt-3 text-xs text-muted-foreground">
        <span className={person.active_task_count > 0 ? 'text-foreground' : ''}>
          {person.active_task_count} active task{person.active_task_count !== 1 ? 's' : ''}
        </span>
        <span className="flex items-center gap-1">
          <Calendar className="h-3 w-3" />
          Since {formatDate(person.created_at)}
        </span>
      </div>
    </div>
  );
}
