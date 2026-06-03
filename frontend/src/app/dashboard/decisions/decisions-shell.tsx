'use client';

import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { CallPendingList } from '@/components/decisions/call-pending-list';
import { WhatsAppPendingList } from '@/components/decisions/whatsapp-pending-list';
import { Phone, MessageSquare } from 'lucide-react';
import type { CallPendingItem, WhatsAppPendingMessage } from '@/lib/decisions/types';

export function DecisionsShell({
  initialCallItems,
  initialWhatsappItems,
}: {
  initialCallItems: CallPendingItem[];
  initialWhatsappItems: WhatsAppPendingMessage[];
}) {
  return (
    <div className="p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Decisions</h1>
      <p className="text-sm text-muted-foreground/70 mt-0.5">
        Review and approve or drop pending items from calls and WhatsApp
      </p>
      <Tabs defaultValue="calls" className="mt-6">
        <TabsList>
          <TabsTrigger value="calls">
            <Phone className="h-4 w-4" />
            Call Items
            {initialCallItems.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {initialCallItems.length}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="whatsapp">
            <MessageSquare className="h-4 w-4" />
            WhatsApp
            {initialWhatsappItems.length > 0 && (
              <span className="ml-1.5 text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded-full tabular-nums">
                {initialWhatsappItems.length}
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
      </Tabs>
    </div>
  );
}
