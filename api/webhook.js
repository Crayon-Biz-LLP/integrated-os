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

// ⏱️ 14-DAY KILL SWITCH HELPER
async function isTrialExpired(userId, supabase) {
    const { data, error } = await supabase
        .from('core_config')
        .select('created_at')
        .eq('user_id', userId)
        .limit(1)
        .single();

    if (error || !data) return false;
    const fourteenDaysMs = 14 * 24 * 60 * 60 * 1000; // Updated to 14 days
    return (Date.now() - new Date(data.created_at).getTime()) > fourteenDaysMs;
}

export default async function handler(req, res) {
    try {
        const update = req.body;
        if (!update?.message) return res.status(200).json({ message: 'No message' });

        const chatId = update.message.chat.id;
        const userId = update.message.from.id;
        const text = update.message.text || '';

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
            // SAFETY CHECK: Don't overwrite existing users
            const { data: existing } = await supabase.from('core_config').select('id').eq('user_id', userId).limit(1);

            if (existing && existing.length > 0) {
                await sendTelegram("✅ Your OS is already active. Type your thoughts or use the menu.");
                return res.status(200).json({ success: true });
            }

            // Seed new user
            await supabase.from('core_config').upsert([
                { user_id: userId, key: 'identity', content: '1' },
                { user_id: userId, key: 'pulse_schedule', content: '2' },
                { user_id: userId, key: 'current_season', content: 'PENDING' }
            ], { onConflict: 'user_id, key' });

            // Do not indent this text, otherwise Telegram formats it poorly!
            const welcomeMsg = `🎯 **Welcome to your 14-Day Sprint.**

I am your Digital 2iC. Let's configure your engine:

**1. Choose My Persona:**
Type \`/persona 1\` for ⚔️ The Commander (Direct, ROI-focused)
Type \`/persona 2\` for 🏗️ The Architect (Systems-focused)
Type \`/persona 3\` for 🌿 The Nurturer (Wholeness-focused)

**2. Choose Your Pulse Schedule:** *(4 on Weekdays, 2 on Weekends)*
Type \`/schedule 1\` for 🌅 Early (7am, 11am, 3pm, 7pm)
Type \`/schedule 2\` for ☀️ Standard (9am, 1pm, 5pm, 9pm)
Type \`/schedule 3\` for 🌙 Late (11am, 3pm, 7pm, 11pm)

**3. Define Your North Star:**
Type \`/season [Your Main Goal Here]\` to lock in your focus.`;

            await sendTelegram(welcomeMsg);
            return res.status(200).json({ success: true });
        }

        // --- 1. SETTINGS COMMANDS (/persona & /schedule) ---
        if (text.startsWith('/persona ')) {
            const choice = text.replace('/persona', '').trim();
            if (['1', '2', '3'].includes(choice)) {
                await supabase.from('core_config').update({ content: choice }).eq('key', 'identity').eq('user_id', userId);
                await sendTelegram(`✅ **Persona updated.** I have adjusted my communication style.`);
            } else {
                await sendTelegram("❌ Invalid choice. Use /persona 1, 2, or 3.");
            }
            return res.status(200).json({ success: true });
        }

        if (text.startsWith('/schedule ')) {
            const choice = text.replace('/schedule', '').trim();
            if (['1', '2', '3'].includes(choice)) {
                await supabase.from('core_config').update({ content: choice }).eq('key', 'pulse_schedule').eq('user_id', userId);
                await sendTelegram(`✅ **Schedule updated.** Your briefing times are locked in.`);
            } else {
                await sendTelegram("❌ Invalid choice. Use /schedule 1, 2, or 3.");
            }
            return res.status(200).json({ success: true });
        }

        // --- 🔒 2. THE KILL SWITCH (Trial Expiry Check) ---
        if (await isTrialExpired(userId, supabase)) {
            await sendTelegram("⏳ **Your 14-Day Sprint has concluded.**\n\nTo continue utilizing the Integrated OS and maintain your operational velocity, it is time for a Season Review. Contact Danny to upgrade.");
            return res.status(200).json({ success: true });
        }

        // --- 3. COMMAND MODE ---
        if (text.startsWith('/') || text === '🔴 Urgent' || text === '📋 Brief' || text === '🧭 Season Context' || text === '🔓 Vault') {
            let reply = "Thinking...";

            // 🔓 THE IDEA VAULT
            if (text === '/vault' || text === '🔓 Vault') {
                const { data: ideas } = await supabase
                    .from('logs')
                    .select('content, created_at')
                    .eq('user_id', userId)
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
                        .eq('user_id', userId)
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
                            .eq('user_id', userId);
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
                    .eq('user_id', userId)
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
                    .eq('user_id', userId)
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

        // --- 4. CAPTURE MODE (Default) ---
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