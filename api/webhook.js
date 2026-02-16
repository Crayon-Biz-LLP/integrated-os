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

        const sendTelegram = async (msg) => {
            await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, text: msg })
            });
        };

        if (text === '/start') {
            const { error } = await supabase.from('core_config').delete().eq('user_id', userId);
            await sendTelegram("🛠️ **HARD-WIRED TEST ACTIVE.**\n\nStep 1: Pick a Persona (Commander/Architect)");
            return res.status(200).send('ok');
        }

        // --- THE ACTUAL WRITE TEST ---
        if (text.includes('Architect') || text.includes('Commander')) {
            const { error } = await supabase.from('core_config').insert([
                { user_id: userId, key: 'identity', content: text }
            ]);

            if (error) {
                await sendTelegram(`❌ DB REJECTED: ${error.message}`);
            } else {
                await sendTelegram("✅ SUCCESS! Database updated. Now try picking a schedule.");
            }
            return res.status(200).send('ok');
        }

        await sendTelegram("I'm receiving your messages, but the state is still resetting. This means the DB write above failed.");
        return res.status(200).send('ok');

    } catch (err) {
        return res.status(200).send(err.message);
    }
}