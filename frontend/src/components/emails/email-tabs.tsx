'use client';

import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Badge } from '@/components/ui/badge';

export function EmailTabs({
  activeTab,
  onTabChange,
  inboxCount,
  pendingCount,
  draftsCount,
}: {
  activeTab: 'inbox' | 'pending' | 'drafts';
  onTabChange: (tab: 'inbox' | 'pending' | 'drafts') => void;
  inboxCount: number;
  pendingCount: number;
  draftsCount: number;
}) {
  const tabs = [
    { key: 'inbox' as const, label: 'Inbox', count: inboxCount },
    { key: 'pending' as const, label: 'Pending Tasks', count: pendingCount },
    { key: 'drafts' as const, label: 'Drafts', count: draftsCount },
  ];

  return (
    <Tabs value={activeTab} onValueChange={(v) => onTabChange(v as typeof activeTab)} className="mb-6">
      <TabsList>
        {tabs.map(({ key, label, count }) => (
          <TabsTrigger key={key} value={key} className={activeTab === key ? "border-b-2 border-primary text-foreground font-medium flex items-center gap-2" : "text-muted-foreground hover:text-foreground transition-colors flex items-center gap-2"}>
            {label}
            <span className="bg-primary/10 text-primary text-xs px-1.5 py-0.5 rounded-full font-semibold ml-1">{count}</span>
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}
