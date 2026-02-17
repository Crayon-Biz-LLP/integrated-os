import { createClient } from '@supabase/supabase-js';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);

const MAIN_KEYBOARD = {
    keyboard: [
        [{ text: "🔴 Urgent" }, { text: "📋 Brief" }],
        [{ text: "👥 People" }, { text: "🔓 Vault" }],
        [{ text: "🧭 Season Context" }]
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
        const userId = String(update.message.from.id);
        const text = update.message.text || '';

        const sendTelegram = async (messageText, customKeyboard = MAIN_KEYBOARD) => {
            await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, text: messageText, parse_mode: 'Markdown', reply_markup: customKeyboard })
            });
        };

        const setConfig = async (key, content) => {
            await supabase.from('core_config').delete().eq('user_id', userId).eq('key', key);
            await supabase.from('core_config').insert([{ user_id: userId, key, content }]);
        };

        // --- 1. /start COMMAND ---
        if (text === '/start') {
            const firstName = update.message.from.first_name || 'Leader';
            await supabase.from('core_config').delete().eq('user_id', userId);
            await supabase.from('people').delete().eq('user_id', userId);
            await setConfig('user_name', firstName);

            const welcomeMsg = `🎯 **Welcome to your 14-Day Sprint, ${firstName}.**\n\nI am your Digital 2iC. Let's configure your engine.\n\n` +
                "**Step 1: Choose my Persona:**\n\n" +
                "⚔️ **Commander:** Direct and urgent. Focuses on rapid execution.\n\n" +
                "🏗️ **Architect:** Methodical and structured. Focuses on engineering systems.\n\n" +
                "🌿 **Nurturer:** Balanced and proactive. Focuses on team dynamics and growth.";

            await sendTelegram(welcomeMsg, PERSONA_KEYBOARD);
            return res.status(200).json({ success: true });
        }

        // --- 2. FETCH CURRENT STATE ---
        let { data: configs } = await supabase.from('core_config').select('key, content').eq('user_id', userId);
        configs = configs || [];

        const identity = configs.find(c => c.key === 'identity')?.content;
        const schedule = configs.find(c => c.key === 'pulse_schedule')?.content;
        const season = configs.find(c => c.key === 'current_season')?.content;
        const hasPeople = configs.find(c => c.key === 'initial_people_setup')?.content;

        // --- 3. THE ONBOARDING STATE MACHINE ---

        // Step 1: Persona
        if (!identity) {
            if (text.match(/Commander|Architect|Nurturer/)) {
                const val = text.includes('Commander') ? '1' : text.includes('Architect') ? '2' : '3';
                await setConfig('identity', val);

                const scheduleMsg = "✅ **Persona locked.**\n\n**Step 2: Choose your Pulse Schedule**\nWhen do you want your Battlefield Briefings?\n\n" +
                    "🌅 **Early:** 6AM, 10AM, 2PM, 6PM\n" +
                    "☀️ **Standard:** 8AM, 12PM, 4PM, 8PM\n" +
                    "🌙 **Late:** 10AM, 2PM, 6PM, 10PM\n\n" +
                    "*(Weekends are reduced to 2 pulses per day)*";

                await sendTelegram(scheduleMsg, SCHEDULE_KEYBOARD);
            } else {
                await sendTelegram("Please select a Persona to continue:", PERSONA_KEYBOARD);
            }
            return res.status(200).json({ success: true });
        }

        // Step 2: Schedule
        if (!schedule) {
            if (text.match(/Early|Standard|Late/)) {
                const val = text.includes('Early') ? '1' : text.includes('Standard') ? '2' : '3';
                await setConfig('pulse_schedule', val);

                const northStarMsg = "✅ **Schedule locked.**\n\n**Step 3: Define your North Star.**\n" +
                    "This is the single most important outcome you are hunting for these 14 days. This will be the anchor for every briefing I send you.";

                await sendTelegram(northStarMsg, { remove_keyboard: true });
            } else {
                await sendTelegram("Please select a briefing schedule:", SCHEDULE_KEYBOARD);
            }
            return res.status(200).json({ success: true });
        }

        // Step 3: North Star
        if (!season) {
            if (text && text.length > 5 && !text.startsWith('/')) {
                await setConfig('current_season', text);

                const peopleMsg = "✅ **North Star locked.**\n\n**Step 4: Key Stakeholders**\nWho are the top people that influence your success? (e.g., 'manager, team lead, client, wife...')\n\n*Type their names below, separated by commas:*";

                await sendTelegram(peopleMsg, { remove_keyboard: true });
            } else {
                await sendTelegram("Please define your 14-day North Star.", { remove_keyboard: true });
            }
            return res.status(200).json({ success: true });
        }

        // Step 4: Key People (Finalizing Onboarding)
        if (!hasPeople) {
            if (text && !text.startsWith('/') && text !== '👥 People') {
                const entries = text.split(',').map(e => e.trim());

                const peopleData = entries.map(entry => {
                    const match = entry.match(/(.*?)\((.*?)\)/);
                    return {
                        user_id: userId,
                        name: match ? match[1].trim() : entry,
                        role: match ? match[2].trim() : 'Sprint Contact',
                        strategic_weight: 5
                    };
                });

                const { error } = await supabase.from('people').insert(peopleData);
                if (error) return await sendTelegram(`❌ Error: ${error.message}`);

                await setConfig('initial_people_setup', 'true');
                await sendTelegram(`✅ **System Armed.** Your inner circle is now intelligent context.`, MAIN_KEYBOARD);
            } else await sendTelegram("List your stakeholders (separated by commas).");
            return res.status(200).json({ success: true });
        }

        // --- 4. THE KILL SWITCH ---
        if (await isTrialExpired(userId)) {
            await sendTelegram("⏳ **Your 14-Day Sprint has concluded.** Contact Danny to upgrade.");
            return res.status(200).json({ success: true });
        }

        // --- 5. COMMAND MODE ---
        let finalReply = ""; // Fixes the "Thinking..." shadowing bug

        if (text.startsWith('/') || text === '🔴 Urgent' || text === '📋 Brief' || text === '🧭 Season Context' || text === '🔓 Vault' || text === '👥 People') {

            if (text === '🔓 Vault') {
                const { data: ideas } = await supabase.from('logs').select('content, created_at').eq('user_id', userId).ilike('entry_type', '%IDEAS%').order('created_at', { ascending: false }).limit(5);
                finalReply = (ideas && ideas.length > 0) ? "🔓 **THE IDEA VAULT (Last 5):**\n\n" + ideas.map(i => `💡 *${new Date(i.created_at).toLocaleDateString()}:* ${i.content}`).join('\n\n') : "The Vault is empty.";
            }
            else if (text === '🧭 Season Context') {
                finalReply = `🧭 **CURRENT NORTH STAR:**\n\n${season}`;
            }
            else if (text === '🔴 Urgent') {
                const { data: fire } = await supabase.from('tasks').select('*').eq('priority', 'urgent').eq('status', 'todo').eq('user_id', userId).limit(1).single();
                finalReply = fire ? `🔴 **ACTION REQUIRED:**\n\n🔥 ${fire.title}` : "✅ No active fires.";
            }
            else if (text === '📋 Brief') {
                const { data: tasks } = await supabase.from('tasks').select('title, priority').eq('status', 'todo').eq('user_id', userId).limit(10);
                if (tasks && tasks.length > 0) {
                    const sorted = tasks.sort((a, b) => (a.priority === 'urgent' ? -1 : 1)).slice(0, 5);
                    finalReply = "📋 **EXECUTIVE BRIEF:**\n\n" + sorted.map(t => `${t.priority === 'urgent' ? '🔴' : '⚪'} ${t.title}`).join('\n');
                } else finalReply = "The list is empty.";
            }
            else if (text === '👥 People') {
                const { data: people } = await supabase.from('people').select('name, role').eq('user_id', userId);
                finalReply = people?.length ? "👥 **STAKEHOLDERS:**\n\n" + people.map(p => `• ${p.name} (${p.role})`).join('\n') : "No one registered.";
            }
            else if (text.startsWith('/person ')) {
                const input = text.replace('/person ', '').trim();
                const [name, weight] = input.split('|').map(s => s.trim());
                if (name) {
                    const { error } = await supabase.from('people').insert([{ user_id: userId, name: name, strategic_weight: parseInt(weight) || 5 }]);
                    finalReply = error ? "❌ Error adding person." : `👤 **Stakeholder Registered:** ${name}\nStrategic Weight: ${weight || 5}/10`;
                } else finalReply = "❌ Format: `/person Name | Weight`";
            }

            if (finalReply) await sendTelegram(finalReply);
            return res.status(200).json({ success: true });
        }

        // --- 6. CAPTURE MODE ---
        if (text) {
            await supabase.from('raw_dumps').insert([{ user_id: userId, content: text }]);
            await sendTelegram('✅');
        }

        return res.status(200).json({ success: true });

    } catch (error) {
        console.error('Webhook Error:', error);
        return res.status(500).json({ error: error.message });
    }
}