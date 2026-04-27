'use client';

import { useState, useEffect } from 'react';
import {
  Sheet, SheetContent, SheetHeader, SheetTitle,
} from '@/components/ui/sheet';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Person, PersonTask } from '@/lib/people/types';
import { fetchPersonTasks, updatePerson } from '@/lib/people/api';
import { stripMarkdown } from '@/lib/utils/strip-markdown';
import { Loader2, Save } from 'lucide-react';

interface PersonDetailSheetProps {
  person: Person | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPersonUpdated: (updated: Person) => void;
}

export function PersonDetailSheet({ person, open, onOpenChange, onPersonUpdated }: PersonDetailSheetProps) {
  const [tasks, setTasks] = useState<PersonTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [role, setRole] = useState('');
  const [strategicWeight, setStrategicWeight] = useState<number>(5);
  const [saving, setSaving] = useState(false);
  const [hasChanges, setHasChanges] = useState(false);

  useEffect(() => {
    if (!person) return;
    setRole(person.role || '');
    setStrategicWeight(person.strategic_weight || 5);
    setHasChanges(false);
    setTasks([]);

    // Load tasks lazily when sheet opens
    if (open) {
      setTasksLoading(true);
      fetchPersonTasks(person.id, person.name)
        .then((data) => {
          setTasks(data);
          setTasksLoading(false);
        })
        .catch(() => setTasksLoading(false));
    }
  }, [person, open]);

  if (!person) return null;

  const handleSave = async () => {
    setSaving(true);
    try {
      const updates: { role?: string; strategic_weight?: number } = {};
      if (role !== (person.role || '')) updates.role = role;
      if (strategicWeight !== (person.strategic_weight || 5)) updates.strategic_weight = strategicWeight;

      const updated = await updatePerson(person.id, updates);
      onPersonUpdated(updated);
      setHasChanges(false);
    } catch (err) {
      console.error('Failed to update person:', err);
    } finally {
      setSaving(false);
    }
  };

  const priorityOrder: Record<string, number> = {
    urgent: 1, high: 2, important: 3, medium: 4, low: 5,
  };

  const sortedTasks = [...tasks].sort((a, b) => {
    const pDiff = (priorityOrder[a.priority] || 6) - (priorityOrder[b.priority] || 6);
    if (pDiff !== 0) return pDiff;
    return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
  });

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return '—';
    try {
      const date = new Date(dateStr);
      const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      return `${months[date.getMonth()]} ${date.getDate()}, ${date.getFullYear()}`;
    }
    catch { return '—'; }
  };

  const getDueDate = (task: PersonTask) => {
    return task.reminder_at || task.deadline || null;
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="text-base">Person Details</SheetTitle>
        </SheetHeader>

        <div className="mt-6 space-y-6">
          {/* Profile Section */}
          <div className="space-y-4">
            <h3 className="text-sm font-medium text-muted-foreground">Profile</h3>

            <div>
              <label className="text-xs text-muted-foreground">Name</label>
              <p className="text-sm font-medium mt-1">{person.name}</p>
            </div>

            <div>
              <label className="text-xs text-muted-foreground">Role</label>
              <Input
                value={role}
                onChange={(e) => { setRole(e.target.value); setHasChanges(true); }}
                placeholder="e.g. Collaborator, Family..."
                className="mt-1"
              />
            </div>

            <div>
              <label className="text-xs text-muted-foreground">Strategic Weight (1-10)</label>
              <div className="flex items-center gap-3 mt-1">
                <Input
                  type="number"
                  min={1}
                  max={10}
                  value={strategicWeight}
                  onChange={(e) => { setStrategicWeight(Number(e.target.value)); setHasChanges(true); }}
                  className="w-20"
                />
                <span className="text-xs text-muted-foreground">/ 10</span>
              </div>
            </div>

            <div>
              <label className="text-xs text-muted-foreground">Added</label>
              <p className="text-sm mt-1">{formatDate(person.created_at)}</p>
            </div>

            {hasChanges && (
              <Button onClick={handleSave} disabled={saving} size="sm" className="w-full">
                {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                Save Changes
              </Button>
            )}
          </div>

          {/* Active Tasks Section */}
          <div className="space-y-3">
            <h3 className="text-sm font-medium text-muted-foreground">
              Active Tasks
              {!tasksLoading && <span className="ml-2 text-xs">({tasks.length})</span>}
            </h3>

            {tasksLoading && (
              <div className="flex items-center justify-center py-4">
                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
              </div>
            )}

            {!tasksLoading && tasks.length === 0 && (
              <p className="text-sm text-muted-foreground">No active tasks mention {person.name}</p>
            )}

            {!tasksLoading && tasks.length > 0 && (
              <div className="space-y-2">
                {sortedTasks.map((task) => (
                  <div key={task.id} className="rounded-lg border p-3 text-sm">
                    <p className="font-medium leading-tight">{stripMarkdown(task.title)}</p>
                    <div className="flex items-center gap-2 mt-2">
                      <Badge variant="secondary" className="text-xs">
                        {task.status}
                      </Badge>
                      <Badge variant="outline" className="text-xs">
                        {task.priority}
                      </Badge>
                      {task.project_name && (
                        <span className="text-xs text-muted-foreground">{task.project_name}</span>
                      )}
                      {getDueDate(task) && (
                        <span className="text-xs text-muted-foreground ml-auto">
                          {formatDate(getDueDate(task))}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
