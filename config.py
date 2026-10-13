"""Конфигурация проекта: всё, что берётся из .env, и значения по умолчанию."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    return {int(p) for p in raw.replace(";", ",").split(",") if p.strip().isdigit()}


# --- Telegram ---------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or os.getenv("Token")
# ID администраторов через запятую. OWNER_ID оставлен для совместимости.
ADMIN_IDS: set[int] = _parse_ids(os.getenv("ADMIN_IDS", "")) | _parse_ids(os.getenv("OWNER_ID", ""))

# --- PostgreSQL -------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "telegram_bot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
# Сколько дней хранить историю ИИ-чата (0 — хранить всегда)
MESSAGE_RETENTION_DAYS = int(os.getenv("MESSAGE_RETENTION_DAYS", "30"))
# Сколько дней хранить журнал действий (какие команды/кнопки нажимали), 0 — всегда
EVENTS_RETENTION_DAYS = int(os.getenv("EVENTS_RETENTION_DAYS", "90"))
# --- GigaChat ---------------------------------------------------------------
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_VERIFY_SSL = os.getenv("GIGACHAT_VERIFY_SSL", "false").lower() == "true"

# --- Параметры, которые админ выбирает кнопками -----------------------------
GIGA_MODELS = ["GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max"]
HISTORY_OPTIONS = [2, 6, 10, 20]
AI_COOLDOWN_SEC = 3  # пауза между запросами к ИИ от одного пользователя

AI_STYLES = {
    "friendly": ("😊 Дружелюбный", "Ты — дружелюбный ассистент в Telegram-боте. Отвечай кратко и понятно."),
    "formal": ("🎩 Деловой", "Ты — вежливый ассистент. Отвечай сдержанно, чётко и по делу."),
    "fun": ("🤪 С юмором", "Ты — весёлый ассистент в Telegram-боте. Отвечай кратко и с лёгким юмором."),
    "teacher": ("👨‍🏫 Учитель", "Ты — терпеливый учитель. Объясняй простыми словами и приводи примеры."),
}

DEFAULT_SETTINGS = {
    "ai_enabled": "1",
    "quiz_enabled": "1",
    "echo_enabled": "1",
    "giga_model": GIGA_MODELS[0],
    "history_limit": "6",
    "ai_style": "friendly",
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS