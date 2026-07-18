'use client';

import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { CallPendingList } from '@/components/decisions/call-pending-list';
import { WhatsAppPendingList } from '@/components/decisions/whatsapp-pending-list';
import { GraphPendingList } from '@/components/decisions/graph-pending-list';
import { NodePendingList } from '@/components/decisions/node-pending-list';
import { MergePendingList } from '@/components/decisions/merge-pending-list';
import { EntityTableList } from '@/components/decisions/entity-table-list';
import { Phone, MessageSquare, Network, Box, GitMerge, Users, Bot } from 'lucide-react';
import { AutoDecisionList } from '@/components/decisions/auto-decision-list';
import type { CallPendingItem, WhatsAppPendingMessage, GraphPendingEdge, GraphPendingNode, GraphMergeProposal, AutoDecisionItem } from '@/lib/decisions/types';

export function DecisionsShell({
  initialCallItems,
  initialWhatsappItems,
  initialGraphItems,
  initialGraphNodes,
  initialMergeProposals,
  initialRejectedNodes,
  initialAutoDecisions,
}: {
  initialCallItems: CallPendingItem[];
  initialWhatsappItems: WhatsAppPendingMessage[];
  initialGraphItems: GraphPendingEdge[];
  initialGraphNodes: GraphPendingNode[];
  initialMergeProposals: GraphMergeProposal[];
  initialRejectedNodes?: GraphPendingNode[];
  initialAutoDecisions?: AutoDecisionItem[];
}) {
  const entityNodes = initialGraphNodes.filter(n => ["person", "project", "organization"].includes(n.type));
  const rejectedEntityNodes = (initialRejectedNodes || []).filter(n => ["person", "project", "organization"].includes(n.type));
  const otherNodes = initialGraphNodes.filter(n => !["person", "project", "organization"].includes(n.type));

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
          
          <TabsTrigger value="entities">
            <Users className="h-4 w-4 mr-2" />
            Entities
            {entityNodes.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {entityNodes.length}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="nodes">
            <Box className="h-4 w-4 mr-2" />
            Graph Nodes
            {otherNodes.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {otherNodes.length}
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
          <TabsTrigger value="auto">
            <Bot className="h-4 w-4 mr-2" />
            Auto
            {(initialAutoDecisions?.length ?? 0) > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {initialAutoDecisions?.length}
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
        
        <TabsContent value="entities" className="mt-4">
          <EntityTableList items={entityNodes} rejectedItems={rejectedEntityNodes} />
        </TabsContent>
        <TabsContent value="nodes" className="mt-4">
          <NodePendingList items={otherNodes} />
        </TabsContent>
        <TabsContent value="merge" className="mt-4">
          <MergePendingList items={initialMergeProposals} />
        </TabsContent>
        <TabsContent value="auto" className="mt-4">
          <AutoDecisionList initialItems={initialAutoDecisions ?? []} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
