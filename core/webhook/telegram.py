import os
import asyncio
import mimetypes
import httpx


def _chunk_message(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = text.rfind(' ', 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks

async def send_telegram(chat_id: int, message_text: str, show_keyboard: bool = True, inline_keyboard: list = None):
    chunks = _chunk_message(message_text)
    total = len(chunks)
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
    success = True
    last_failed = -1
    async with httpx.AsyncClient() as client:
        for i, chunk in enumerate(chunks):
            suffix = f"({i+1}/{total})"
            if total > 1:
                if i == 0:
                    nl = chunk.find('\n')
                    if nl != -1:
                        chunk = chunk[:nl] + " " + suffix + chunk[nl:]
                    else:
                        chunk = chunk + " " + suffix
                else:
                    chunk = suffix + "\n\n" + chunk
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            if inline_keyboard and i == total - 1:
                payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
            elif show_keyboard and i == total - 1:
                payload["reply_markup"] = {
                    "keyboard": [
                        [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
                        [{"text": "🚀 Cluster"}, {"text": "📚 Library"}],
                        [{"text": "🧭 Season Context"}, {"text": "🔓 Vault"}],
                        [{"text": "📊 Status"}]
                    ],
                    "resize_keyboard": True,
                    "persistent": True,
                }
            # Send with one retry
            for attempt in range(2):
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 400 and "can't parse entities" in resp.text.lower():
                        clean = chunk.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
                        payload["text"] = clean
                        payload.pop("parse_mode", None)
                        resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        break
                    if attempt == 0:
                        await asyncio.sleep(1)
                except Exception as e:
                    if attempt == 0:
                        print(f"Telegram chunk {i+1}/{total} retrying: {e}")
                        await asyncio.sleep(1)
                    else:
                        print(f"Telegram chunk {i+1}/{total} failed after retry: {e}")
                        success = False
                        last_failed = i
    # Notify user if some chunks were lost
    if not success and last_failed >= 0 and last_failed < total - 1:
        try:
            note = f"⚠️ *Response incomplete* — part {last_failed+2}/{total} failed to send."
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": chat_id, "text": note, "parse_mode": "Markdown"})
        except Exception:
            pass
    return success

async def download_telegram_file(file_id: str) -> tuple[bytes, str]:
    """Download file from Telegram and return (bytes, mime_type)."""
    try:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

        async with httpx.AsyncClient() as client:
            file_info = await client.get(f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}")
            file_data = file_info.json()

            if not file_data.get('ok'):
                raise Exception(f"Telegram API error: {file_data}")

            file_path = file_data['result']['file_path']
            mime_type = file_data['result'].get('mime_type', 'application/octet-stream')

            if mime_type == 'application/octet-stream':
                guessed, _ = mimetypes.guess_type(file_path)
                if guessed:
                    mime_type = guessed

            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            file_resp = await client.get(download_url)
            file_resp.raise_for_status()

            return file_resp.content, mime_type
    except Exception as e:
        raise Exception(f"Failed to download Telegram file {file_id}: {e}")

KEYBOARD = {
    "keyboard": [
        [{"text": "🔴 Urgent"}, {"text": "📋 Brief"}],
        [{"text": "🚀 Cluster"}, {"text": "📚 Library"}],
        [{"text": "🧭 Season Context"}, {"text": "🔓 Vault"}],
        [{"text": "📊 Status"}]
    ],
    "resize_keyboard": True,
    "persistent": True
}



async def answer_callback_query(callback_query_id: str, text: str = None):
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{telegram_bot_token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    
    import httpx
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload)
        except Exception as e:
            print(f"Failed to answer callback query: {e}")
