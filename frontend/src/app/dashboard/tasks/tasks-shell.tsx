'use client';

import { useState, useMemo, useCallback } from 'react';
import type { Task, TaskFilters as TaskFiltersType, Project, TaskStats } from '@/lib/tasks/types';
import { markTaskDone } from '@/lib/tasks/api';
import { TasksStats } from '@/components/tasks/tasks-stats';
import { TasksFilters } from '@/components/tasks/tasks-filters';
import { TasksTable } from '@/components/tasks/tasks-table';
import { TaskDetailSheet } from '@/components/tasks/task-detail-sheet';
import { ChangeProjectDialog } from '@/components/tasks/change-project-dialog';

const defaultFilters: TaskFiltersType = {
  search: '',
  status: 'all',
  priority: 'all',
  projectId: 'all',
  dueWindow: 'all',
};

function matchDueWindow(task: Task, dueWindow: string): boolean {
  const now = new Date();
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const tomorrow = new Date(today.getTime() + 86400000);

  if (["done", "cancelled"].includes(task.status)) return false;
  const dueDate = task.reminder_at || task.deadline;
  if (!dueDate) return false;
  const d = new Date(dueDate);

  if (dueWindow === "today") return d >= today && d < tomorrow;
  if (dueWindow === "overdue") return d < now;
  if (dueWindow === "upcoming") return d >= tomorrow;
  return true;
}

function filterTasks(tasks: Task[], filters: TaskFiltersType): Task[] {
  return tasks.filter((task) => {
    if (filters.search) {
      const q = filters.search.toLowerCase();
      if (!task.title.toLowerCase().includes(q)) return false;
    }
    if (filters.status && filters.status !== "all" && task.status !== filters.status) return false;
    if (filters.priority && filters.priority !== "all" && task.priority !== filters.priority) return false;
    if (filters.projectId && filters.projectId !== "all" && String(task.project_id) !== filters.projectId) return false;
    if (filters.dueWindow && filters.dueWindow !== "all" && !matchDueWindow(task, filters.dueWindow)) return false;
    return true;
  });
}

export function TasksShell({
  initialTasks,
  initialStats,
  projects,
}: {
  initialTasks: Task[];
  initialStats: TaskStats;
  projects: Project[];
}) {
  const [tasks] = useState(initialTasks);
  const [doneTaskIds, setDoneTaskIds] = useState<Set<number>>(new Set());
  const [filters, setFilters] = useState<TaskFiltersType>(defaultFilters);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [detailSheetOpen, setDetailSheetOpen] = useState(false);
  const [changeProjectDialogOpen, setChangeProjectDialogOpen] = useState(false);

  const visibleTasks = useMemo(
    () => filterTasks(tasks.filter((t) => !doneTaskIds.has(t.id)), filters),
    [tasks, doneTaskIds, filters]
  );

  const handleTaskClick = useCallback((task: Task) => {
    setSelectedTask(task);
    setDetailSheetOpen(true);
  }, []);

  const handleChangeProjectClick = useCallback((task: Task) => {
    setSelectedTask(task);
    setDetailSheetOpen(false);
    setChangeProjectDialogOpen(true);
  }, []);

  const handleDetailChangeProjectClick = useCallback(() => {
    setDetailSheetOpen(false);
    setChangeProjectDialogOpen(true);
  }, []);

  const handleProjectUpdated = useCallback((updatedTask: Task) => {
    setSelectedTask(updatedTask);
  }, []);

  const handleTaskDone = useCallback(async (task: Task) => {
    setDoneTaskIds((prev) => new Set(prev).add(task.id));
    try {
      await markTaskDone(task.id);
    } catch {
      setDoneTaskIds((prev) => {
        const next = new Set(prev);
        next.delete(task.id);
        return next;
      });
    }
  }, []);

  return (
    <div className="p-4 md:p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">Tasks</h1>
        <p className="text-sm text-muted-foreground/70 mt-0.5">Track progress across active work</p>
      </div>

      <TasksStats stats={initialStats} loading={false} />

      <div className="mt-6">
        <TasksFilters filters={filters} onFiltersChange={setFilters} projects={projects} />
      </div>

      <div className="mt-4">
        <TasksTable
          tasks={visibleTasks}
          onTaskClick={handleTaskClick}
          onChangeProjectClick={handleChangeProjectClick}
          onTaskDone={handleTaskDone}
        />
      </div>

      <TaskDetailSheet
        task={selectedTask}
        open={detailSheetOpen}
        onOpenChange={setDetailSheetOpen}
        onChangeProjectClick={handleDetailChangeProjectClick}
      />

      <ChangeProjectDialog
        task={selectedTask}
        open={changeProjectDialogOpen}
        onOpenChange={setChangeProjectDialogOpen}
        onSuccess={handleProjectUpdated}
      />
    </div>
  );
}
