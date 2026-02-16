// api/pulse.js
import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

// ⏱️ 10-DAY KILL SWITCH HELPER
async function isTrialExpired(userId, supabase) {
    const { data, error } = await supabase
        .from('core_config')
        .select('created_at')
        .eq('user_id', userId)
        .limit(1)
        .single();

    if (error || !data) return false;
    const tenDaysMs = 10 * 24 * 60 * 60 * 1000;
    return (Date.now() - new Date(data.created_at).getTime()) > tenDaysMs;
}

export default async function handler(req, res) {
    try {
        // --- 1. SECURITY GATEKEEPER ---
        const authSecret = req.headers['x-pulse-secret'];
        if (process.env.PULSE_SECRET && authSecret !== process.env.PULSE_SECRET) {
            return res.status(401).json({ error: 'Unauthorized manual trigger.' });
        }

        // --- 2. GET ALL ACTIVE USERS ---
        // We find users by looking at who has a current_season configured.
        const { data: activeUsers, error: userError } = await supabase
            .from('core_config')
            .select('user_id')
            .eq('key', 'current_season');

        if (userError || !activeUsers?.length) {
            return res.status(200).json({ message: 'No active users found.' });
        }

        // Deduplicate user IDs just in case
        const uniqueUserIds = [...new Set(activeUsers.map(u => u.user_id))];
        console.log(`🚀 PULSE INITIATED: Scanning ${uniqueUserIds.length} users.`);

        // --- 3. THE MULTI-TENANT LOOP ---
        for (const userId of uniqueUserIds) {
            console.log(`Processing User: ${userId}`);

            // Check if their 10-day trial is up
            if (await isTrialExpired(userId, supabase)) {
                console.log(`Skipping User ${userId} (Trial Expired)`);
                continue;
            }

            // 3.1 READ: Fetch data ONLY for this specific user
            const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('user_id', userId).eq('is_processed', false);
            const { data: active_tasks } = await supabase.from('tasks').select('id, title, project_id, priority, created_at').eq('user_id', userId).not('status', 'in', '("done","cancelled")');

            // If THIS user has nothing to do, skip to the next user
            if (!dumps?.length && !active_tasks?.length) {
                console.log(`Silence is golden for user ${userId}. Moving on.`);
                continue;
            }

            // Fetch THIS user's specific metadata
            const { data: core } = await supabase.from('core_config').select('key, content').eq('user_id', userId);
            const { data: projects } = await supabase.from('projects').select('id, name, org_tag').eq('user_id', userId);
            const { data: people } = await supabase.from('people').select('name, strategic_weight').eq('user_id', userId);

            // 3.2 TIME & DAY INTELLIGENCE (IST)
            const now = new Date();
            const istOffset = 5.5 * 60 * 60 * 1000;
            const istDate = new Date(now.getTime() + istOffset);
            const day = istDate.getDay();
            const hour = istDate.getHours();
            const isWeekend = (day === 0 || day === 6);
            const isMondayMorning = (day === 1 && hour < 11);

            let briefing_mode = "🟡 IMPORTANT";
            let system_persona = "High-energy Battlefield Chief of Staff";

            // User Persona Customization
            const identityRow = core.find(c => c.key === 'identity')?.content || '';
            if (identityRow.toLowerCase().includes('architect')) system_persona = "System Designer. Logical, structure-focused.";
            if (identityRow.toLowerCase().includes('nurturer')) system_persona = "Wholeness Coach. Focused on family and rest.";

            if (isWeekend) {
                briefing_mode = "⚪ CHORES & 💡 IDEAS (Weekend Rest)";
            } else {
                if (hour < 11) briefing_mode = "🔴 URGENT: CRITICAL ACTIONS";
                else if (hour < 15) briefing_mode = "🟡 IMPORTANT: STRATEGIC MOMENTUM";
                else if (hour < 19) briefing_mode = "⚪ CHORES: OPERATIONAL SHUTDOWN";
                else briefing_mode = "💡 IDEAS: MENTAL CLEAR-OUT";
            }

            // 3.3 FILTERING & CONTEXT COMPRESSION
            const filteredTasks = active_tasks.filter(t => {
                if (t.priority === 'urgent') return true;
                const project = projects.find(p => p.id === t.project_id);
                const oTag = project?.org_tag || "INBOX";

                if (isWeekend) return (oTag === 'PERSONAL' || oTag === 'CHURCH');
                if (hour < 19) return (oTag !== 'PERSONAL' && oTag !== 'CHURCH');
                return (oTag === 'PERSONAL' || oTag === 'CHURCH');
            });

            const compressedTasks = filteredTasks.map(t => {
                const project = projects.find(p => p.id === t.project_id);
                return `[${project?.org_tag || "INBOX"} >> ${project?.name || "General"}] ${t.title} (${t.priority}) [ID:${t.id}]`;
            }).join(' | ').slice(0, 3000);

            const seasonConfig = core.find(c => c.key === 'current_season')?.content || 'No season defined.';
            const overdueTasks = filteredTasks.filter(t => t.priority === 'urgent' && (now - new Date(t.created_at)) / (1000 * 60 * 60) > 48).map(t => t.title);

            // 3.4 PROMPT BUILDING
            const prompt = `    
            ROLE: Chief of Staff for this user.
            STRATEGIC CONTEXT (NORTH STAR): ${seasonConfig}
            CURRENT PHASE: ${briefing_mode}
            STAGNANT URGENT_TASKS: ${JSON.stringify(overdueTasks)}
            PERSONA GUIDELINE: ${system_persona}
            CONTEXT:
            - PROJECTS: ${JSON.stringify(projects?.map(p => p.name) || [])}
            - PEOPLE: ${JSON.stringify(people?.map(p => p.name) || [])}
            - CURRENT OPEN TASKS (COMPRESSED): ${compressedTasks}
            - NEW INPUTS: ${dumps.map(d => d.content).join('\n---\n')}

            INSTRUCTIONS:
            1. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects.
            2. STRATEGIC NAG: Call out stagnant urgent tasks blocking the North Star.
            3. CHECK FOR COMPLETION: Compare inputs against OPEN TASKS to identify IDs finished.
            4. AUTO-ONBOARDING: Add new Projects/People if mentioned.
            5. WEEKEND FILTER: If isWeekend is true (${isWeekend}), hide Work tasks.
            6. EXECUTIVE BRIEF FORMAT: Use exactly "${briefing_mode}". Sections: ✅ COMPLETED, 🛡️ WORK, 🏠 HOME, 💡 IDEAS.

            OUTPUT JSON:
            {
                "completed_task_ids": ["uuid-here"],
                "new_projects": [{ "name": "...", "org_tag": "INBOX" }],
                "new_people": [{ "name": "...", "role": "...", "strategic_weight": 5 }],
                "new_tasks": [{ "title": "...", "project_name": "...", "priority": "urgent", "est_min": 15 }],
                "logs": [{ "entry_type": "IDEAS", "content": "..." }],
                "briefing": "The formatted text string for Telegram."
            }`;

            // 3.5 CALL GEMINI
            let aiData = null;
            try {
                const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash", generationConfig: { responseMimeType: "application/json" } });
                const result = await model.generateContent(prompt);
                const jsonStr = result.response.text().replace(/^```json\n?/, '').replace(/\n?```$/, '').trim();
                aiData = JSON.parse(jsonStr);
            } catch (e) {
                console.error(`AI Error for user ${userId}:`, e);
                continue; // Skip writing if AI failed
            }

            // 3.6 WRITE (Database Updates - LOCKED TO userId)

            // New Projects
            if (aiData.new_projects?.length) {
                const pInserts = aiData.new_projects.map(p => ({ user_id: userId, name: p.name, org_tag: p.org_tag || 'INBOX', status: 'active', context: 'work' }));
                await supabase.from('projects').insert(pInserts);
            }

            // New People
            if (aiData.new_people?.length) {
                const pplInserts = aiData.new_people.map(p => ({ user_id: userId, ...p }));
                await supabase.from('people').insert(pplInserts);
            }

            // Task Completions
            if (aiData.completed_task_ids?.length) {
                const ids = aiData.completed_task_ids.map(item => typeof item === 'string' ? item : item.id);
                await supabase.from('tasks').update({ status: 'done', completed_at: new Date().toISOString() }).in('id', ids).eq('user_id', userId);
            }

            // Refresh projects so new tasks can map to newly created projects
            const { data: updatedProjects } = await supabase.from('projects').select('id, name, org_tag').eq('user_id', userId);
            const fallbackProjectId = updatedProjects?.[0]?.id || null; // Saftey fallback

            // New Tasks
            if (aiData.new_tasks?.length && fallbackProjectId) {
                const taskInserts = aiData.new_tasks.map(task => {
                    const aiTarget = task.project_name?.toLowerCase() || "";
                    const project = updatedProjects.find(p => aiTarget.includes(p.name.toLowerCase()) || p.name.toLowerCase().includes(aiTarget)) || updatedProjects[0];
                    return { user_id: userId, title: task.title, project_id: project.id, priority: task.priority || 'important', status: 'todo' };
                });
                await supabase.from('tasks').insert(taskInserts);
            }

            // Logs & Cleanup
            if (aiData.logs?.length) {
                const logInserts = aiData.logs.map(l => ({ user_id: userId, ...l }));
                await supabase.from('logs').insert(logInserts);
            }
            await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id)).eq('user_id', userId);

            // 3.7 SPEAK (Send via Telegram to THIS specific user)
            if (aiData.briefing) {
                await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        chat_id: userId, // In Telegram, the user_id acts as the chat_id for DMs
                        text: aiData.briefing,
                        parse_mode: 'Markdown'
                    })
                });
            }
        } // <-- End of User Loop

        return res.status(200).json({ success: true, message: 'All users processed.' });

    } catch (error) {
        console.error('Pulse Critical Error:', error);
        return res.status(500).json({ error: error.message });
    }
}