import { PatternHealthList, type Pattern, type SubsystemRollup } from '@/components/patterns/pattern-health-list';

export const dynamic = 'force-dynamic';

export default async function PatternsPage() {
  let patterns: Pattern[] = [];
  let rollups: Record<string, SubsystemRollup> = {};

  try {
    const baseUrl = process.env.NEXT_PUBLIC_SITE_URL || 'http://localhost:3000';
    const res = await fetch(`${baseUrl}/api/patterns`, {
      cache: 'no-store',
      headers: {
        'x-api-key': process.env.API_SECRET_KEY || '',
      },
    });
    if (res.ok) {
      const data = (await res.json()) as {
        patterns: Pattern[];
        subsystem_rollups: Record<string, SubsystemRollup>;
      };
      patterns = data.patterns || [];
      rollups = data.subsystem_rollups || {};
    }
  } catch (err) {
    console.error('Failed to fetch patterns:', err);
  }

  return (
    <div className="p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Pattern Health</h1>
      <p className="text-sm text-muted-foreground/70 mt-0.5">
        Monitor what Rhodey has learned from your decisions — pattern confidence, decay status, and subsystem health
      </p>
      <div className="mt-6">
        <PatternHealthList initialPatterns={patterns} initialRollups={rollups} />
      </div>
    </div>
  );
}
