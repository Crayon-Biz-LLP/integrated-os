import type { TaskStats } from './types';

export function computeTaskStats(
  data: Array<{ status: string | null; reminder_at: string | null; deadline: string | null; completed_at: string | null }>
): TaskStats {
  const now = new Date();
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const tomorrow = new Date(today.getTime() + 86400000);
  const threeDaysAgo = new Date(now.getTime() - 3 * 24 * 60 * 60 * 1000);

  const open = data.filter(
    (t) => !["done", "cancelled"].includes(t.status ?? "todo")
  ).length;

  const dueToday = data.filter((t) => {
    if (["done", "cancelled"].includes(t.status ?? "todo")) return false;
    const d = t.reminder_at || t.deadline;
    if (!d) return false;
    const date = new Date(d);
    return date >= today && date < tomorrow;
  }).length;

  const overdue = data.filter((t) => {
    if (["done", "cancelled"].includes(t.status ?? "todo")) return false;
    const d = t.reminder_at || t.deadline;
    if (!d) return false;
    return new Date(d) < now;
  }).length;

  const completedRecently = data.filter((t) => {
    if (!t.completed_at) return false;
    return new Date(t.completed_at) > threeDaysAgo;
  }).length;

  return { open, dueToday, overdue, completedRecently };
}
