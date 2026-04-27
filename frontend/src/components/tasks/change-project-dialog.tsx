'use client';

import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { Task, Project } from '@/lib/tasks/types';
import { fetchProjects, updateTaskProject } from '@/lib/tasks/api';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

interface ChangeProjectDialogProps {
  task: Task | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: (updatedTask: Task) => void;
}

export function ChangeProjectDialog({ task, open, onOpenChange, onSuccess }: ChangeProjectDialogProps) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [search, setSearch] = useState('');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      fetchProjects().then((data) => {
        setProjects(data);
        if (task) {
          setSelectedId(task.project_id);
        }
        setSearch('');
      });
    }
  }, [open, task]);

  const filteredProjects = search
    ? projects.filter((p) => p.name.toLowerCase().includes(search.toLowerCase()))
    : projects;

  const handleSave = async () => {
    if (!task) return;
    
    setSaving(true);
    try {
      await updateTaskProject(task.id, selectedId);
      const updatedProject = projects.find(p => p.id === selectedId);
      onSuccess({
        ...task,
        project_id: selectedId,
        project_name: updatedProject?.name ?? 'Inbox',
        project_org_tag: updatedProject?.org_tag ?? null,
      });
    } catch (error) {
      console.error('Failed to update task project:', error);
    } finally {
      setSaving(false);
      onOpenChange(false);
    }
  };

  if (!task) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Change Project</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <div>
            <p className="text-sm text-muted-foreground mb-2">Task</p>
            <p className="text-sm font-medium line-clamp-2">{task.title}</p>
          </div>

          <div>
            <p className="text-sm text-muted-foreground mb-2">Current Project</p>
            <p className="text-sm">{task.project_name}</p>
          </div>

          <div>
            <p className="text-sm text-muted-foreground mb-2">Select New Project</p>
            <Input
              placeholder="Search projects..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="mb-2"
            />
            <div className="max-h-48 overflow-y-auto rounded-md border">
              {filteredProjects.map((project) => (
                <button
                  key={project.id}
                  onClick={() => setSelectedId(project.id)}
                  className={`w-full px-3 py-2 text-left text-sm hover:bg-muted focus:bg-muted ${
                    selectedId === project.id ? 'bg-accent' : ''
                  }`}
                >
                  {project.name}
                </button>
              ))}
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleSave} disabled={saving || selectedId === task.project_id}>
            {saving ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                Saving...
              </>
            ) : (
              'Save'
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}