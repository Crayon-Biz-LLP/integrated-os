# Integrated-OS | Executive Command (Python Engine)

A proprietary Digital Chief of Staff (2iC) engineered for high-density, strategic execution. Integrated-OS operates natively via Telegram, utilizing asynchronous serverless architecture and AI to synthesize raw thoughts, manage stakeholders, and drive momentum toward 14-day sprint goals.

## 🏗️ Architecture

- **Runtime:** Python 3.11+ (Serverless via Vercel)
- **Framework:** FastAPI (Asynchronous routing)
- **AI Engine:** Google Gemini 2.5 Flash (`google-generativeai`)
- **Database:** Supabase (PostgreSQL)
- **Interface:** Telegram Bot API (`httpx`)

## 📂 Repository Structure

```text
integrated-os/
├── api/
│   ├── index.py        # FastAPI application entry point
│   ├── webhook.py      # Telegram webhook state machine & logic
│   └── pulse.py        # Asynchronous batch processing & Gemini AI integration
├── .env                # Local environment variables (ignored)
├── .gitignore
├── requirements.txt    # Python dependencies
└── vercel.json         # Vercel production deployment configuration
```

## ⚙️ Environment Variables

Create a `.env` file in the root directory and ensure the following keys are populated:

```ini
TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_ANON_KEY="your_supabase_anon_key"
GEMINI_API_KEY="your_google_gemini_api_key"
PULSE_SECRET="a_secure_random_string_for_cron_auth"
```

## 🚀 Local Development

1. **Install Dependencies:**
Ensure you are using Python 3.11+. Create a virtual environment and install the requirements:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
pip install -r requirements.txt
```

2. **Run the FastAPI Server locally:**
```bash
uvicorn api.index:app --reload --port 3000
```

3. **Test Webhooks locally (using ngrok):**
```bash
ngrok http 3000
```

*Register the generated ngrok URL with Telegram:*
```bash
curl -F "url=https://<your-ngrok-url>.ngrok.app/api/webhook" https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook
```

## ☁️ Production Deployment (Vercel)

1. **Deploy via Vercel CLI or GitHub Integration:**
```bash
vercel --prod
```

2. **Configure Environment Variables:**
Ensure all `.env` variables are added to your Vercel Project Settings.
3. **Register the Production Webhook:**
Point your Telegram bot to the live Vercel domain:
```bash
curl -F "url=https://your-vercel-project.vercel.app/api/webhook" https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook
```

## ⏱️ Triggering the Pulse Engine

The `/api/pulse` endpoint is designed to run on a scheduled Cron job (e.g., via GitHub Actions, Vercel Cron, or cron-job.org).

**HTTP Request to trigger the Pulse:**

```bash
curl -X POST https://your-vercel-project.vercel.app/api/pulse \
  -H "X-Pulse-Secret: your_secure_pulse_secret" \
  -H "X-Manual-Trigger: true"
```

## 📱 Usage Guide (Telegram Interface)

Once deployed, interact with the bot directly in Telegram:

* `/start`: Initializes the engine and begins the 14-day Sprint configuration (Persona, Schedule, Timezone, Main Goal, Stakeholders).
* **Capture Mode:** Send any raw text message. The engine will capture it asynchronously to the "Vault" and parse it into actionable tasks during the next scheduled Pulse.
* **Menu Commands:**
  * 🔴 **Urgent:** Pulls the immediate, highest-priority task.
  * 📋 **Brief:** Retrieves a formatted list of up to 5 pending tasks.
  * 🔓 **Vault:** Recalls the last 5 raw thoughts submitted.
  * 👥 **People:** Displays current stakeholders and their strategic weight.
  * ⚙️ **Settings:** Opens the recalibration menu to adjust Persona, Schedule, or Goal.

## 🔒 Privacy Protocol

The system operates on an isolated database instance. Raw input processing occurs strictly between your verified Telegram ID, the Supabase backend, and the Gemini API (which is configured to prohibit model training on user inputs).
