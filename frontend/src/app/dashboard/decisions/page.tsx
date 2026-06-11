import { createServerSupabaseClient } from "@/lib/supabase-server";
import type { CallPendingItem, WhatsAppPendingMessage } from "@/lib/decisions/types";
import { DecisionsShell } from "./decisions-shell";

export const dynamic = 'force-dynamic';

export default async function DecisionsPage() {
  const supabase = await createServerSupabaseClient();

  const [callRes, whatsappRes] = await Promise.all([
    supabase
      .from("messages")
      .select("*")
      .eq("channel", "call")
      .is("danny_decision", null)
      .order("created_at", { ascending: false })
      .limit(100),
    supabase
      .from("messages")
      .select("*")
      .eq("channel", "whatsapp")
      .is("danny_decision", null)
      .eq("classification", "actionable")
      .order("created_at", { ascending: false })
      .limit(100),
  ]);

  const rawCallItems = callRes.data ?? [];
  const callItems = rawCallItems.map((row: any) => ({
    ...row,
    action_type: row.metadata?.action_type ?? 'task',
    people_mentioned: row.metadata?.people_mentioned ?? '[]',
  })) as unknown as CallPendingItem[];

  const rawWhatsappItems = whatsappRes.data ?? [];
  const whatsappItems = rawWhatsappItems.map((row: any) => ({
    ...row,
    sender_phone: row.sender_id,
    message_text: row.body,
    linked_person_name: row.metadata?.linked_person_name,
  })) as unknown as WhatsAppPendingMessage[];

  return (
    <DecisionsShell
      initialCallItems={callItems}
      initialWhatsappItems={whatsappItems}
    />
  );
}
