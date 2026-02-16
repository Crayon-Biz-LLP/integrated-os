// api/webhook.js
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);

const KEYBOARD = {
    keyboard: [
        [{ text: "🔴 Urgent" }, { text: "📋 Brief" }],
        [{ text: "🧭 Season Context" }, { text: "🔓 Vault" }]
    ],
    resize_keyboard: true,
    persistent: true
};

// ⏱️ 14-DAY KILL SWITCH HELPER..
async function isTrialExpired(userId, supabase) {
    const { data, error } = await supabase
        .from('core_config')
        .select('created_at')
        .eq('user_id', userId)
        .limit(1)
        .single();

    if (error || !data) return false; // New user, hasn't started yet
    const tenDaysMs = 14 * 24 * 60 * 60 * 1000;
    return (Date.now() - new Date(data.created_at).getTime()) > tenDaysMs;
}

// 🏗️ DATABASE INITIALIZATION (ONBOARDING)
async function initializeUser(userId, supabase) {
    // Check if they already exist to avoid overwriting
    const { data: existing } = await supabase.from('core_config').select('id').eq('user_id', userId).limit(1);
    if (existing && existing.length > 0) return false; // Already initialized

    // Seed their private config rows
    await supabase.from('core_config').upsert([
        { user_id: userId, key: 'identity', content: 'PENDING_PERSONA' },
        { user_id: userId, key: 'current_season', content: 'PENDING_SEASON' }
    ], { onConflict: 'user_id, key' });

    return true; // Newly initialized
}

export default async function handler(req, res) {
    try {
        const update = req.body;
        if (!update || !update.message) return res.status(200).json({ message: 'No message' });

        const chatId = update.message.chat.id;
        const userId = update.message.from.id; // Unique Telegram User ID
        const text = update.message.text || '';

        // Helper to send messages with the persistent keyboard
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

        // --- 0. NEW USER REGISTRATION (/start) ---
        if (text === '/start') {
            const isNew = await initializeUser(userId, supabase);
            if (isNew) {
                const welcomeMsg = `🎯 **Welcome to the 10-Day Sprint.**\n\nI am your Digital 2iC. To protect your focus, we need to configure your operating system.\n\n**Reply with your North Star:** What is the single most important outcome you are hunting for these 10 days? (e.g., 'Close 2 new leads' or 'Launch beta'). I will update your Season Context.`;
                await sendTelegram(welcomeMsg);
            } else {
                await sendTelegram("✅ Your OS is already initialized and active.");
            }
            return res.status(200).json({ success: true });
        }

        // --- 🔒 1. THE KILL SWITCH (Trial Expiry Check) ---
        if (await isTrialExpired(userId, supabase)) {
            await sendTelegram("⏳ **Your 10-Day Sprint has concluded.**\n\nTo continue utilizing the Integrated OS and maintain your operational velocity, it is time for a Season Review. Contact Danny to upgrade.");
            return res.status(200).json({ success: true });
        }

        // --- 2. COMMAND MODE ---
        if (text.startsWith('/') || text === '🔴 Urgent' || text === '📋 Brief' || text === '🧭 Season Context' || text === '🔓 Vault') {
            let reply = "Thinking...";

            // 🔓 THE IDEA VAULT
            if (text === '/vault' || text === '🔓 Vault') {
                const { data: ideas } = await supabase
                    .from('logs')
                    .select('content, created_at')
                    .eq('user_id', userId) // <-- Privacy Firewall
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

            // 🧭 SEASON CONTEXT
            else if (text.startsWith('/season') || text === '🧭 Season Context') {
                const params = text.replace('/season', '').replace('🧭 Season Context', '').trim();

                if (params.length === 0) {
                    const { data: season } = await supabase
                        .from('core_config')
                        .select('content')
                        .eq('key', 'current_season')
                        .eq('user_id', userId) // <-- Privacy Firewall
                        .single();

                    reply = season
                        ? `🧭 **CURRENT NORTH STAR:**\n\n${season.content}`
                        : "⚠️ No Season Context found. Type `/season [your focus here]` to set it.";
                } else {
                    if (params.length < 10) {
                        reply = "❌ **Error:** Definition too short.";
                    } else {
                        const { error } = await supabase
                            .from('core_config')
                            .update({ content: params })
                            .eq('key', 'current_season')
                            .eq('user_id', userId); // <-- Privacy Firewall (CRITICAL)
                        reply = error ? "❌ Database Error" : "✅ **Season Updated.**\nTarget Locked.";
                    }
                }
            }

            // 🔴 URGENT FIRE CHECK
            else if (text === '/urgent' || text === '🔴 Urgent') {
                const { data: fire } = await supabase
                    .from('tasks')
                    .select('*')
                    .eq('priority', 'urgent')
                    .eq('status', 'todo')
                    .eq('user_id', userId) // <-- Privacy Firewall
                    .limit(1)
                    .single();

                reply = fire
                    ? `🔴 **ACTION REQUIRED:**\n\n🔥 ${fire.title}\n⏱️ Est: ${fire.estimated_minutes} mins`
                    : "✅ No active fires. You are strategic.";
            }

            // 📋 EXECUTIVE BRIEF
            else if (text === '/brief' || text === '📋 Brief') {
                const { data: tasks } = await supabase
                    .from('tasks')
                    .select('title, priority')
                    .eq('status', 'todo')
                    .eq('user_id', userId) // <-- Privacy Firewall
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
                    reply = "The list is empty. Go enjoy your time.";
                }
            }

            await sendTelegram(reply);
            return res.status(200).json({ success: true });
        }

        // --- 3. CAPTURE MODE (Default) ---
        if (text) {
            // Note the addition of user_id here so the system knows whose brain dump this is!
            const { error } = await supabase.from('raw_dumps').insert([{ user_id: userId, content: text }]);
            if (error) throw error;

            await sendTelegram('✅');
        }

        return res.status(200).json({ success: true });

    } catch (error) {
        console.error('Webhook Error:', error);
        return res.status(500).json({ error: error.message });
    }
}