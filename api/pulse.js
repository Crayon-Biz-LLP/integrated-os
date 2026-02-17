import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

// 🛡️ SPRINT GATEKEEPER
async function isTrialExpired(userId) {
    const { data } = await supabase.from('core_config').select('created_at').eq('user_id', userId).order('created_at', { ascending: true }).limit(1).single();
    if (!data) return false;
    const fourteenDaysMs = 14 * 24 * 60 * 60 * 1000;
    return (Date.now() - new Date(data.created_at).getTime()) > fourteenDaysMs;
}

export default async function handler(req, res) {
    try {
        const authSecret = req.headers['x-pulse-secret'] || req.headers['X-Pulse-Secret'];
        if (!authSecret || authSecret !== process.env.PULSE_SECRET) {
            return res.status(401).json({ error: 'Unauthorized.' });
        }

        // 🧪 TESTING OVERRIDE: Only bypass time-check if specifically requested
        const isManualTest = req.headers['x-manual-trigger'] === 'true';

        const { data: activeUsers } = await supabase.from('core_config').select('user_id').eq('key', 'current_season');
        if (!activeUsers?.length) return res.status(200).json({ message: 'No active users.' });

        for (const userId of [...new Set(activeUsers.map(u => u.user_id))]) {
            if (await isTrialExpired(userId)) continue;

            const { data: core } = await supabase.from('core_config').select('key, content').eq('user_id', userId);

            // --- 🕒 TIME LOGIC (IST) ---
            const now = new Date();
            const istDate = new Date(now.getTime() + (5.5 * 60 * 60 * 1000));
            const hour = istDate.getHours();
            const scheduleRow = core?.find(c => c.key === 'pulse_schedule')?.content || '2';

            let shouldPulse = isManualTest;
            if (!isManualTest) {
                // Early: 6, 10, 14, 18 | Standard: 8, 12, 16, 20 | Late: 10, 14, 18, 22
                if (scheduleRow === '1' && [6, 10, 14, 18].includes(hour)) shouldPulse = true;
                if (scheduleRow === '2' && [8, 12, 16, 20].includes(hour)) shouldPulse = true;
                if (scheduleRow === '3' && [10, 14, 18, 22].includes(hour)) shouldPulse = true;
            }

            if (!shouldPulse) continue;

            // --- 🧠 CONTEXT AGGREGATION ---
            const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('user_id', userId).eq('is_processed', false);
            const { data: tasks } = await supabase.from('tasks').select('id, title, priority').eq('user_id', userId).not('status', 'in', '("done","cancelled")');
            const { data: people } = await supabase.from('people').select('name, role').eq('user_id', userId);
            const season = core?.find(c => c.key === 'current_season')?.content || 'No Goal Set';
            const userName = core?.find(c => c.key === 'user_name')?.content || 'Leader';

            if (!dumps?.length && !tasks?.length) continue;

            const prompt = `
            ROLE: Digital 2iC for ${userName}.
            NORTH STAR: ${season}
            STAKEHOLDERS: ${JSON.stringify(people)}
            ACTIVE TASKS: ${JSON.stringify(tasks)}
            NEW INPUTS: ${dumps.map(d => d.content).join('\n---\n')}

            INSTRUCTIONS:
            1. Address ${userName} personally.
            2. Prioritize tasks involving stakeholders based on their roles (Wife, Client, etc.).
            3. Persona: Architect (Methodical, structured, systems-focused).
            4. If new tasks are identified in the inputs, add them to the new_tasks array.

            OUTPUT JSON:
            {
                "new_tasks": [{"title": "", "priority": "urgent/important/chore"}],
                "briefing": "The Markdown string for Telegram."
            }`;

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

            // --- 💾 DATABASE UPDATE ---
            if (dumps.length > 0) {
                await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id));
            }
            if (aiData.new_tasks?.length > 0) {
                await supabase.from('tasks').insert(aiData.new_tasks.map(t => ({
                    user_id: userId, title: t.title, priority: t.priority, status: 'todo'
                })));
            }
        }
        return res.status(200).json({ success: true });
    } catch (error) {
        return res.status(500).json({ error: error.message });
    }
}