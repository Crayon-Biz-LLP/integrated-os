import { createClient } from '@supabase/supabase-js';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);

const MAIN_KEYBOARD = {
    keyboard: [[{ text: "🔴 Urgent" }, { text: "📋 Brief" }], [{ text: "👥 People" }, { text: "🔓 Vault" }], [{ text: "🧭 Season Context" }]],
    resize_keyboard: true, persistent: true
};

const PERSONA_KEYBOARD = { keyboard: [[{ text: "⚔️ Commander" }, { text: "🏗️ Architect" }, { text: "🌿 Nurturer" }]], resize_keyboard: true, one_time_keyboard: true };
const SCHEDULE_KEYBOARD = { keyboard: [[{ text: "🌅 Early" }, { text: "☀️ Standard" }, { text: "🌙 Late" }]], resize_keyboard: true, one_time_keyboard: true };

export default async function handler(req, res) {
    try {
        const update = req.body;
        if (!update?.message) return res.status(200).json({ ok: true });

        const chatId = update.message.chat.id;
        const userId = String(update.message.from.id);
        const text = update.message.text || '';

        const sendTelegram = async (msg, kb = MAIN_KEYBOARD) => {
            await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, text: msg, parse_mode: 'Markdown', reply_markup: kb })
            });
        };

        const setConfig = async (key, content) => {
            await supabase.from('core_config').delete().eq('user_id', userId).eq('key', key);
            await supabase.from('core_config').insert([{ user_id: userId, key, content }]);
        };

        if (text === '/start') {
            const firstName = update.message.from.first_name || 'Leader';
            await supabase.from('core_config').delete().eq('user_id', userId);
            await setConfig('user_name', firstName);
            await sendTelegram(`🎯 **Welcome, ${firstName}.**\n\nI am your Digital 2iC. Let's configure your engine.\n\n**Step 1: Choose my Persona**:`, PERSONA_KEYBOARD);
            return res.status(200).json({ ok: true });
        }

        let { data: configs } = await supabase.from('core_config').select('key, content').eq('user_id', userId);
        configs = configs || [];
        const identity = configs.find(c => c.key === 'identity')?.content;
        const schedule = configs.find(c => c.key === 'pulse_schedule')?.content;
        const season = configs.find(c => c.key === 'current_season')?.content;
        const hasPeople = configs.find(c => c.key === 'initial_people_setup')?.content;

        // --- ONBOARDING ---
        if (!identity) {
            if (text.match(/Commander|Architect|Nurturer/)) {
                await setConfig('identity', text.includes('Commander') ? '1' : text.includes('Architect') ? '2' : '3');
                await sendTelegram("✅ **Persona locked.**\n\n**Step 2: Choose your Pulse Schedule**", SCHEDULE_KEYBOARD);
            } else await sendTelegram("Please select a persona:", PERSONA_KEYBOARD);
            return res.status(200).json({ ok: true });
        }

        if (!schedule) {
            if (text.match(/Early|Standard|Late/)) {
                await setConfig('pulse_schedule', text.includes('Early') ? '1' : text.includes('Standard') ? '2' : '3');
                await sendTelegram("✅ **Schedule locked.**\n\n**Step 3: Define your North Star.**", { remove_keyboard: true });
            } else await sendTelegram("Please select a schedule:", SCHEDULE_KEYBOARD);
            return res.status(200).json({ ok: true });
        }

        if (!season) {
            if (text && text.length > 5 && !text.startsWith('/')) {
                await setConfig('current_season', text);
                await sendTelegram("✅ **North Star locked.**\n\n**Step 4: Key Stakeholders**\nType names (Sunju, Jeremy, etc) separated by commas:", { remove_keyboard: true });
            } else await sendTelegram("Please define your North Star.");
            return res.status(200).json({ ok: true });
        }

        if (!hasPeople) {
            if (text && !text.startsWith('/') && text !== '👥 People') {
                const names = text.split(',').map(n => n.trim());
                // EXPLICIT ERROR CHECKING
                const { error } = await supabase.from('people').insert(names.map(name => ({ user_id: userId, name: name, strategic_weight: 5 })));

                if (error) {
                    await sendTelegram(`❌ **DB Error:** ${error.message}. Try typing the names again.`);
                } else {
                    await setConfig('initial_people_setup', 'true');
                    await sendTelegram(`✅ **System Armed.**\n\nYour inner circle is tracked. Let's get to work.`, MAIN_KEYBOARD);
                }
            } else await sendTelegram("Please list at least one stakeholder.");
            return res.status(200).json({ ok: true });
        }

        // --- COMMAND MODE ---
        let finalReply = "";
        if (text === '👥 People') {
            const { data: people } = await supabase.from('people').select('name').eq('user_id', userId);
            finalReply = (people?.length > 0) ? "👥 **STAKEHOLDERS:**\n\n" + people.map(p => `• ${p.name}`).join('\n') : "No stakeholders found.";
        } else if (text === '🧭 Season Context') {
            finalReply = `🧭 **NORTH STAR:**\n\n${season}`;
        } else if (text === '🔴 Urgent' || text === '📋 Brief' || text === '🔓 Vault') {
            finalReply = `Your ${text} is currently clean.`;
        } else if (text && !text.startsWith('/')) {
            await supabase.from('raw_dumps').insert([{ user_id: userId, content: text }]);
            finalReply = '✅';
        }

        if (finalReply) await sendTelegram(finalReply);
        return res.status(200).json({ ok: true });
    } catch (error) {
        return res.status(500).json({ error: error.message });
    }
}