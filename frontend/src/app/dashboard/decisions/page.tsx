import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { CallPendingItem, WhatsAppPendingMessage } from "@/lib/decisions/types";
import { DecisionsShell } from "./decisions-shell";

export const dynamic = 'force-dynamic';

export default async function DecisionsPage() {
  const supabase = await createServerSupabaseClient();

  const [callRes, whatsappRes] = await Promise.all([
    supabase
      .from("call_pending_items")
      .select("*")
      .is("danny_decision", null)
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("whatsapp_messages")
      .select("*")
      .is("danny_decision", null)
      .eq("classification", "actionable")
      .order("created_at", { ascending: false })
      .limit(100),
  ]);

  const callItems = (callRes.data ?? []) as unknown as CallPendingItem[];
  const whatsappItems = (whatsappRes.data ?? []) as unknown as WhatsAppPendingMessage[];

  return (
    <DecisionsShell
      initialCallItems={callItems}
      initialWhatsappItems={whatsappItems}
    />
  );
}
