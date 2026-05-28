"""Telegram notification sink."""
from __future__ import annotations
import asyncio
import httpx
from config import settings


async def notify(title: str, body: str) -> None:
    """Fire-and-forget notification."""
    if settings.NOTIFY_TELEGRAM_TOKEN and settings.NOTIFY_TELEGRAM_CHAT_ID:
        asyncio.create_task(_telegram(title, body))


async def _telegram(title: str, body: str) -> None:
    text = f"*{_esc(title)}*\n{_esc(body)}"
    url = f"https://api.telegram.org/bot{settings.NOTIFY_TELEGRAM_TOKEN}/sendMessage"
    proxy = settings.YTS_API_PROXY or None
    try:
        async with httpx.AsyncClient(timeout=10, proxy=proxy) as cli:
            await cli.post(url, json={
                "chat_id": settings.NOTIFY_TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "MarkdownV2",
            })
    except Exception as e:
        from store import log_event
        log_event("warn", f"Telegram notify failed: {e}")


def _esc(s: str) -> str:
    """Escape MarkdownV2 special characters."""
    for c in r"\_*[]()~#+-=|{}.!":
        s = s.replace(c, "\\" + c)
    return s
