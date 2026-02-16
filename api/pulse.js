// api/pulse.js
import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

async function isTrialExpired(userId, supabase) {
    const { data, error } = await supabase.from('core_config').select('created_at').eq('user_id', userId).limit(1).single();
    if (error || !data) return false;
    return (Date.now() - new Date(data.created_at).getTime()) > (10 * 24 * 60 * 60 * 1000);
}

export default async function handler(req, res) {
    try {
        const authSecret = req.headers['x-pulse-secret'];
        if (process.env.PULSE_SECRET && authSecret !== process.env.PULSE_SECRET) {
            return res.status(401).json({ error: 'Unauthorized manual trigger.' });
        }

        const { data: activeUsers } = await supabase.from('core_config').select('user_id').eq('key', 'current_season');
        if (!activeUsers?.length) return res.status(200).json({ message: 'No active users.' });

        const uniqueUserIds = [...new Set(activeUsers.map(u => u.user_id))];

        for (const userId of uniqueUserIds) {
            if (await isTrialExpired(userId, supabase)) continue;

            // Fetch User Configs
            const { data: core } = await supabase.from('core_config').select('key, content').eq('user_id', userId);

            // --- 🕒 TIME SLOT LOGIC ---
            const now = new Date();
            const istDate = new Date(now.getTime() + (5.5 * 60 * 60 * 1000));
            const day = istDate.getDay();
            const hour = istDate.getHours();
            const isWeekend = (day === 0 || day === 6);

            // Default to Schedule 2 if they haven't picked one
            const scheduleRow = core?.find(c => c.key === 'pulse_schedule')?.content || '2';

            let shouldPulse = false;
            if (isWeekend) {
                if (scheduleRow === '1' && [9, 18].includes(hour)) shouldPulse = true;
                if (scheduleRow === '2' && [10, 19].includes(hour)) shouldPulse = true;
                if (scheduleRow === '3' && [11, 20].includes(hour)) shouldPulse = true;
            } else {
                if (scheduleRow === '1' && [7, 11, 15, 19].includes(hour)) shouldPulse = true;
                if (scheduleRow === '2' && [9, 13, 17, 21].includes(hour)) shouldPulse = true;
                if (scheduleRow === '3' && [11, 15, 19, 23].includes(hour)) shouldPulse = true;
            }

            // If it's not their time, skip them entirely!
            if (!shouldPulse) continue;

            // --- 🧠 CORE LOGIC (Only runs if it's their time) ---
            const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('user_id', userId).eq('is_processed', false);
            const { data: active_tasks } = await supabase.from('tasks').select('id, title, project_id, priority, created_at').eq('user_id', userId).not('status', 'in', '("done","cancelled")');

            if (!dumps?.length && !active_tasks?.length) continue;

            const { data: projects } = await supabase.from('projects').select('id, name, org_tag').eq('user_id', userId);
            const { data: people } = await supabase.from('people').select('name, strategic_weight').eq('user_id', userId);

            let briefing_mode = isWeekend ? "⚪ CHORES & 💡 IDEAS" : (hour < 12 ? "🔴 URGENT ACTIONS" : "🟡 STRATEGIC MOMENTUM");

            // --- 🎭 PERSONA LOGIC ---
            let system_persona = "High-energy Battlefield Chief of Staff (Direct, ROI-focused)";
            const identityRow = core?.find(c => c.key === 'identity')?.content || '';
            if (identityRow === '2') system_persona = "System Architect (Logical, process-oriented, structured)";
            if (identityRow === '3') system_persona = "Wholeness Coach (Balanced, supportive, family-focused)";

            const filteredTasks = active_tasks.filter(t => t.priority === 'urgent' || !isWeekend);
            const compressedTasks = filteredTasks.map(t => `[ID:${t.id}] ${t.title}`).join(' | ').slice(0, 3000);
            const seasonConfig = core?.find(c => c.key === 'current_season')?.content || 'No season defined.';

            const prompt = `    
            ROLE: Chief of Staff for this user.
            STRATEGIC CONTEXT: ${seasonConfig}
            CURRENT PHASE: ${briefing_mode}
            PERSONA GUIDELINE: ${system_persona}
            CONTEXT:
            - PROJECTS: ${JSON.stringify(projects?.map(p => p.name) || [])}
            - PEOPLE: ${JSON.stringify(people?.map(p => p.name) || [])}
            - CURRENT OPEN TASKS: ${compressedTasks}
            - NEW INPUTS: ${dumps.map(d => d.content).join('\n---\n')}

            INSTRUCTIONS: Map tasks, answer dumps, and build a briefing matching the Persona Guideline. Use exactly "${briefing_mode}" as the title.

            OUTPUT JSON:
            {
                "completed_task_ids": [], "new_projects": [], "new_people": [], "new_tasks": [], "logs": [],
                "briefing": "The formatted text string for Telegram."
            }`;

            try {
                const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash", generationConfig: { responseMimeType: "application/json" } });
                const result = await model.generateContent(prompt);
                const aiData = JSON.parse(result.response.text().replace(/^```json\n?/, '').replace(/\n?```$/, '').trim());

                if (aiData.briefing) {
                    await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ chat_id: userId, text: aiData.briefing, parse_mode: 'Markdown' })
                    });
                }
                await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id)).eq('user_id', userId);
            } catch (e) {
                console.error(`AI Error for user ${userId}:`, e);
            }
        }
        return res.status(200).json({ success: true, message: 'Pulse complete.' });
    } catch (error) {
        return res.status(500).json({ error: error.message });
    }
}