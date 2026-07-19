"""Stream output adapters for channel-agnostic response streaming.

Currently supports Telegram via editMessageText.
Designed to be swappable for WebSocket (Flutter app) or SSE (Web UI) adapters
by implementing the same StreamAdapter interface.
"""

import asyncio
import os
from typing import Optional

import httpx

from core.lib.audit_logger import audit_log_sync


class StreamAdapter:
    """Base interface for streaming output to any channel.
    
    Usage:
        adapter = SomeAdapter(chat_id, ...)
        await adapter.send_header("🧠 From your vault:")
        async for token in response_stream:
            await adapter.send_chunk(token)
        await adapter.send_complete()
    """

    async def send_header(self, text: str) -> None:
        """Send the initial message. Returns nothing — subclasses track state."""
        raise NotImplementedError

    async def send_chunk(self, text: str) -> None:
        """Accumulate and forward a chunk of response text."""
        raise NotImplementedError

    async def send_complete(self) -> None:
        """Finalize the stream. Optional — subclasses may flush here."""
        pass


class TelegramStreamAdapter(StreamAdapter):
    """Stream adapter for Telegram that progressively edits a single message.
    
    Flow:
    1. send_header() sends the initial message via sendMessage → gets message_id
    2. send_chunk() appends text and calls editMessageText with the full accumulated text
    3. send_complete() does the final edit (no-op if last chunk already edited)
    
    Rate-limit awareness: Telegram allows ~30 edits/min per message.
    We batch edits to at most one per FLUSH_INTERVAL seconds.
    """
    
    FLUSH_INTERVAL = 0.5  # seconds between edits — stays under rate limits
    
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.message_id: Optional[int] = None
        self._accumulated = ""
        self._last_flush = 0.0
        self._complete = False
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self._bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN not set")
        self._http: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        
    async def __aenter__(self):
        self._http = httpx.AsyncClient()
        return self
        
    async def __aexit__(self, *args):
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
        return self._http

    async def send_header(self, text: str) -> None:
        """Send the initial message via sendMessage and store the message_id."""
        if self._complete:
            return
        self._accumulated = text
        client = await self._ensure_http()
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok") and data.get("result", {}).get("message_id"):
                    self.message_id = data["result"]["message_id"]
                else:
                    audit_log_sync("stream", "WARNING", f"send_header: unexpected response: {data}")
            elif resp.status_code == 400 and "can't parse entities" in resp.text.lower():
                # Retry without Markdown
                clean = text.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
                payload["text"] = clean
                payload.pop("parse_mode", None)
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok") and data.get("result", {}).get("message_id"):
                        self.message_id = data["result"]["message_id"]
            else:
                audit_log_sync("stream", "WARNING", f"send_header failed: HTTP {resp.status_code}")
        except Exception as e:
            audit_log_sync("stream", "WARNING", f"send_header exception: {e}")
        self._last_flush = asyncio.get_event_loop().time()

    async def send_chunk(self, text: str) -> None:
        """Append text and flush (edit message) if enough time has passed."""
        if self._complete or not text:
            return
        self._accumulated += text
        now = asyncio.get_event_loop().time()
        if now - self._last_flush >= self.FLUSH_INTERVAL:
            await self._flush()

    async def _flush(self) -> None:
        """Edit the Telegram message with current accumulated text."""
        if not self.message_id or not self._accumulated:
            return
        async with self._lock:
            client = await self._ensure_http()
            url = f"https://api.telegram.org/bot{self._bot_token}/editMessageText"
            payload = {
                "chat_id": self.chat_id,
                "message_id": self.message_id,
                "text": self._accumulated,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code == 400 and "can't parse entities" in resp.text.lower():
                    clean = self._accumulated.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
                    payload["text"] = clean
                    payload.pop("parse_mode", None)
                    resp = await client.post(url, json=payload)
                if resp.status_code not in (200, 429):
                    audit_log_sync("stream", "WARNING", f"_flush: HTTP {resp.status_code}")
            except Exception as e:
                audit_log_sync("stream", "WARNING", f"_flush exception: {e}")
            self._last_flush = asyncio.get_event_loop().time()

    async def flush_text(self, text: str) -> None:
        """Replace the entire message text and flush.
        
        Useful for error fallback: if streaming fails, replace partial output
        with a graceful error message before the session cleans up.
        """
        self._accumulated = text
        await self._flush()

    async def send_complete(self) -> None:
        """Final flush to ensure the last chunk is visible."""
        if self._complete:
            return
        self._complete = True
        await self._flush()

    @property
    def accumulated_text(self) -> str:
        return self._accumulated
