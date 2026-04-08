import os
import json
import asyncio
import httpx
from urllib.parse import quote
from supabase import create_client, Client
from google import genai


supabase: Client = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)
gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

CLASSIFICATION_MODEL = "gemini-3.1-flash-lite"


async def send_telegram(chat_id: int, message_text: str):
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message_text,
        "parse_mode": "Markdown"
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)


async def run_agent():
    print("🕵️ Research Agent starting...")

    try:
        res = supabase.table('agent_queue').select('*').eq('status', 'pending').execute()
        pending_items = res.data or []

        if not pending_items:
            print("No pending research tasks.")
            return

        print(f"Found {len(pending_items)} pending task(s)")

        for item in pending_items:
            task_id = item.get('id')
            task_text = item.get('task', '')

            if not task_text:
                continue

            print(f"🔍 Researching: {task_text[:50]}...")

            supabase.table('agent_queue').update({"status": "processing"}).eq('id', task_id).execute()

            try:
                encoded_query = quote(task_text)
                jina_url = f"https://s.jina.ai/{encoded_query}"
                headers = {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {os.getenv('JINA_API_KEY', '')}"
                }

                async with httpx.AsyncClient() as client:
                    search_response = await client.get(jina_url, headers=headers, timeout=30.0)
                    search_results = search_response.text

                synthesis_prompt = f"""You are Danny's Elite Research Analyst. He delegated this research task: "{task_text}". Read the attached web search results and synthesize a highly actionable, structured dossier. Extract only the signal. No fluff. Return the dossier formatted beautifully in Markdown.

Web Search Results:
{search_results}"""

                response = gemini_client.models.generate_content(
                    model=CLASSIFICATION_MODEL,
                    contents=synthesis_prompt
                )

                dossier = response.text.strip()

                supabase.table('raw_dumps').insert([{
                    "content": f"RESEARCH DOSSIER: {task_text}\n\n{dossier}",
                    "metadata": json.dumps({"source": "research_agent", "task_id": task_id})
                }]).execute()

                telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
                if telegram_chat_id:
                    task_snippet = task_text[:40] + "..." if len(task_text) > 40 else task_text
                    await send_telegram(int(telegram_chat_id), f"✅ **Research Complete:** {task_snippet}\n\nThe dossier is in your staging area.")

                supabase.table('agent_queue').update({
                    "status": "completed",
                    "completed_at": "now()"
                }).eq('id', task_id).execute()

                print(f"✅ Completed: {task_text[:30]}...")

            except Exception as e:
                print(f"❌ Error processing {task_id}: {e}")
                supabase.table('agent_queue').update({
                    "status": "failed",
                    "metadata": json.dumps({"error": str(e)})
                }).eq('id', task_id).execute()

    except Exception as e:
        print(f"Agent error: {e}")


if __name__ == '__main__':
    asyncio.run(run_agent())