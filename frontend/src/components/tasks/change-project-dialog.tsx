'use client';

import { useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { Task, Project } from '@/lib/tasks/types';
import { fetchProjects, fetchOrganizations, updateTaskProject } from '@/lib/tasks/api';
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
  const [organizations, setOrganizations] = useState<{ id: string; name: string }[]>([]);
  const [search, setSearch] = useState('');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selectedOrgId, setSelectedOrgId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      Promise.all([fetchProjects(), fetchOrganizations()]).then(([projData, orgData]) => {
        setProjects(projData);
        setOrganizations(orgData);
        if (task) {
          setSelectedId(task.project_id);
          setSelectedOrgId(task.organization_id || null);
        }
        setSearch('');
      });
    }
  }, [open, task]);

  const orgProjects = selectedOrgId
    ? projects.filter(p => p.organization_id === selectedOrgId)
    : projects.filter(p => !p.organization_id);

  const filteredProjects = search
    ? orgProjects.filter((p) => p.name.toLowerCase().includes(search.toLowerCase()))
    : orgProjects;

  const handleSave = async () => {
    if (!task) return;
    
    setSaving(true);
    try {
      await updateTaskProject(task.id, selectedId, selectedOrgId);
      const updatedProject = projects.find(p => p.id === selectedId);
      const updatedOrg = organizations.find(o => o.id === selectedOrgId);
      onSuccess({
        ...task,
        project_id: selectedId,
        project_name: updatedProject?.name ?? 'Inbox',
        organization_id: selectedOrgId,
        organization_name: updatedOrg?.name ?? null,
      });
    } catch (error) {
      console.error('Failed to update task project:', error);
    } finally {
      setSaving(false);
      onOpenChange(false);
    }
  };

  const hasChanges = task?.project_id !== selectedId || (task?.organization_id || null) !== selectedOrgId;

  if (!task) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Change Organization & Project</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <div>
            <p className="text-sm text-muted-foreground mb-2">Organization</p>
            <select
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-base shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm mb-2"
              value={selectedOrgId || ''}
              onChange={(e) => {
                const newOrgId = e.target.value || null;
                setSelectedOrgId(newOrgId);
                setSelectedId(null);
              }}
            >
              <option value="">No Organization</option>
              {organizations.map(org => (
                <option key={org.id} value={org.id}>{org.name}</option>
              ))}
            </select>
          </div>

          <div>
            <p className="text-sm text-muted-foreground mb-2">Project</p>
            <Input
              placeholder="Search projects..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="mb-2"
            />
            <div className="max-h-48 overflow-y-auto rounded-md border">
              {filteredProjects.length === 0 && (
                <div className="p-3 text-sm text-muted-foreground text-center">
                  No projects found.
                </div>
              )}
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
          <Button onClick={handleSave} disabled={saving || !hasChanges}>
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