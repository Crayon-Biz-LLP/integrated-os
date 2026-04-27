'use client';

import { useEffect, useState } from 'react';
import { Task, TaskFilters } from '@/lib/tasks/types';
import { fetchTasks } from '@/lib/tasks/api';
import { TasksStats } from '@/components/tasks/tasks-stats';
import { TasksFilters } from '@/components/tasks/tasks-filters';
import { TasksTable } from '@/components/tasks/tasks-table';
import { TaskDetailSheet } from '@/components/tasks/task-detail-sheet';
import { ChangeProjectDialog } from '@/components/tasks/change-project-dialog';

const defaultFilters: TaskFilters = {
  search: '',
  status: 'all',
  priority: 'all',
  projectId: 'all',
  dueWindow: 'all',
};

export default function Page() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<TaskFilters>(defaultFilters);

  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [detailSheetOpen, setDetailSheetOpen] = useState(false);
  const [changeProjectDialogOpen, setChangeProjectDialogOpen] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetchTasks(filters).then((data) => {
      setTasks(data);
      setLoading(false);
    });
  }, [filters]);

  const handleTaskClick = (task: Task) => {
    setSelectedTask(task);
    setDetailSheetOpen(true);
  };

  const handleChangeProjectClick = (task: Task) => {
    setSelectedTask(task);
    setDetailSheetOpen(false);
    setChangeProjectDialogOpen(true);
  };

  const handleDetailChangeProjectClick = () => {
    setDetailSheetOpen(false);
    setChangeProjectDialogOpen(true);
  };

  const handleProjectUpdated = (updatedTask: Task) => {
    setTasks((prev) =>
      prev.map((t) => (t.id === updatedTask.id ? updatedTask : t))
    );
    setSelectedTask(updatedTask);
  };

  return (
    <div className="p-4 md:p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold">Tasks</h1>
        <p className="text-muted-foreground mt-1">Track progress across active work</p>
      </div>

      <TasksStats />

      <div className="mt-6">
        <TasksFilters filters={filters} onFiltersChange={setFilters} />
      </div>

      <div className="mt-4">
        <TasksTable
          tasks={tasks}
          onTaskClick={handleTaskClick}
          onChangeProjectClick={handleChangeProjectClick}
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