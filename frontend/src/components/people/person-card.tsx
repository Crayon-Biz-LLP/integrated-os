'use client';

import { Person } from '@/lib/people/types';
import { Calendar } from 'lucide-react';

interface PersonCardProps {
  person: Person;
  onClick: (person: Person) => void;
}

function getWeightColor(weight: number | null): string {
  const base = 'text-sm font-bold tabular-nums';
  if (!weight) return `${base} text-muted-foreground`;
  if (weight >= 8) return `${base} text-primary`;
  if (weight >= 5) return `${base} text-amber-500`;
  return `${base} text-muted-foreground`;
}

function getWeightBadge(weight: number | null): string {
  if (!weight) return '—';
  return `${weight}/10`;
}

function getCardClass(weight: number | null, activeTaskCount: number): string {
  return 'card-premium p-4 flex flex-col gap-2 cursor-pointer group';
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
          <h3 className="font-semibold text-base tracking-tight group-hover:text-primary transition-colors duration-150 truncate">{person.name}</h3>
          {person.role && (
            <p className="text-xs text-muted-foreground/70 mt-0.5 truncate">{person.role}</p>
          )}
        </div>
        <div className={`${weightColor} shrink-0`}>
          {getWeightBadge(person.strategic_weight)}
        </div>
      </div>

      <div className="flex items-center gap-3 mt-3 text-xs text-muted-foreground">
        <span className={person.active_task_count > 0 ? 'text-xs text-primary font-medium font-mono' : 'text-xs text-muted-foreground/50 font-mono'}>
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
