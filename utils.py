"""Мелкие общие помощники."""
from __future__ import annotations

import asyncio

from telegram.error import BadRequest


async def run_sync(func, *args):
    """Запускает синхронную функцию (БД, GigaChat) в потоке, чтобы не блокировать бота."""
    return await asyncio.to_thread(func, *args)


async def safe_edit(query, text: str, reply_markup=None) -> None:
    """edit_message_text без падения на «Message is not modified»."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as error:
        if "not modified" not in str(error).lower():
            raise


def split_text(text: str, limit: int = 4000) -> list[str]:
    """Режет длинный ответ на части (лимит Telegram — 4096 символов)."""
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]