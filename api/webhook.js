// api/webhook.js
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);

const MAIN_KEYBOARD = {
    keyboard: [
        [{ text: "🔴 Urgent" }, { text: "📋 Brief" }],
        [{ text: "🧭 Season Context" }, { text: "🔓 Vault" }]
    ],
    resize_keyboard: true,
    persistent: true
};

const PERSONA_KEYBOARD = {
    keyboard: [[{ text: "⚔️ Commander" }, { text: "🏗️ Architect" }, { text: "🌿 Nurturer" }]],
    resize_keyboard: true,
    one_time_keyboard: true
};

const SCHEDULE_KEYBOARD = {
    keyboard: [[{ text: "🌅 Early" }, { text: "☀️ Standard" }, { text: "🌙 Late" }]],
    resize_keyboard: true,
    one_time_keyboard: true
};

async function isTrialExpired(userId) {
    const { data, error } = await supabase.from('core_config').select('created_at').eq('user_id', userId).order('created_at', { ascending: true }).limit(1).single();
    if (error || !data) return false;
    const fourteenDaysMs = 14 * 24 * 60 * 60 * 1000;
    return (Date.now() - new Date(data.created_at).getTime()) > fourteenDaysMs;
}

export default async function handler(req, res) {
    try {
        const update = req.body;
        if (!update?.message) return res.status(200).json({ message: 'No message' });

        const chatId = update.message.chat.id;
        const userId = String(update.message.from.id); // Force string for database
        const text = update.message.text || '';

        const sendTelegram = async (messageText, customKeyboard = MAIN_KEYBOARD) => {
            await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, text: messageText, parse_mode: 'Markdown', reply_markup: customKeyboard })
            });
        };

        // 🛡️ BULLETPROOF DATABASE SAVER (Bypasses all Supabase constraints)
        const setConfig = async (key, content) => {
            await supabase.from('core_config').delete().eq('user_id', userId).eq('key', key);
            await supabase.from('core_config').insert([{ user_id: userId, key, content }]);
        };

        // --- 1. /start COMMAND (THE MASTER RESET) ---
        if (text === '/start') {
            await supabase.from('core_config').delete().eq('user_id', userId); // Wipe brain clean
            await sendTelegram("🎯 **Welcome to your 14-Day Sprint.**\n\nI am your Digital 2iC. Let's configure your engine.\n\n**Step 1: Choose my Persona** using the buttons below:", PERSONA_KEYBOARD);
            return res.status(200).json({ success: true });
        }

        // --- 2. FETCH CURRENT STATE ---
        let { data: configs } = await supabase.from('core_config').select('key, content').eq('user_id', userId);
        configs = configs || [];

        const identity = configs.find(c => c.key === 'identity')?.content;
        const schedule = configs.find(c => c.key === 'pulse_schedule')?.content;
        const season = configs.find(c => c.key === 'current_season')?.content;

        // --- 3. THE ONBOARDING STATE MACHINE ---

        // Step 1: Persona
        if (!identity) {
            if (text.includes('Commander') || text.includes('Architect') || text.includes('Nurturer')) {
                const val = text.includes('Commander') ? '1' : text.includes('Architect') ? '2' : '3';
                await setConfig('identity', val);
                await sendTelegram("✅ **Persona locked.**\n\n**Step 2: Choose your Pulse Schedule**\nWhen do you want your Battlefield Briefings? (4 on Weekdays, 2 on Weekends)", SCHEDULE_KEYBOARD);
            } else {
                await sendTelegram("Please select a persona using the buttons below:", PERSONA_KEYBOARD);
            }
            return res.status(200).json({ success: true });
        }

        // Step 2: Schedule
        if (!schedule) {
            if (text.includes('Early') || text.includes('Standard') || text.includes('Late')) {
                const val = text.includes('Early') ? '1' : text.includes('Standard') ? '2' : '3';
                await setConfig('pulse_schedule', val);
                await sendTelegram("✅ **Schedule locked.**\n\n**Step 3: Define your North Star.**\nWhat is the single most important outcome you are hunting for these 14 days? (Type your answer below)", { remove_keyboard: true });
            } else {
                await sendTelegram("Please select a briefing schedule using the buttons below:", SCHEDULE_KEYBOARD);
            }
            return res.status(200).json({ success: true });
        }

        // Step 3: North Star
        if (!season) {
            if (text && text.length > 5 && !text.startsWith('/')) {
                await setConfig('current_season', text);
                const finalMsg = `✅ **North Star locked. Your OS is armed.**\n\n**How to use me:** Just talk to me naturally. No rigid commands needed.\n\n📥 **To capture tasks or ideas:** Just dump them here.\n*(e.g., "Remind me to call John on Tuesday," or "Idea: start a podcast.")*\n\n✅ **To close or cancel a task:** Just tell me.\n*(e.g., "I finished the John call," or "Cancel the podcast idea.")*\n\nUse the menu below for quick status reports. Let's get to work.`;
                await sendTelegram(finalMsg, MAIN_KEYBOARD);
            } else {
                await sendTelegram("Please reply with a short text defining your North Star for this sprint.", { remove_keyboard: true });
            }
            return res.status(200).json({ success: true });
        }

        // --- 4. THE KILL SWITCH (Trial Expiry Check) ---
        if (await isTrialExpired(userId)) {
            await sendTelegram("⏳ **Your 14-Day Sprint has concluded.**\n\nTo continue utilizing the Integrated OS and maintain your operational velocity, it is time for a Season Review. Contact Danny to upgrade.");
            return res.status(200).json({ success: true });
        }

        // --- 5. COMMAND MODE (Post-Onboarding) ---
        if (text.startsWith('/') || text === '🔴 Urgent' || text === '📋 Brief' || text === '🧭 Season Context' || text === '🔓 Vault') {
            let reply = "Thinking...";

            if (text.startsWith('/persona ')) {
                const choice = text.replace('/persona', '').trim();
                if (['1', '2', '3'].includes(choice)) {
                    await setConfig('identity', choice);
                    reply = `✅ **Persona updated.**`;
                } else {
                    reply = "❌ Invalid choice. Use /persona 1, 2, or 3.";
                }
            }
            else if (text.startsWith('/schedule ')) {
                const choice = text.replace('/schedule', '').trim();
                if (['1', '2', '3'].includes(choice)) {
                    await setConfig('pulse_schedule', choice);
                    reply = `✅ **Schedule updated.**`;
                } else {
                    reply = "❌ Invalid choice. Use /schedule 1, 2, or 3.";
                }
            }
            else if (text === '/vault' || text === '🔓 Vault') {
                const { data: ideas } = await supabase.from('logs').select('content, created_at').eq('user_id', userId).ilike('entry_type', '%IDEAS%').order('created_at', { ascending: false }).limit(5);
                reply = (ideas && ideas.length > 0) ? "🔓 **THE IDEA VAULT (Last 5):**\n\n" + ideas.map(i => `💡 *${new Date(i.created_at).toLocaleDateString()}:* ${i.content}`).join('\n\n') : "The Vault is empty. Start dreaming.";
            }
            else if (text.startsWith('/season') || text === '🧭 Season Context') {
                const params = text.replace('/season', '').replace('🧭 Season Context', '').trim();
                if (params.length === 0) {
                    reply = `🧭 **CURRENT NORTH STAR:**\n\n${season}`;
                } else if (params.length > 5) {
                    await setConfig('current_season', params);
                    reply = "✅ **Season Updated.**\nTarget Locked.";
                }
            }
            else if (text === '/urgent' || text === '🔴 Urgent') {
                const { data: fire } = await supabase.from('tasks').select('*').eq('priority', 'urgent').eq('status', 'todo').eq('user_id', userId).limit(1).single();
                reply = fire ? `🔴 **ACTION REQUIRED:**\n\n🔥 ${fire.title}\n⏱️ Est: ${fire.estimated_minutes} mins` : "✅ No active fires. You are strategic.";
            }
            else if (text === '/brief' || text === '📋 Brief') {
                const { data: tasks } = await supabase.from('tasks').select('title, priority').eq('status', 'todo').eq('user_id', userId).limit(10);
                if (tasks && tasks.length > 0) {
                    const sortOrder = { 'urgent': 1, 'important': 2, 'chores': 3, 'ideas': 4 };
                    const sortedTasks = tasks.sort((a, b) => (sortOrder[a.priority] || 99) - (sortOrder[b.priority] || 99)).slice(0, 5);
                    reply = "📋 **EXECUTIVE BRIEF:**\n\n" + sortedTasks.map(t => `${t.priority === 'urgent' ? '🔴' : t.priority === 'important' ? '🟡' : '⚪'} ${t.title}`).join('\n');
                } else {
                    reply = "The list is empty. Go enjoy your time.";
                }
            }

            await sendTelegram(reply);
            return res.status(200).json({ success: true });
        }

        // --- 6. CAPTURE MODE (Default Brain Dump) ---
        if (text) {
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