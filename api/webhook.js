import { createClient } from '@supabase/supabase-js';

// --- MANUALLY PASTE YOUR KEYS HERE FOR THIS TEST ---
const SB_URL = "https://tjlerjcbssargalmqpxd.supabase.co";
const SB_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRqbGVyamNic3NhcmdhbG1xcHhkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzEyMjAwMjQsImV4cCI6MjA4Njc5NjAyNH0.8NIs86TfFl_lmkFi5Xw7H9fAsq5iPq31whPwWQ55lRc";
const TG_TOKEN = "8557809212:AAEVBr3A4hnHLxpi9yQ93_o1WT5TkRSJ1nU";

const supabase = createClient(SB_URL, SB_KEY);

export default async function handler(req, res) {
    try {
        const update = req.body;
        if (!update?.message) return res.status(200).send('ok');

        const chatId = update.message.chat.id;
        const userId = String(update.message.from.id);
        const text = update.message.text || '';

        const sendTelegram = async (msg, kb = { remove_keyboard: true }) => {
            await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, text: msg, parse_mode: 'Markdown', reply_markup: kb })
            });
        };

        // --- 1. THE RE-ENGINEERED SAVER ---
        const forceSave = async (key, content) => {
            // We delete first to clear any 'ghost' rows, then insert fresh
            await supabase.from('core_config').delete().eq('user_id', userId).eq('key', key);
            const { error } = await supabase.from('core_config').insert([{ user_id: userId, key, content }]);
            if (error) throw new Error(error.message);
        };

        // --- 2. MASTER RESET ---
        if (text === '/start') {
            await supabase.from('core_config').delete().eq('user_id', userId);
            const welcome = "🎯 **System Reset.**\n\n**Step 1: Choose Persona**";
            const kb = { keyboard: [[{ text: "⚔️ Commander" }, { text: "🏗️ Architect" }]], resize_keyboard: true };
            await sendTelegram(welcome, kb);
            return res.status(200).send('ok');
        }

        // --- 3. STATE CHECK ---
        const { data: configs } = await supabase.from('core_config').select('key, content').eq('user_id', userId);
        const identity = configs?.find(c => c.key === 'identity')?.content;
        const schedule = configs?.find(c => c.key === 'pulse_schedule')?.content;

        // --- 4. THE FLOW ---
        // If no identity, save it and ask for schedule
        if (!identity) {
            if (text.includes('Commander') || text.includes('Architect')) {
                await forceSave('identity', text);
                const kb = { keyboard: [[{ text: "🌅 Early" }, { text: "☀️ Standard" }]], resize_keyboard: true };
                await sendTelegram("✅ **Persona Locked.**\n\n**Step 2: Choose Schedule**", kb);
            } else {
                await sendTelegram("Please select a Persona:");
            }
            return res.status(200).send('ok');
        }

        // If no schedule, save it and ask for North Star
        if (!schedule) {
            if (text.includes('Early') || text.includes('Standard')) {
                await forceSave('pulse_schedule', text);
                await sendTelegram("✅ **Schedule Locked.**\n\n**Step 3: Define your North Star.**");
            } else {
                await sendTelegram("Please select a Schedule:");
            }
            return res.status(200).send('ok');
        }

        await sendTelegram("🚀 **Configuration Complete.** Just dump your thoughts now.");
        return res.status(200).send('ok');

    } catch (err) {
        // This will now text you the EXACT reason it failed
        await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: req.body.message.chat.id, text: `🐞 CRITICAL ERROR: ${err.message}` })
        });
        return res.status(200).send('ok');
    }
}