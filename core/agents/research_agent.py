import os
import json
import asyncio
import httpx
from urllib.parse import quote

from core.services.db import get_supabase, get_embedding
from core.services.telegram import send_telegram
from core.services.llm import call_gemini_with_retry, get_gemini_client, CLASSIFICATION_MODEL

supabase = get_supabase()


async def run_agent():
    print("Research Agent starting...")

    if not os.getenv("JINA_API_KEY"):
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if telegram_chat_id:
            await send_telegram(int(telegram_chat_id), "Research Agent: JINA_API_KEY is not set. Agent queue is stalled.")
        print("ERROR: JINA_API_KEY not set. Aborting agent run.")
        return

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

            print(f"Researching: {task_text[:50]}...")

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

                response = await call_gemini_with_retry(
                    prompt=synthesis_prompt,
                    model=CLASSIFICATION_MODEL
                )

                dossier = response.text.strip()

                content = f"RESEARCH DOSSIER: {task_text}\n\n{dossier}"
                embedding = get_embedding(content)
                if not embedding:
                    embedding = None
                supabase.table('raw_dumps').insert([{
                    "content": content,
                    "direction": "incoming",
                    "metadata": json.dumps({"source": "research_agent", "task_id": task_id}),
                    "embedding": embedding
                }]).execute()

                telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
                if telegram_chat_id:
                    task_snippet = task_text[:40] + "..." if len(task_text) > 40 else task_text
                    try:
                        await send_telegram(int(telegram_chat_id), f"**Research Complete:** {task_snippet}\n\nThe dossier is in your staging area.")
                    except Exception as e:
                        print(f"Telegram notify failed for task {task_id}: {e}")

                supabase.table('agent_queue').update({
                    "status": "completed",
                    "completed_at": "now()"
                }).eq('id', task_id).execute()

                print(f"Completed: {task_text[:30]}...")

            except Exception as e:
                print(f"Error processing {task_id}: {e}")
                supabase.table('agent_queue').update({
                    "status": "failed",
                    "metadata": json.dumps({"error": str(e)})
                }).eq('id', task_id).execute()

    except Exception as e:
        print(f"Agent error: {e}")


if __name__ == '__main__':
    asyncio.run(run_agent())
