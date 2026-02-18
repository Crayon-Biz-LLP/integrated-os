import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

// 🛡️ Hobby Tier max execution time
export const maxDuration = 60;

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

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

        const isManualTest = req.headers['x-manual-trigger'] === 'true';

        // Fetch users who have completed setup
        const { data: activeUsers } = await supabase.from('core_config').select('user_id').eq('key', 'current_season');
        if (!activeUsers?.length) return res.status(200).json({ message: 'No active users.' });

        // 🛠️ FIX 1: Robust ID Normalization (Trimmed Strings)
        const uniqueUserIds = [...new Set(activeUsers.map(u => String(u.user_id).trim()))];
        console.log(`[ENGINE] Found ${uniqueUserIds.length} active users: ${uniqueUserIds.join(', ')}`);

        const processUser = async (userId) => {
            try {
                console.log(`[PULSE START] Processing User: ${userId}`);

                if (await isTrialExpired(userId)) {
                    console.log(`[EXIT] User ${userId}: Trial Expired.`);
                    return;
                }

                const { data: core } = await supabase.from('core_config').select('key, content').eq('user_id', userId);

                if (!core || core.length === 0) {
                    console.log(`[EXIT] User ${userId}: No configuration found.`);
                    return;
                }

                const now = new Date();
                const userOffset = core?.find(c => c.key === 'timezone_offset')?.content || '5.5';
                const localDate = new Date(now.getTime() + (parseFloat(userOffset) * 60 * 60 * 1000));
                const hour = localDate.getHours();
                const scheduleRow = core?.find(c => c.key === 'pulse_schedule')?.content || '2';

                console.log(`[TIME CHECK] User ${userId}: Local Hour ${hour} | Schedule ${scheduleRow} | Offset ${userOffset}`);

                let shouldPulse = isManualTest;
                if (!isManualTest) {
                    const checkHour = (targetHours) => targetHours.includes(hour);
                    if (scheduleRow === '1' && checkHour([6, 10, 14, 18])) shouldPulse = true;
                    if (scheduleRow === '2' && checkHour([8, 12, 16, 20])) shouldPulse = true;
                    if (scheduleRow === '3' && checkHour([10, 14, 18, 22])) shouldPulse = true;
                }

                if (!shouldPulse) {
                    console.log(`[EXIT] User ${userId}: Not scheduled for current hour.`);
                    return;
                }

                // Data Retrieval
                const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('user_id', userId).eq('is_processed', false);
                const { data: tasks } = await supabase.from('tasks').select('id, title, priority').eq('user_id', userId).neq('status', 'done').neq('status', 'cancelled');
                const { data: people } = await supabase.from('people').select('name, role').eq('user_id', userId);
                const season = core?.find(c => c.key === 'current_season')?.content || 'No Goal Set';
                const userName = core?.find(c => c.key === 'user_name')?.content || 'Leader';

                if (!dumps?.length && !tasks?.length) {
                    console.log(`[EXIT] User ${userId}: No active data to pulse.`);
                    return;
                }

                const prompt = `
                ROLE: Digital 2iC for ${userName}.
                Main Goal: ${season}
                STAKEHOLDERS: ${JSON.stringify(people)}
                ACTIVE TASKS: ${JSON.stringify(tasks)}
                NEW INPUTS: ${dumps?.map(d => d.content).join('\n---\n') || 'None'}

                INSTRUCTIONS:
                1. Address ${userName} personally.
                2. Use a high-density, scannable Markdown format. No long paragraphs.
                3. Structure: 
                    - [Emoji] [PULSE NAME]: [TIME-STAMP/TRIGGER NAME]
                    - Personal Greeting + Progress Tracker.
                    - 1-2 sharp, direct sentences from the Persona (Commander: Urgent/Aggressive | Architect: Systems/Logic | Nurturer: Balanced/Relationship-focused).
                    - CATEGORIZED LISTS: (Work, Home, Ideas). 
                    - Use 🔴 for Urgent, 🟡 for Important, ⚪ for Chore/Idea.
                4. Prioritize tasks involving stakeholders based on their roles.
                5. NEVER display Task IDs to the user. Keep the text clean.
                6. If new tasks are identified in the inputs, add them to the new_tasks array.
                7. SEMANTIC MATCHING: If the user's input indicates they finished or closed a task, find its 'id' in the ACTIVE TASKS list and add it to the "completed_task_ids" array.

                OUTPUT JSON:
                {
                    "new_tasks": [{"title": "", "priority": "urgent/important/chore"}],
                    "completed_task_ids": [],
                    "briefing": "The Clean Markdown string."
                }`;

                const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash", generationConfig: { responseMimeType: "application/json" } });
                const result = await model.generateContent(prompt);

                // 🛠️ FIX 2: JSON Sanitizer (Removes markdown code blocks if Gemini includes them)
                const rawText = result.response.text();
                const cleanJson = rawText.replace(/```json/gi, '').replace(/```/gi, '').trim();
                const aiData = JSON.parse(cleanJson);

                if (aiData.briefing) {
                    const tgUrl = `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`;

                    // 🛠️ FIX 3: Telegram Markdown Failsafe
                    const tgRes = await fetch(tgUrl, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ chat_id: userId, text: aiData.briefing, parse_mode: 'Markdown' })
                    });

                    // If Markdown fails (bad tag), retry as plain text so the user gets the message
                    if (!tgRes.ok) {
                        console.error(`[TG ERROR] User ${userId}: Markdown rejected. Retrying plain text.`);
                        await fetch(tgUrl, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ chat_id: userId, text: aiData.briefing })
                        });
                    }
                }

                // Database Updates
                if (dumps?.length > 0) await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id));
                if (aiData.new_tasks?.length > 0) {
                    await supabase.from('tasks').insert(aiData.new_tasks.map(t => ({ user_id: userId, title: t.title, priority: t.priority, status: 'todo' })));
                }
                if (aiData.completed_task_ids?.length > 0) {
                    await supabase.from('tasks').update({ status: 'done' }).in('id', aiData.completed_task_ids).eq('user_id', userId);
                }

            } catch (userError) {
                console.error(`[CRITICAL] User ${userId}:`, userError.message);
                // Notification to YOU (Admin)
                await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ chat_id: '756478183', text: `🚨 Pulse Failure: ${userId}\nErr: ${userError.message}` })
                });
            }
        };

        const BATCH_SIZE = 10;
        for (let i = 0; i < uniqueUserIds.length; i += BATCH_SIZE) {
            const batch = uniqueUserIds.slice(i, i + BATCH_SIZE);
            await Promise.allSettled(batch.map(id => processUser(String(id).trim())));
            if (i + BATCH_SIZE < uniqueUserIds.length) await new Promise(r => setTimeout(r, 1000));
        }

        return res.status(200).json({ success: true });
    } catch (error) {
        console.error('Master Pulse Error:', error);
        return res.status(500).json({ error: error.message });
    }
}