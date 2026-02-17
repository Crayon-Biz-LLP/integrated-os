import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

export default async function handler(req, res) {
    try {
        // 🛡️ Logic to catch the secret from multiple possible header formats
        const authSecret = req.headers['x-pulse-secret'] || req.headers['X-Pulse-Secret'];

        // Log for your Vercel console so you can see what's arriving
        console.log("Secret Received:", authSecret ? "YES" : "NO");

        if (!authSecret || authSecret !== process.env.PULSE_SECRET) {
            console.error("Auth Failed. Expected:", process.env.PULSE_SECRET, "Got:", authSecret);
            return res.status(401).json({ error: 'Unauthorized manual trigger.' });
        }

        // --- 🧪 TESTING MODE LOGIC ---
        // If triggered manually, we might want to skip the time check
        const isManualTrigger = req.headers['x-manual-trigger'] === 'true' || true; // Set to true for your test

        const { data: activeUsers } = await supabase.from('core_config').select('user_id').eq('key', 'current_season');
        if (!activeUsers?.length) return res.status(200).json({ message: 'No active users.' });

        const uniqueUserIds = [...new Set(activeUsers.map(u => u.user_id))];

        for (const userId of uniqueUserIds) {
            const { data: core } = await supabase.from('core_config').select('key, content').eq('user_id', userId);

            // --- 🕒 TIME CHECK (Bypassed if manual) ---
            const now = new Date();
            const istDate = new Date(now.getTime() + (5.5 * 60 * 60 * 1000));
            const hour = istDate.getHours();
            const scheduleRow = core?.find(c => c.key === 'pulse_schedule')?.content || '2';

            let shouldPulse = isManualTrigger; // Force pulse for your manual test

            if (!isManualTrigger) {
                // ... (standard timing logic here)
            }

            if (!shouldPulse) continue;

            // --- 🧠 FETCH DATA ---
            const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('user_id', userId).eq('is_processed', false);
            const { data: active_tasks } = await supabase.from('tasks').select('id, title, priority').eq('user_id', userId).not('status', 'in', '("done","cancelled")');
            const { data: people } = await supabase.from('people').select('name, role').eq('user_id', userId);
            const seasonConfig = core?.find(c => c.key === 'current_season')?.content || 'Testing phase';
            const userName = core?.find(c => c.key === 'user_name')?.content || 'Leader';

            // IF NO DATA, STOP
            if (!dumps?.length && !active_tasks?.length) continue;

            const prompt = `
            ROLE: Digital 2iC / Chief of Staff for ${userName}.
            NORTH STAR: ${seasonConfig}
            STAKEHOLDERS: ${JSON.stringify(people)}
            ACTIVE TASKS: ${JSON.stringify(active_tasks)}
            NEW RAW INPUTS: ${dumps.map(d => d.content).join('\n---\n')}

            INSTRUCTIONS:
            1. Address ${userName} personally.
            2. Analyze raw inputs to extract new tasks or updates.
            3. Use Stakeholder roles to prioritize (e.g., Wife, Client).
            4. Keep the tone professional and direct.

            OUTPUT JSON:
            {
                "new_tasks": [{"title": "", "priority": "urgent/important/chore"}],
                "briefing": "Markdown string for Telegram."
            }`;

            try {
                const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash", generationConfig: { responseMimeType: "application/json" } });
                const result = await model.generateContent(prompt);
                const aiData = JSON.parse(result.response.text());

                if (aiData.briefing) {
                    await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ chat_id: userId, text: aiData.briefing, parse_mode: 'Markdown' })
                    });
                }

                // MARK PROCESSED
                if (dumps.length > 0) {
                    await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id));
                }

                // SAVE NEW TASKS
                if (aiData.new_tasks?.length > 0) {
                    await supabase.from('tasks').insert(aiData.new_tasks.map(t => ({
                        user_id: userId,
                        title: t.title,
                        priority: t.priority,
                        status: 'todo'
                    })));
                }

            } catch (e) {
                console.error('Gemini/DB Error:', e);
            }
        }
        return res.status(200).json({ success: true });
    } catch (error) {
        return res.status(500).json({ error: error.message });
    }
}