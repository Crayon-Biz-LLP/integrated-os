import asyncio
from dotenv import load_dotenv

load_dotenv()

from core.services.db import get_supabase  # noqa: E402
from core.llm import get_embedding  # noqa: E402

async def backfill():
    supabase = get_supabase()
    
    print("Fetching whatsapp messages without embeddings...")
    res = supabase.table('whatsapp_messages').select('id, sender_name, message_text').is_('embedding', 'null').execute()
    messages = res.data or []
    print(f"Found {len(messages)} messages to embed.")
    
    count = 0
    for msg in messages:
        id_ = msg['id']
        sender = msg.get('sender_name') or 'Unknown'
        text = msg.get('message_text') or ''
        
        content_to_embed = f"From {sender}: {text}"
        
        try:
            emb = await get_embedding(content_to_embed)
            if emb and emb.vector:
                supabase.table('whatsapp_messages').update({'embedding': emb.vector}).eq('id', id_).execute()
                count += 1
                if count % 20 == 0:
                    print(f"Embedded {count}/{len(messages)}")
        except Exception as e:
            print(f"Error on msg {id_}: {e}")
            
    print(f"Done. Embedded {count} messages.")

if __name__ == "__main__":
    asyncio.run(backfill())