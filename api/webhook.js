// api/webhook.js
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);

// --- 🎛️ THE CONTROL PANEL ---
// Updated with the Vault button
const KEYBOARD = {
    keyboard: [
        [{ text: "🔴 Urgent" }, { text: "📋 Brief" }],
        [{ text: "🧭 Season Context" }, { text: "🔓 Vault" }] // <--- Added Vault Here
    ],
    resize_keyboard: true,
    persistent: true
};

export default async function handler(req, res) {
    try {
        const update = req.body;
        if (!update || !update.message) return res.status(200).json({ message: 'No message' });

        const chatId = update.message.chat.id;
        const text = update.message.text;

        // --- 🔒 SECURITY GATEKEEPER ---
        const OWNER_ID = process.env.TELEGRAM_CHAT_ID;
        if (!OWNER_ID || chatId.toString() !== OWNER_ID.toString()) {
            console.warn(`⛔ Unauthorized access attempt from Chat ID: ${chatId}`);
            return res.status(200).json({ message: 'Unauthorized' });
        }
        // -----------------------------

        // Helper to send message with the Keyboard attached
        const sendTelegram = async (messageText) => {
            await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    chat_id: chatId,
                    text: messageText,
                    parse_mode: 'Markdown',
                    reply_markup: KEYBOARD
                })
            });
        };

        // 1. COMMAND MODE (Handles /commands AND Button Text)
        // Added '🔓 Vault' to this check
        if (text.startsWith('/') || text === '🔴 Urgent' || text === '📋 Brief' || text === '🧭 Season Context' || text === '🔓 Vault') {
            let reply = "Thinking...";

            // --- COMMAND: VAULT (Retrieve Ideas) ---
            // New Block
            if (text === '/vault' || text === '🔓 Vault') {
                const { data: ideas } = await supabase
                    .from('logs')
                    .select('content, created_at')
                    .ilike('entry_type', '%IDEAS%')
                    .order('created_at', { ascending: false })
                    .limit(5);

                if (ideas && ideas.length > 0) {
                    reply = "🔓 **THE IDEA VAULT (Last 5):**\n\n" + ideas.map(i => {
                        const date = new Date(i.created_at).toLocaleDateString();
                        return `💡 *${date}:* ${i.content}`;
                    }).join('\n\n');
                } else {
                    reply = "The Vault is empty. Start dreaming.";
                }
            }

            // --- COMMAND: SEASON (View or Update) ---
            else if (text.startsWith('/season') || text === '🧭 Season Context') {
                const params = text.replace('/season', '').replace('🧭 Season Context', '').trim();

                // Scenario A: View Current Season
                if (params.length === 0) {
                    const { data: season } = await supabase
                        .from('core_config')
                        .select('content')
                        .eq('key', 'current_season')
                        .single();

                    reply = season
                        ? `🧭 **CURRENT NORTH STAR:**\n\n${season.content}`
                        : "⚠️ No Season Context found. Set one using `/season text...`";
                }
                // Scenario B: Update Season
                else {
                    if (params.length < 10) {
                        reply = "❌ **Error:** Definition too short.";
                    } else {
                        const { error } = await supabase
                            .from('core_config')
                            .update({ content: params })
                            .eq('key', 'current_season');
                        reply = error ? "❌ Database Error" : "✅ **Season Updated.**\nTarget Locked.";
                    }
                }
            }

            // --- COMMAND: URGENT (Fire Check) ---
            else if (text === '/urgent' || text === '🔴 Urgent') {
                const { data: fire } = await supabase
                    .from('tasks')
                    .select('*')
                    .eq('priority', 'urgent')
                    .eq('status', 'todo')
                    .limit(1)
                    .single();

                reply = fire
                    ? `🔴 **ACTION REQUIRED:**\n\n🔥 ${fire.title}\n⏱️ Est: ${fire.estimated_minutes} mins`
                    : "✅ No active fires. You are strategic.";
            }

            // --- COMMAND: BRIEF (Strategic Plan) ---
            else if (text === '/brief' || text === '📋 Brief') {
                const { data: tasks } = await supabase
                    .from('tasks')
                    .select('title, priority')
                    .eq('status', 'todo')
                    .limit(10);

                if (tasks && tasks.length > 0) {
                    const sortOrder = { 'urgent': 1, 'important': 2, 'chores': 3, 'ideas': 4 };
                    const sortedTasks = tasks.sort((a, b) => {
                        return (sortOrder[a.priority] || 99) - (sortOrder[b.priority] || 99);
                    }).slice(0, 5);

                    reply = "📋 **EXECUTIVE BRIEF:**\n\n" + sortedTasks.map(t => {
                        const icon = t.priority === 'urgent' ? '🔴' : t.priority === 'important' ? '🟡' : '⚪';
                        return `${icon} ${t.title}`;
                    }).join('\n');
                } else {
                    reply = "The list is empty. Go enjoy your family.";
                }
            }

            await sendTelegram(reply);
            return res.status(200).json({ success: true });
        }

        // 2. CAPTURE MODE (Default)
        if (text) {
            const { error } = await supabase.from('raw_dumps').insert([{ content: text }]);
            if (error) throw error;

            // Receipt Tick
            await sendTelegram('✅');
        }

        return res.status(200).json({ success: true });

    } catch (error) {
        console.error('Webhook Error:', error);
        return res.status(500).json({ error: error.message });
    }
}