import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { Mission, MissionStats } from "@/lib/missions/types";
import { MissionsShell } from "./missions-shell";

export const dynamic = 'force-dynamic';

export default async function MissionsPage() {
  const supabase = await createServerSupabaseClient();

  const [missionsRes] = await Promise.all([
    supabase
      .from("missions")
      .select("*")
      .order("created_at", { ascending: false })
      .limit(100),
  ]);

  const missions = (missionsRes.data ?? []) as unknown as Mission[];

  const stats: MissionStats = {
    total: missions.length,
    active: missions.filter((m) => m.status === "active").length,
    completed: missions.filter((m) => m.status === "completed").length,
    archived: missions.filter((m) => m.status === "archived").length,
  };

  return <MissionsShell initialMissions={missions} initialStats={stats} />;
}
