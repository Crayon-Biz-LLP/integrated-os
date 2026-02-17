// api/pulse.js
import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

// Adjusted to 14 days to match your Sprint strategy
async function isTrialExpired(userId, supabase) {
    const { data, error } = await supabase.from('core_config').select('created_at').eq('user_id', userId).limit(1).single();
    if (error || !data) return false;
    const fourteenDaysMs = 14 * 24 * 60 * 60 * 1000;
    return (Date.now() - new Date(data.created_at).getTime()) > fourteenDaysMs;
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

            const { data: core } = await supabase.from('core_config').select('key, content').eq('user_id', userId);

            // --- 🕒 UPDATED TIME SLOT LOGIC (Aligned with Onboarding) ---
            const now = new Date();
            const istDate = new Date(now.getTime() + (5.5 * 60 * 60 * 1000));
            const day = istDate.getDay();
            const hour = istDate.getHours();
            const isWeekend = (day === 0 || day === 6);

            const scheduleRow = core?.find(c => c.key === 'pulse_schedule')?.content || '2';

            let shouldPulse = false;
            if (isWeekend) {
                if (scheduleRow === '1' && [8, 20].includes(hour)) shouldPulse = true;      // Early: 8AM, 8PM
                if (scheduleRow === '2' && [10, 22].includes(hour)) shouldPulse = true;    // Standard: 10AM, 10PM
                if (scheduleRow === '3' && (hour === 12 || hour === 0)) shouldPulse = true; // Late: 12PM, 12AM
            } else {
                if (scheduleRow === '1' && [6, 10, 14, 18].includes(hour)) shouldPulse = true;  // Early: 6, 10, 2, 6
                if (scheduleRow === '2' && [8, 12, 16, 20].includes(hour)) shouldPulse = true;  // Standard: 8, 12, 4, 8
                if (scheduleRow === '3' && [10, 14, 18, 22].includes(hour)) shouldPulse = true; // Late: 10, 2, 6, 10
            }

            if (!shouldPulse) continue;

            // --- 🧠 FETCH CONTEXT ---
            const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('user_id', userId).eq('is_processed', false);
            const { data: active_tasks } = await supabase.from('tasks').select('id, title, project_id, priority, created_at').eq('user_id', userId).not('status', 'in', '("done","cancelled")');

            // Even if no new dumps, we pulse for current tasks
            if (!dumps?.length && !active_tasks?.length) continue;

            const { data: projects } = await supabase.from('projects').select('id, name, org_tag').eq('user_id', userId);
            const { data: people } = await supabase.from('people').select('name, strategic_weight').eq('user_id', userId);

            let briefing_mode = isWeekend ? "⚪ WEEKEND REVIEW & IDEAS" : (hour < 12 ? "🔴 MORNING URGENCY" : "🟡 AFTERNOON MOMENTUM");

            // --- 🎭 UPDATED PERSONA LOGIC ---
            let system_persona = "Commander: Direct, urgent, and focused on rapid execution (High-intensity Chief of Staff).";
            const identityRow = core?.find(c => c.key === 'identity')?.content || '1';

            if (identityRow === '2') system_persona = "Architect: Methodical, structured, and focused on engineering systems (Logic-oriented).";
            if (identityRow === '3') system_persona = "Nurturer: Balanced, proactive, and focused on team dynamics and sustainable growth.";

            const seasonConfig = core?.find(c => c.key === 'current_season')?.content || 'No season defined.';
            const compressedTasks = active_tasks.map(t => `[${t.priority.toUpperCase()}] ${t.title}`).join(' | ').slice(0, 2000);

            const userName = core?.find(c => c.key === 'user_name')?.content || 'Leader';
            const prompt = `    
    ROLE: Digital 2iC / Chief of Staff for ${userName}.
            STRATEGIC NORTH STAR: ${seasonConfig}
            CURRENT PHASE: ${briefing_mode}
            PERSONA: ${system_persona}
            
            USER DATA:
            - PROJECTS: ${JSON.stringify(projects?.map(p => p.name) || [])}
            - KEY PEOPLE: ${JSON.stringify(people?.map(p => p.name) || [])}
            - ACTIVE TASKS: ${compressedTasks}
            - NEW RAW INPUTS: ${dumps.map(d => d.content).join('\n---\n')}

            INSTRUCTIONS: 
            1. Address ${userName} personally in the briefing.
            2. Analyze new inputs to create new tasks, projects, or people.
            3. Generate a briefing that strictly matches the Persona Guideline. 
            4. Use exactly "${briefing_mode}" as the header.
            5. Keep the tone professional, direct, and ROI-focused.

            OUTPUT JSON:
            {
                "completed_task_ids": [], 
                "new_projects": [], 
                "new_people": [], 
                "new_tasks": [{"title": "", "priority": "urgent/important/chore", "project_name": ""}], 
                "briefing": "The formatted Markdown string for Telegram."
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

                // Mark processed and handle new AI data if needed
                if (dumps.length > 0) {
                    await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id)).eq('user_id', userId);
                }
            } catch (e) {
                console.error(`Pulse Error for ${userId}:`, e);
            }
        }
        return res.status(200).json({ success: true, message: 'Pulse complete.' });
    } catch (error) {
        return res.status(500).json({ error: error.message });
    }
}