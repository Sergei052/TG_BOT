"""Обёртка над GigaChat и простое извлечение фактов о пользователе в память."""
from __future__ import annotations

import json
import logging
import re

from config import GIGACHAT_CREDENTIALS, GIGACHAT_SCOPE, GIGACHAT_VERIFY_SSL
from db import get_user_memory, update_user_memory

log = logging.getLogger(__name__)
_client = None


def _get_client():
    """Клиент создаётся лениво: бот запускается, даже если ключ GigaChat не задан."""
    global _client
    if _client is None:
        if not GIGACHAT_CREDENTIALS:
            raise RuntimeError("GIGACHAT_CREDENTIALS не задан в .env")
        from gigachat import GigaChat

        _client = GigaChat(
            credentials=GIGACHAT_CREDENTIALS,
            scope=GIGACHAT_SCOPE,
            verify_ssl_certs=GIGACHAT_VERIFY_SSL,
        )
    return _client


def get_giga_response(user_query: str, system_prompt: str | None = None,
                      history: list | None = None, model: str | None = None) -> str | None:
    """Отправляет запрос в GigaChat. Возвращает текст ответа или None при ошибке."""
    try:
        from gigachat.models import Chat, Messages, MessagesRole

        messages = []
        if system_prompt:
            messages.append(Messages(role=MessagesRole.SYSTEM, content=system_prompt))
        for msg in history or []:
            role = MessagesRole.USER if msg["role"] == "user" else MessagesRole.ASSISTANT
            messages.append(Messages(role=role, content=msg["content"]))
        messages.append(Messages(role=MessagesRole.USER, content=user_query))

        response = _get_client().chat(Chat(messages=messages, model=model or None))
        return response.choices[0].message.content
    except Exception:
        log.exception("Ошибка GigaChat")
        return None


def extract_and_update_memory(user_id: int, text: str) -> None:
    """Достаёт простые факты («меня зовут …», «я люблю …») и сохраняет в preferences."""
    try:
        name_match = re.search(r"меня зовут\s+([А-Яа-яЁёA-Za-z-]+)", text, re.IGNORECASE)
        love_match = re.search(r"\bя люблю\s+(.+)", text, re.IGNORECASE)
        if not name_match and not love_match:
            return

        prefs = get_user_memory(user_id).get("preferences", {})
        if name_match:
            prefs["имя"] = name_match.group(1)
        if love_match:
            love = love_match.group(1).strip()[:100]
            loves = prefs.setdefault("любит", [])
            if love not in loves:
                loves.append(love)
                del loves[:-20]  # храним не больше 20 последних
        update_user_memory(user_id, preferences=json.dumps(prefs, ensure_ascii=False))
    except Exception:
        log.exception("Ошибка извлечения памяти")