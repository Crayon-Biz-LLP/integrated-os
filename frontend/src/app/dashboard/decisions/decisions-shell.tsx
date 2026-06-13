'use client';

import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { CallPendingList } from '@/components/decisions/call-pending-list';
import { WhatsAppPendingList } from '@/components/decisions/whatsapp-pending-list';
import { GraphPendingList } from '@/components/decisions/graph-pending-list';
import { MergePendingList } from '@/components/decisions/merge-pending-list';
import { Phone, MessageSquare, Network, GitMerge } from 'lucide-react';
import type { CallPendingItem, WhatsAppPendingMessage, GraphPendingEdge, GraphMergeProposal } from '@/lib/decisions/types';

export function DecisionsShell({
  initialCallItems,
  initialWhatsappItems,
  initialGraphItems,
  initialMergeProposals,
}: {
  initialCallItems: CallPendingItem[];
  initialWhatsappItems: WhatsAppPendingMessage[];
  initialGraphItems: GraphPendingEdge[];
  initialMergeProposals: GraphMergeProposal[];
}) {
  return (
    <div className="p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Decisions</h1>
      <p className="text-sm text-muted-foreground/70 mt-0.5">
        Review and approve or drop pending items from calls, WhatsApp, and Graph Edges
      </p>
      <Tabs defaultValue="calls" className="mt-6">
        <TabsList>
          <TabsTrigger value="calls">
            <Phone className="h-4 w-4 mr-2" />
            Call Items
            {initialCallItems.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {initialCallItems.length}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="whatsapp">
            <MessageSquare className="h-4 w-4 mr-2" />
            WhatsApp
            {initialWhatsappItems.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {initialWhatsappItems.length}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="graph">
            <Network className="h-4 w-4 mr-2" />
            Graph Edges
            {initialGraphItems.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {initialGraphItems.length}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="merge">
            <GitMerge className="h-4 w-4 mr-2" />
            Merges
            {initialMergeProposals.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {initialMergeProposals.length}
              </span>
            )}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="calls" className="mt-4">
          <CallPendingList items={initialCallItems} />
        </TabsContent>
        <TabsContent value="whatsapp" className="mt-4">
          <WhatsAppPendingList items={initialWhatsappItems} />
        </TabsContent>
        <TabsContent value="graph" className="mt-4">
          <GraphPendingList items={initialGraphItems} />
        </TabsContent>
        <TabsContent value="merge" className="mt-4">
          <MergePendingList items={initialMergeProposals} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
