import { NextResponse } from "next/server";
import { createServerSupabaseClient } from "@/lib/supabase-server";

const STATUSES = ['staged', 'pending', 'processing', 'processed', 'completed', 'embedding_failed', 'noise'];

export async function GET() {
  if (!process.env.NEXT_PUBLIC_SUPABASE_URL || !process.env.SUPABASE_SERVICE_ROLE_KEY) {
    return NextResponse.json({ error: 'Missing Supabase environment variables' }, { status: 500 });
  }
  try {
    const supabase = await createServerSupabaseClient();

    // raw_dumps status breakdown
    const rawDumps: Record<string, number> = {};
    for (const s of STATUSES) {
      const { count } = await supabase
        .from('raw_dumps')
        .select('id', { count: 'exact', head: true })
        .eq('status', s);
      rawDumps[s] = count ?? 0;
    }

    // failed_queue
    const { count: dlqTotal } = await supabase
      .from('failed_queue')
      .select('id', { count: 'exact', head: true });
    const { count: dlqUnresolved } = await supabase
      .from('failed_queue')
      .select('id', { count: 'exact', head: true })
      .gte('retry_count', 5);
    const { data: dlqItems } = await supabase
      .from('failed_queue')
      .select('id, source_table, operation, error_message, retry_count, created_at')
      .order('created_at', { ascending: false })
      .limit(20);

    // audit_logs recent errors
    const { data: recentErrors } = await supabase
      .from('audit_logs')
      .select('created_at, service, level, message')
      .eq('level', 'ERROR')
      .order('created_at', { ascending: false })
      .limit(20);

    // memories
    const { count: memTotal } = await supabase
      .from('memories')
      .select('id', { count: 'exact', head: true });
    const sevenDaysAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
    const { count: memRecent } = await supabase
      .from('memories')
      .select('id', { count: 'exact', head: true })
      .gte('created_at', sevenDaysAgo);

    // tasks
    const { count: tasksOpen } = await supabase
      .from('tasks')
      .select('id', { count: 'exact', head: true })
      .eq('is_current', true)
      .not('status', 'in', "('done','cancelled')");
    const { count: tasksClosed } = await supabase
      .from('tasks')
      .select('id', { count: 'exact', head: true })
      .eq('is_current', true)
      .in('status', ['done', 'cancelled']);

    const stats = {
      rawDumps,
      failedQueue: { total: dlqTotal ?? 0, unresolved: dlqUnresolved ?? 0, recentItems: dlqItems ?? [] },
      auditLogs: { recentErrors: recentErrors ?? [] },
      memories: { total: memTotal ?? 0, recentWeek: memRecent ?? 0 },
      tasks: { open: tasksOpen ?? 0, closed: tasksClosed ?? 0 },
    };

    return NextResponse.json({ stats });
  } catch (err: any) {
    console.error("Health API error:", err);
    return NextResponse.json({ error: err.message || "Internal server error" }, { status: 500 });
  }
}
