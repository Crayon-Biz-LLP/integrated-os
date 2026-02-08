// api/pulse.js
import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

export default async function handler(req, res) {
    try {
        // 1. READ
        const { data: dumps } = await supabase.from('raw_dumps').select('*').eq('is_processed', false);

        // --- 🤫 SILENCE IS GOLDEN ---
        if (!dumps || dumps.length === 0) {
            return res.status(200).json({ message: 'No new dumps. Silence is golden.' });
        }

        const { data: core } = await supabase.from('core_config').select('*');
        const { data: projects } = await supabase.from('projects').select('*');
        const { data: active_tasks } = await supabase.from('tasks').select('id, title, project_id, priority').neq('status', 'done');

        // --- 🕒 1.2 UNIFIED TIME & DAY INTELLIGENCE (IST) ---
        const now = new Date();
        const istOffset = 5.5 * 60 * 60 * 1000;
        const istDate = new Date(now.getTime() + istOffset);

        const day = istDate.getDay(); // 0 = Sun, 6 = Sat
        const hour = istDate.getHours();
        const isWeekend = (day === 0 || day === 6);

        let briefing_mode = "";
        let system_persona = "";

        if (isWeekend) {
            briefing_mode = "⚪ CHORES & 💡 IDEAS (Weekend Rest)";
            system_persona = "Focus ONLY on Home, Family, and Chores. Explicitly hide Work tasks. Be relaxed.";
        } else {
            if (hour < 11) {
                briefing_mode = "🔴 URGENT: CRITICAL ACTIONS";
                system_persona = "High-energy. Direct focus toward URGENT tasks and high-stakes 'Battlefield' items.";
            } else if (hour < 15) {
                briefing_mode = "🟡 IMPORTANT: STRATEGIC MOMENTUM";
                system_persona = "Tactical update. Focus on IMPORTANT tasks, scaling, and growth projects.";
            } else if (hour < 19) {
                briefing_mode = "⚪ CHORES: OPERATIONAL SHUTDOWN";
                system_persona = "Shutdown mode. Push Danny to close work loops and transition to Father mode.";
            } else {
                briefing_mode = "💡 IDEAS: MENTAL CLEAR-OUT";
                system_persona = "Relaxed reflection. Focus on logging IDEAS and observations. Prep for sleep.";
            }
        }

        // --- 1.5 SEASON EXPIRY LOGIC ---
        const seasonRow = core.find(c => c.key === 'current_season');
        const seasonConfig = seasonRow?.content || '';
        const expiryMatch = seasonConfig.match(/\[EXPIRY:\s*(\d{4}-\d{2}-\d{2})\]/);

        let system_context = "OPERATIONAL";
        if (expiryMatch) {
            const expiryDate = new Date(expiryMatch[1]);
            const today = new Date();
            if (today > expiryDate) system_context = "CRITICAL: Season Context EXPIRED.";
        }

        // 2. THINK
        const model = genAI.getGenerativeModel({ model: "gemini-1.5-flash" });

        const prompt = `
    ROLE: Chief of Staff for Danny (Executive Office).
    CURRENT PHASE: ${briefing_mode}
    PERSONA GUIDELINE: ${system_persona}
    SYSTEM STATUS: ${system_context}
    
    CONTEXT:
    - IDENTITY: ${JSON.stringify(core)}
    - PROJECTS: ${JSON.stringify(projects)}
    - CURRENT OPEN TASKS: ${JSON.stringify(active_tasks)}
    - NEW INPUTS: ${JSON.stringify(dumps)}
    
    INSTRUCTIONS:
    1. Analyze NEW INPUTS.
    2. CHECK FOR COMPLETION: Did Danny say he finished something? Compare inputs against "CURRENT OPEN TASKS".
       - If he said "Sent the proposal" and there is a task "Write Proposal" (ID: 12), mark ID 12 as COMPLETED.
    3. WEEKEND FILTER: If isWeekend is true (${isWeekend}), do NOT suggest or list Work tasks. Move work-related inputs to a 'Monday' reminder.
    4. CLASSIFY: Classify remaining inputs as URGENT, IMPORTANT, CHORES, IDEAS.
    5. EXECUTIVE BRIEF FORMAT:
       - HEADLINE RULE: Use the exactly this headline: "${briefing_mode}".
       - ICON RULES: 
         * Use 🔴 for URGENT items.
         * Use 🟡 for IMPORTANT items.
         * Use ⚪ for CHORES items.
         * Use 💡 for IDEAS.
       - SECTIONS: ✅ COMPLETED, 🛡️ WORK (Hide on weekends), 🏠 HOME, 💡 IDEAS (Only at night pulse).
       - TONE: Match the PERSONA GUIDELINE.
    
    OUTPUT JSON:
    {
      "completed_task_ids": [], 
      "new_tasks": [{ "title": "...", "project_name": "...", "priority": "urgent/important/chores", "est_min": 15 }],
      "logs": [{ "entry_type": "IDEAS/OBSERVATION/JOURNAL", "content": "..." }],
      "briefing": "The formatted text string for Telegram."
    }
    `;

        const result = await model.generateContent(prompt);
        const text = result.response.text().replace(/```json|```/g, '').trim();
        const aiData = JSON.parse(text);

        // 3. WRITE (Database Updates)
        if (aiData.completed_task_ids?.length > 0) {
            await supabase.from('tasks').update({ status: 'done', completed_at: new Date() }).in('id', aiData.completed_task_ids);
        }

        if (aiData.new_tasks?.length) {
            for (const task of aiData.new_tasks) {
                const project = projects.find(p => p.name.toLowerCase().includes(task.project_name?.toLowerCase())) || projects[0];
                const priorityClean = task.priority?.toLowerCase() || 'important';
                await supabase.from('tasks').insert({
                    title: task.title,
                    project_id: project.id,
                    priority: priorityClean,
                    estimated_minutes: task.est_min || 15
                });
            }
        }

        if (aiData.logs?.length) await supabase.from('logs').insert(aiData.logs);

        const dumpIds = dumps.map(d => d.id);
        await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumpIds);

        // 4. SPEAK
        if (process.env.TELEGRAM_CHAT_ID && aiData.briefing) {
            await fetch(`https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    chat_id: process.env.TELEGRAM_CHAT_ID,
                    text: aiData.briefing,
                    parse_mode: 'Markdown'
                })
            });
        }

        return res.status(200).json({ success: true, briefing: aiData.briefing });

    } catch (error) {
        console.error('Pulse Error:', error);
        return res.status(500).json({ error: error.message });
    }
}