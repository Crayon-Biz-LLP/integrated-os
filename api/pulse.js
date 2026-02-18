import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

// 🛡️ FREE TIER OVERRIDE: Instructs Vercel to allow up to 60 seconds of execution (Hobby Tier max)
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

        const { data: activeUsers } = await supabase.from('core_config').select('user_id').eq('key', 'current_season');
        if (!activeUsers?.length) return res.status(200).json({ message: 'No active users.' });

        const uniqueUserIds = [...new Set(activeUsers.map(u => String(u.user_id)))];

        // --- 🚀 THE PARALLEL PROCESSING ENGINE ---
        const processUser = async (userId) => {
            try {
                // LOG 1: Entry Check (Confirms the engine actually sees the user)
                console.log(`[PULSE START] Processing User: ${userId}`);

                if (await isTrialExpired(userId)) {
                    console.log(`[EXIT] User ${userId}: Trial Expired.`);
                    return;
                }

                const { data: core } = await supabase.from('core_config').select('key, content').eq('user_id', userId);

                // LOG 2: Data Check (Detects the String/Integer mismatch)
                if (!core || core.length === 0) {
                    console.log(`[EXIT] User ${userId}: No core_config found. Possible type mismatch.`);
                    return;
                }

                const now = new Date();
                const userOffset = core?.find(c => c.key === 'timezone_offset')?.content || '5.5';
                const localDate = new Date(now.getTime() + (parseFloat(userOffset) * 60 * 60 * 1000));
                const hour = localDate.getHours();
                const scheduleRow = core?.find(c => c.key === 'pulse_schedule')?.content || '2';

                // LOG 3: Time Sync (Verifies if the bot thinks it's the right hour)
                console.log(`[TIME CHECK] User ${userId}: Local Hour ${hour} | Schedule ${scheduleRow} | Offset ${userOffset}`);

                let shouldPulse = isManualTest;
                if (!isManualTest) {
                    const checkHour = (targetHours) => targetHours.includes(hour);
                    if (scheduleRow === '1' && checkHour([6, 10, 14, 18])) shouldPulse = true;
                    if (scheduleRow === '2' && checkHour([8, 12, 16, 20])) shouldPulse = true;
                    if (scheduleRow === '3' && checkHour([10, 14, 18, 22])) shouldPulse = true;
                }

                if (!shouldPulse) {
                    console.log(`[EXIT] User ${userId}: Not scheduled for Hour ${hour}.`);
                    return;
                }

                // Fetch Context & Data
                const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('user_id', userId).eq('is_processed', false);
                const { data: tasks } = await supabase.from('tasks').select('id, title, priority').eq('user_id', userId).not('status', 'in', '("done","cancelled")');
                const { data: people } = await supabase.from('people').select('name, role').eq('user_id', userId);
                const season = core?.find(c => c.key === 'current_season')?.content || 'No Goal Set';
                const userName = core?.find(c => c.key === 'user_name')?.content || 'Leader';

                if (!dumps?.length && !tasks?.length) return;

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
                const aiData = JSON.parse(result.response.text());

                // Send to Telegram
                if (aiData.briefing) {
                    await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ chat_id: userId, text: aiData.briefing, parse_mode: 'Markdown' })
                    });
                }

                // Database Updates (Atomic)
                if (dumps?.length > 0) {
                    await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id));
                }
                if (aiData.new_tasks?.length > 0) {
                    await supabase.from('tasks').insert(aiData.new_tasks.map(t => ({
                        user_id: userId, title: t.title, priority: t.priority, status: 'todo'
                    })));
                }
                // --- NEW: CLOSE COMPLETED TASKS ---
                if (aiData.completed_task_ids?.length > 0) {
                    await supabase.from('tasks').update({ status: 'done' }).in('id', aiData.completed_task_ids).eq('user_id', userId);
                }
            } catch (userError) {
                console.error(`Error processing user ${userId}:`, userError);
                try {
                    await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            chat_id: '756478183', // REPLACE WITH YOUR ACTUAL TELEGRAM ID
                            text: `🚨 **PULSE ENGINE FAILURE**\n\n**User:** ${userId}\n**Error:** ${userError.message}\n\nCheck Vercel logs immediately.`,
                            parse_mode: 'Markdown'
                        })
                    });
                } catch (notifyError) {
                    console.error("Failed to send error notification to admin:", notifyError);
                }
            }
        };

        // --- 🚀 THE PARALLEL BATCHING ENGINE ---
        const BATCH_SIZE = 10;

        for (let i = 0; i < uniqueUserIds.length; i += BATCH_SIZE) {
            const batch = uniqueUserIds.slice(i, i + BATCH_SIZE);

            // Fire 10 users to Gemini simultaneously
            await Promise.allSettled(batch.map(id => processUser(String(id))));

            // If there are more users waiting, pause for 1 second to respect Gemini Rate Limits
            if (i + BATCH_SIZE < uniqueUserIds.length) {
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
        }

        return res.status(200).json({ success: true });
    } catch (error) {
        console.error('Master Pulse Error:', error);
        return res.status(500).json({ error: error.message });
    }
}