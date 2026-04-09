Integrated-OS 🛰️

Integrated-OS is a proprietary "Executive Command" system designed to act as an AI-powered Chief of Staff. It bridges the gap between raw input (voice, text, images via Telegram) and strategic execution (Google Calendar, Google Tasks, and Supabase). It is specifically tuned for high-velocity environments, focusing on revenue-critical tasks and strategic "Seasons."
🏗️ Core Architecture

The system operates as a triangular engine:

    Intake: A FastAPI-based webhook receiver for Telegram.

    Intelligence: A Gemini-powered processing layer that classifies intent (Tasks, Notes, Research).

    The Pulse: A scheduled briefing engine that syncs calendars and delivers SITREPs (Situation Reports).

🧩 Component Breakdown
1. core/webhook.py (The Intake)

This is the system's "ears." It handles real-time communication from Telegram.

    Multimodal Processing: It can "see" and "hear." It processes photos (OCR), audio (transcription), and documents to extract tasks without requiring manual typing.

    Stealth Routing: Automatically assigns inputs to specific entities (e.g., SOLVSTRAT, CRAYON, PERSONAL) using a "Stealth Status" report that confirms logging without cluttering the chat with technical metadata.

    Intent Classification: Uses gemini-3.1-flash-lite to distinguish between a "Task" (to-do), a "Note" (memory), or "Noise" (ignore).

2. core/pulse.py (The Engine)

This is the system's "brain" and "executor." It runs on a schedule via GitHub Actions.

    The Pulse Engine: Synthesizes the current state of all projects into a dry, punchy Telegram briefing.

    Calendar Guard: It checks for conflicts before booking time on Google Calendar. If a new task overlaps with an existing meeting, it flags a "Snooze Conflict."

    Hindsight Memories: Performs a hybrid search (Vector + Graph) to recall past lessons or notes relevant to your current active tasks, preventing you from making the same mistake twice.

    Google Sync: Two-way synchronization between Supabase and Google Tasks/Calendar.

3. core/research_agent.py (The Intern)

A specialized worker that handles "Delegated" tasks. When you ask the system to research a competitor or a tool, this agent uses the Jina AI search engine to browse the live web and synthesize a "Research Dossier" back into your staging area.
🤖 How "Agents" Interact with the Core

The "Agents" in Integrated-OS are not just chatbots; they are functional modules triggered by the Intelligence Layer:

    Trigger: You send a Telegram message: "Research the new pricing for Qhord's competitors."

    Classification: webhook.py identifies the intent as DELEGATE and drops a record into the agent_queue.

    Execution: The research_agent.py (running as a separate GitHub Worker) picks up the queue item, crawls the web, and generates a dossier.

    Feedback: The result is injected back into raw_dumps, and pulse.py summarizes the findings in your next scheduled SITREP.

🚀 Quick-Start Setup
1. Environment Variables

You will need to populate a .env file with the following:

    AI: GEMINI_API_KEY

    Database: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

    Communication: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    Google Auth: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN

    Security: PULSE_SECRET (A custom string to authorize cron jobs)

🧭 Strategic Note

Integrated-OS is governed by a "Season Context" stored in core_config. The AI uses this to prioritize tasks that align with your current 3–6 month goals (e.g., debt recovery or scaling a specific product). Any task not aligning with the "North Star" is deprioritized in the briefings.
