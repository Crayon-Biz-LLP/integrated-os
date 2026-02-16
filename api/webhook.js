// api/webhook.js
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);

const MAIN_KEYBOARD = {
    keyboard: [[{ text: "🔴 Urgent" }, { text: "📋 Brief" }], [{ text: "🧭 Season Context" }, { text: "🔓 Vault" }]],
    resize_keyboard: true, persistent: true
};

const PERSONA_KEYBOARD = {
    keyboard: [[{ text: "⚔️ Commander" }, { text: "🏗️ Architect" }, { text: "🌿 Nurturer" }]],
    resize_keyboard: true, one_time_keyboard: true
};

const SCHEDULE_KEYBOARD = {
    keyboard: [[{ text: "🌅 Early" }, { text: "☀️ Standard" }, { text: "🌙 Late" }]],
    resize_keyboard: true, one_time_keyboard: true
};

export default async function handler(req, res) {
    try {
        const update = req.body;
        if (!update?.message) return res.status(200).json({ message: 'No message' });

        const chatId = update.message.chat.id;
        const userId = String(update.message.from.id);
        const text = update.message.text || '';

        const sendTelegram = async (messageText, customKeyboard = { remove_keyboard: true }) => {
            await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, text: messageText, parse_mode: 'Markdown', reply_markup: customKeyboard })
            });
        };

        // 🛠️ DIAGNOSTIC SAVER: This will text you the exact database error!
        const setConfig = async (key, content) => {
            const { error: delErr } = await supabase.from('core_config').delete().eq('user_id', userId).eq('key', key);
            if (delErr) await sendTelegram(`🐞 **Delete Error:** ${delErr.message}`);

            const { error: insErr } = await supabase.from('core_config').insert([{ user_id: userId, key, content }]);
            if (insErr) await sendTelegram(`🐞 **Insert Error:** ${insErr.message}`);
        };

        // 1. Master Reset
        if (text === '/start') {
            const { error: wipeErr } = await supabase.from('core_config').delete().eq('user_id', userId);
            if (wipeErr) await sendTelegram(`🐞 **Wipe Error:** ${wipeErr.message}`);

            await sendTelegram("🎯 **Diagnostic Mode Active.**\n\nStep 1: Choose my Persona:", PERSONA_KEYBOARD);
            return res.status(200).json({ success: true });
        }

        // 2. Fetch State
        let { data: configs, error: fetchErr } = await supabase.from('core_config').select('key, content').eq('user_id', userId);
        if (fetchErr) {
            await sendTelegram(`🐞 **Fetch Error:** ${fetchErr.message}`);
            return res.status(200).json({ success: true });
        }
        configs = configs || [];

        const identity = configs.find(c => c.key === 'identity')?.content;
        const schedule = configs.find(c => c.key === 'pulse_schedule')?.content;
        const season = configs.find(c => c.key === 'current_season')?.content;

        // Step 1: Persona
        if (!identity) {
            if (text.includes('Commander') || text.includes('Architect') || text.includes('Nurturer')) {
                const val = text.includes('Commander') ? '1' : text.includes('Architect') ? '2' : '3';
                await setConfig('identity', val);
                await sendTelegram("✅ **Persona locked.**\n\nStep 2: Pulse Schedule:", SCHEDULE_KEYBOARD);
            } else {
                await sendTelegram("Please select a persona:", PERSONA_KEYBOARD);
            }
            return res.status(200).json({ success: true });
        }

        // Step 2: Schedule
        if (!schedule) {
            if (text.includes('Early') || text.includes('Standard') || text.includes('Late')) {
                const val = text.includes('Early') ? '1' : text.includes('Standard') ? '2' : '3';
                await setConfig('pulse_schedule', val);
                await sendTelegram("✅ **Schedule locked.**\n\nStep 3: Define North Star:", { remove_keyboard: true });
            } else {
                await sendTelegram("Please select a schedule:", SCHEDULE_KEYBOARD);
            }
            return res.status(200).json({ success: true });
        }

        // Step 3: North Star
        if (!season) {
            if (text && text.length > 5 && !text.startsWith('/')) {
                await setConfig('current_season', text);
                await sendTelegram("✅ **System Armed.**", MAIN_KEYBOARD);
            } else {
                await sendTelegram("Reply with North Star:", { remove_keyboard: true });
            }
            return res.status(200).json({ success: true });
        }

        await sendTelegram("✅ Brain Dump Received.", MAIN_KEYBOARD);
        return res.status(200).json({ success: true });

    } catch (error) {
        return res.status(500).json({ error: error.message });
    }
}