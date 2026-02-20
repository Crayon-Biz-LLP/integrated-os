// api/pulse.js
import { createClient } from '@supabase/supabase-js';
import { GoogleGenerativeAI } from '@google/generative-ai';

const supabase = createClient(process.env.SUPABASE_URL, process.env.SUPABASE_ANON_KEY);
const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);

export default async function handler(req, res) {
    try {
        // --- 1.1 SECURITY GATEKEEPER ---
        // Verifies the trigger is authorized to prevent outside interference.
        const authSecret = req.headers['x-pulse-secret'];
        if (process.env.PULSE_SECRET && authSecret !== process.env.PULSE_SECRET) {
            return res.status(401).json({ error: 'Unauthorized manual trigger.' });
        }

        // 1. READ: Fetch everything needed for a full state briefing
        const { data: dumps } = await supabase.from('raw_dumps').select('id, content').eq('is_processed', false);
        const { data: active_tasks } = await supabase.from('tasks').select('id, title, project_id, priority, created_at').not('status', 'in', '("done","cancelled")');

        // 💡 NEW LOGIC: Only silence the tool if BOTH new dumps AND open tasks are empty
        if (!dumps?.length && !active_tasks?.length) {
            return res.status(200).json({ message: 'Nothing to process, nothing to nag about. Silence is golden.' });
        }

        console.log(`🚀 PULSE START: Processing ${dumps?.length || 0} new dumps and ${active_tasks?.length || 0} active tasks.`);

        // Fetch supporting metadata
        const { data: core } = await supabase.from('core_config').select('key, content');
        const { data: projects } = await supabase.from('projects').select('id, name, org_tag');
        const { data: people } = await supabase.from('people').select('name, strategic_weight');

        // --- 🕒 1.2 UNIFIED TIME & DAY INTELLIGENCE (IST) ---
        const now = new Date();
        const istOffset = 5.5 * 60 * 60 * 1000;
        const istDate = new Date(now.getTime() + istOffset);
        const day = istDate.getDay();
        const hour = istDate.getHours();
        const isWeekend = (day === 0 || day === 6);
        const isMondayMorning = (day === 1 && hour < 11); // Detection for Monday re-entry

        let briefing_mode = isWeekend ? "⚪ CHORES & 💡 IDEAS" : (hour < 11 ? "🔴 URGENT: CRITICAL ACTIONS" : "🟡 IMPORTANT");
        let system_persona = isWeekend ? "Relaxed Father Mode" : "High-energy Battlefield Chief of Staff";

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

        // --- 1.3 BANDWIDTH & BUFFER CHECK ---
        // Flag for the AI if task volume is high during Operation Turnaround.
        const isOverloaded = active_tasks.length > 15;

        // --- 1.3.1 STRATEGIC TASK FILTERING ---
        const filteredTasks = active_tasks.filter(t => {
            // 1. THE URGENT BYPASS: Always show Urgent tasks regardless of day/time
            if (t.priority === 'urgent') return true;

            // Find the project's org_tag to apply situational filters
            const project = projects.find(p => p.id === t.project_id);
            const oTag = project?.org_tag || "INBOX";

            // 2. WEEKEND FILTER: Only show Home/Church
            if (isWeekend) {
                return (oTag === 'PERSONAL' || oTag === 'CHURCH');
            }

            // 3. WEEKDAY TIME FILTERS:
            if (hour < 19) {
                // Business Hours: Solvstrat, Product Labs, Crayon, or Inbox
                return (oTag === 'SOLVSTRAT' || oTag === 'PRODUCT_LABS' || oTag === 'CRAYON' || oTag === 'INBOX');
            } else {
                // After Hours: Transition to Home/Church
                return (oTag === 'PERSONAL' || oTag === 'CHURCH');
            }
        });

        // --- 1.4 CONTEXT COMPRESSION (Updated to use filteredTasks) ---
        const compressedTasks = filteredTasks.map(t => {
            const project = projects.find(p => p.id === t.project_id);
            const pName = project?.name || "General";
            const oTag = project?.org_tag || "INBOX";

            return `[${oTag} >> ${pName}] ${t.title} (${t.priority}) [ID:${t.id}]`;
        }).join(' | ');

        // --- 1.5 SEASON EXPIRY LOGIC ---
        const seasonRow = core.find(c => c.key === 'current_season');
        const seasonConfig = seasonRow?.content || ''; // One source of truth
        const expiryMatch = seasonConfig.match(/\[EXPIRY:\s*(\d{4}-\d{2}-\d{2})\]/);

        let system_context = "OPERATIONAL";
        if (expiryMatch) {
            const expiryDate = new Date(expiryMatch[1]);
            if (now > expiryDate) system_context = "CRITICAL: Season Context EXPIRED.";
        }

        // --- 🛡️ 1.6 THE NAG LOGIC (STAGNANT TASK GUARD) ---
        // Change active_tasks to filteredTasks here to respect the Day/Time filters
        const overdueTasks = filteredTasks.filter(t => {
            const createdDate = new Date(t.created_at);
            const hoursOld = (now - createdDate) / (1000 * 60 * 60);
            return t.priority === 'urgent' && hoursOld > 48;
        }).map(t => t.title);


        // 2. THINK
        console.log('🤖 Building prompt...');
        console.log('📊 Data: dumps=', dumps.length, 'tasks=', active_tasks.length);

        // FIXED CONTEXT (prevents bloat)
        const coreSummary = JSON.stringify(core.map(c => ({ [c.key]: c.content })));
        const projectsList = projects.map(p => p.name).join(', ');
        const peopleSummary = JSON.stringify(people.map(p => ({ n: p.name, w: p.strategic_weight })));
        const compressedTasksFinal = compressedTasks.slice(0, 3000);  // Hard limit
        const newInputSummary = dumps.slice(0, 5).map(d => d.content).join(' | ');

        const prompt = `    
        ROLE: Chief of Staff for Danny (Executive Office).
        STRATEGIC CONTEXT: ${seasonConfig}
        CURRENT PHASE: ${briefing_mode}
        SYSTEM_LOAD: ${isOverloaded ? 'OVERLOADED' : 'OPTIMAL'}
        MONDAY_REENTRY: ${isMondayMorning ? 'TRUE' : 'FALSE'}
        STAGNANT URGENT_TASKS: ${JSON.stringify(overdueTasks)}
        PERSONA GUIDELINE: ${system_persona}
        SYSTEM STATUS: ${system_context}
        CONTEXT:
        - IDENTITY: ${JSON.stringify(core)}
        - PROJECTS: ${JSON.stringify(projects.map(p => p.name))}
        - PEOPLE: ${JSON.stringify(people?.map(p => p.name) || [])}
        - CURRENT OPEN TASKS (COMPRESSED): ${compressedTasks}
        - NEW INPUTS: ${dumps.map(d => d.content).join('\n---\n')}

        / --- NEW: PROJECT ROUTING LOGIC ---
        // Use this hierarchy to assign NEW_TASKS or match COMPLETIONS:
        1. SOLVSTRAT (CASH ENGINE): Match tasks for Atna.ai, Smudge, new Lead Gen here or new SaaS and technology projects. Goal: High-ticket revenue.
        2. PRODUCT LABS (INCUBATOR): 
            - Match existing: CashFlow+ (Vasuuli), Integrated-OS.
            - Match NEW IDEAS: If the input involves "SaaS research," "New Product concept," "MVPs," or "Validation" that is NOT for a current Solvstrat client, tag as PRODUCT LABS.
            - Goal: Future equity and passive income.
        3. CRAYON (UMBRELLA): Match Governance, Tax, and Legal here.
        4. PERSONAL: Match Sunju, kids, dogs here.
        5. CHURCH: 
            - Note: All church-related activities must map to the project "Church".

        NEW PROJECT CREATION CRITERIA:
        1. Only add to "new_projects" if a COMPLETELY UNKNOWN client or organization is mentioned 

        INSTRUCTIONS:
        1. ANALYZE NEW INPUTS: Identify completions, new tasks, new people, and new projects. Use the ROUTING LOGIC to categorize completions and new tasks.
        2. STRATEGIC NAG: If STAGNANT_URGENT_TASKS exists, start the brief by calling these out. Ask why these ₹30L velocity blockers are stalled.
        3. CHECK FOR COMPLETION: Compare inputs against OPEN TASKS to identify IDs finished by Danny.
            - If Danny says he finished or completed a task, mark it as done.
            - If Danny describes a result that fulfills a task's objective (e.g., "The contract is signed" fulfills "Get contract signed"), mark it DONE.
            - If Danny uses the past tense of a task's core action verb (e.g., "Mailed the check" fulfills "Mail the check"), mark it DONE.
            - If the input describes the final step of a process (e.g., "App is on the store" fulfills "Submit app for review"), mark it DONE.
            - If Danny says "Cancel", "Ignore", "Forget", or "Not doing" a task, mark it as cancelled.
            - If Danny indicates he is "skipping," "dropping," or "not doing" something, add the ID to "cancelled_task_ids".
        4. AUTO-ONBOARDING:
            - If a new Client/Project is mentioned, add to "new_projects".
            - If a new Person is mentioned, add to "new_people".
        5. STRATEGIC WEIGHTING: Grade items (1-10) based on Cashflow Recovery (₹30L debt).
        6. WEEKEND FILTER: If isWeekend is true (${isWeekend}), do NOT suggest or list Work tasks. Move work inputs to a 'Monday' reminder.
        7. EXECUTIVE BRIEF FORMAT:
            - HEADLINE RULE: Use exactly "${briefing_mode}".
            - ICON RULES: 🔴 (URGENT), 🟡 (IMPORTANT), ⚪ (CHORES), 💡 (IDEAS).
            - SECTIONS: ✅ COMPLETED, 🛡️ WORK (Hide on weekends), 🏠 HOME, 💡 IDEAS (Only at night pulse).
            - TONE: Match the PERSONA GUIDELINE.
        8. MONDAY RULE: If MONDAY_REENTRY is TRUE, start with a "🛡️ WEEKEND RECON" section summarizing any work ideas dumped during the weekend.

        OUTPUT JSON:
        {
            "completed_task_ids": [
                { "id": "uuid-here", "status": "done" },
                { "id": "uuid-here", "status": "cancelled" }
            ],
            "new_projects": [{ "name": "...", "importance": 8, "org_tag": "SOLVSTRAT/PRODUCT_LABS/PERSONAL/CHURCH" }],
            "new_people": [{ "name": "...", "role": "...", "strategic_weight": 9 }],
            "new_tasks": [{ "title": "...", "project_name": "...", "priority": "urgent/important/chores", "est_min": 15 }],
            "logs": [{ "entry_type": "IDEAS/OBSERVATION/JOURNAL", "content": "..." }],
            "briefing": "The formatted text string for Telegram."
        }
    `;

        console.log('🤖 Prompt ready, length:', prompt.length);
        console.log('🤖 Calling Gemini...');

        let rawText = '';
        let aiData = {
            briefing: `⚠️ FALLBACK MODE\\n\\n${dumps.length} new inputs:\\n${newInputSummary.substring(0, 200)}`,
            new_tasks: [], logs: [], completed_task_ids: [], new_projects: [], new_people: []
        };

        try {
            const model = genAI.getGenerativeModel({
                model: "gemini-2.5-flash",  // STABLE [web:82]
                generationConfig: { responseMimeType: "application/json" },  // FORCE JSON! [web:91]
            });

            const result = await model.generateContent(prompt);
            const responseText = result.response.text();
            aiData = JSON.parse(responseText);
            console.log('✅ AI Data Parsed Successfully:', Object.keys(aiData));

            // SUPER ROBUST JSON EXTRACTOR[4]
            // 1. Aggressive markdown strip
            let jsonStr = responseText
                .replace(/^```json\n?/, '')     // Strip leading markdown
                .replace(/\n?```$/, '')         // Strip trailing markdown
                .trim();

            // 2. Fix common JSON errors
            jsonStr = jsonStr
                .replace(/,\s*([}\]])/g, '$1')  // Trailing commas
                .replace(/:\s*([}\]]|$)/g, ': ""');  // Empty values

            // 3. Extract JSON object (handles partial)
            const jsonMatch = jsonStr.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
                jsonStr = jsonMatch[0]; // <--- Added [0] here. This was the bug.
            }

            console.log('🔧 Cleaned JSON preview:', jsonStr.substring(0, 300));
            aiData = JSON.parse(jsonStr);
        } catch (e) {
            console.error("AI JSON Parse Error. Falling back.");
            return res.status(500).json({ error: "AI response failed validation." });
        }


        console.log('📝 Starting DB writes with:', Object.keys(aiData));

        // 3. WRITE (Database Updates)

        // A. BATCH NEW PROJECTS (Deduplicated)
        if (aiData.new_projects?.length) {
            const validTags = ['SOLVSTRAT', 'PRODUCT_LABS', 'PERSONAL', 'CRAYON', 'CHURCH'];

            // Filter out projects that are actually just sub-tasks of existing ones
            const filteredNewProjects = aiData.new_projects.filter(newP => {
                const alreadyExists = projects.some(existingP =>
                    newP.name.toLowerCase().includes(existingP.name.toLowerCase()) ||
                    existingP.name.toLowerCase().includes(newP.name.toLowerCase())
                );
                return !alreadyExists; // Only keep if it's truly a new entity
            });

            if (filteredNewProjects.length > 0) {
                const projectInserts = filteredNewProjects.map(p => ({
                    name: p.name,
                    org_tag: validTags.includes(p.org_tag) ? p.org_tag : 'INBOX',
                    status: 'active',
                    context: (p.org_tag === 'CHURCH' || p.org_tag === 'PERSONAL') ? 'personal' : 'work'
                }));

                const { data: createdProjects, error: pError } = await supabase
                    .from('projects')
                    .insert(projectInserts)
                    .select();

                if (pError) {
                    console.error("❌ Project Insert Error:", pError.message);
                } else if (createdProjects) {
                    projects.push(...createdProjects);
                    console.log(`✅ Created ${createdProjects.length} new entity projects.`);
                }
            }
        }
        // B. Batch New People
        if (aiData.new_people?.length) await supabase.from('people').insert(aiData.new_people);

        // C. BATCH TASK UPDATES (The Safety Bridge)
        if (aiData.completed_task_ids?.length) {
            const updates = aiData.completed_task_ids.map(item => {
                // SAFETY CHECK: Handle both old string format and new object format
                const id = typeof item === 'string' ? item : item.id;
                const status = (typeof item !== 'string' && item.status === 'cancelled') ? 'cancelled' : 'done';

                return supabase.from('tasks')
                    .update({
                        status: status,
                        completed_at: new Date().toISOString()
                    })
                    .eq('id', id);
            });

            await Promise.all(updates);
            console.log(`✅ Processed ${aiData.completed_task_ids.length} task status updates.`);
        }

        // D. BATCH NEW TASKS (Entity-First Matching)
        if (aiData.new_tasks?.length) {
            const taskInserts = aiData.new_tasks.map(task => {
                // 1. Clean the AI's suggested project name to look for the core entity
                const aiTarget = task.project_name?.toLowerCase() || "";

                // 2. Strong Matching: Look for exact or partial matches in your existing project list
                const project = projects.find(p =>
                    aiTarget.includes(p.name.toLowerCase()) ||
                    p.name.toLowerCase().includes(aiTarget)
                )
                    // 3. Fallback logic: Map to INBOX or generic PERSONAL to prevent Atna.ai pollution
                    || projects.find(p => p.org_tag === 'INBOX')
                    || projects[0];

                return {
                    title: task.title,
                    project_id: project.id,
                    priority: task.priority?.toLowerCase() || 'important',
                    status: 'todo',
                    estimated_minutes: task.est_min || 15
                };
            });
            await supabase.from('tasks').insert(taskInserts);
        }

        // E. Cleanup & Logs
        if (aiData.logs?.length) await supabase.from('logs').insert(aiData.logs);
        await supabase.from('raw_dumps').update({ is_processed: true }).in('id', dumps.map(d => d.id));


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
        console.error('Pulse Critical Error:', error);
        return res.status(500).json({ error: error.message });
    }
}