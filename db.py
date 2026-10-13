"""Слой работы с PostgreSQL (pg8000).

Все функции синхронные: из async-обработчиков их вызывают через utils.run_sync.
Каждая публичная функция при ошибке пишет её в лог и возвращает безопасное значение.
"""
from __future__ import annotations

import json
import logging
import secrets
from functools import wraps

import pg8000

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, DEFAULT_SETTINGS
from question_answer import CHARACTER_KEYS, HOUSE_KEYS

log = logging.getLogger(__name__)

# Кэши в памяти (бот работает одним процессом): настройки и список забаненных.
_settings_cache: dict[str, str] = {}
_banned_cache: set[int] = set()


# ==================== БАЗОВЫЕ ПОМОЩНИКИ ====================

def get_connection():
    return pg8000.connect(
        host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASSWORD
    )


def _safe(default=None):
    """Декоратор: ловит ошибки БД, пишет в лог и возвращает default."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                log.exception("Ошибка БД в %s", func.__name__)
                return default
        return wrapper
    return decorator


def _execute(statements, fetch=None):
    """Выполняет несколько запросов в одной транзакции.
    statements: список (sql, params). fetch: None | 'one' | 'all' — для последнего запроса."""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        for sql, params in statements:
            cursor.execute(sql, params)
        result = None
        if fetch == "one":
            result = cursor.fetchone()
        elif fetch == "all":
            result = cursor.fetchall()
        connection.commit()
        cursor.close()
        return result
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    finally:
        connection.close()


def _exec(sql, params=()):
    _execute([(sql, params)])


def _one(sql, params=()):
    return _execute([(sql, params)], fetch="one")


def _all(sql, params=()):
    return _execute([(sql, params)], fetch="all")


# ==================== СХЕМА БД ====================

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        preferences TEXT,
        memory TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS user_visits (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        visit_count INT DEFAULT 1,
        first_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_visit TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS quiz_results (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        house VARCHAR(50) NOT NULL,
        griffindor_score INT DEFAULT 0,
        slytherin_score INT DEFAULT 0,
        ravenclaw_score INT DEFAULT 0,
        hufflepuff_score INT DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS character_quiz_results (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        character VARCHAR(50) NOT NULL,
        harry_score INT DEFAULT 0,
        hermione_score INT DEFAULT 0,
        ron_score INT DEFAULT 0,
        dumbledore_score INT DEFAULT 0,
        snape_score INT DEFAULT 0,
        luna_score INT DEFAULT 0,
        hagrid_score INT DEFAULT 0,
        sirius_score INT DEFAULT 0,
        malfoy_score INT DEFAULT 0,
        voldemort_score INT DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        role VARCHAR(10) NOT NULL,
        content TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS user_events (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        event VARCHAR(64) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL
    )""",
]

# Миграции для баз, созданных старыми версиями бота: добавляют недостающие колонки.
_NEW_COLUMNS = {
    "users": {
        "username": "TEXT",
        "first_name": "TEXT",
        "preferences": "TEXT",
        "memory": "TEXT",
        "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "is_banned": "BOOLEAN NOT NULL DEFAULT FALSE",
        "last_name": "TEXT",
        "language_code": "TEXT",
        "is_premium": "BOOLEAN NOT NULL DEFAULT FALSE",
        "chat_type": "TEXT",
        "start_source": "TEXT",
        "last_command": "TEXT",
        "is_anonymized": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "user_visits": {
        "visit_count": "INT DEFAULT 1",
        "first_visit": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "last_visit": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
    "messages": {"timestamp": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"},
}
MIGRATIONS = [
    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}"
    for table, columns in _NEW_COLUMNS.items()
    for column, definition in columns.items()
]

# Склеиваем дубли строк в user_visits (если они были), чтобы можно было создать уникальный индекс.
DEDUPE = [
    """UPDATE user_visits v SET visit_count = s.total, first_visit = s.first_v, last_visit = s.last_v
       FROM (SELECT MAX(id) AS keep_id, SUM(visit_count) AS total,
                    MIN(first_visit) AS first_v, MAX(last_visit) AS last_v
             FROM user_visits GROUP BY user_id HAVING COUNT(*) > 1) s
       WHERE v.id = s.keep_id""",
    "DELETE FROM user_visits a USING user_visits b WHERE a.user_id = b.user_id AND a.id < b.id",
]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS user_visits_user_id_uq ON user_visits (user_id)",
    "CREATE INDEX IF NOT EXISTS messages_user_id_idx ON messages (user_id, id)",
    "CREATE INDEX IF NOT EXISTS messages_timestamp_idx ON messages (timestamp)",
    "CREATE INDEX IF NOT EXISTS user_events_user_id_idx ON user_events (user_id, id)",
    "CREATE INDEX IF NOT EXISTS user_events_created_idx ON user_events (created_at)",
]

# Что должно быть в БД — используется диагностикой в админ-панели.
EXPECTED_COLUMNS = {
    "users": {"user_id", "username", "first_name", "preferences", "memory",
              "created_at", "updated_at", "is_banned", "last_name", "language_code",
              "is_premium", "chat_type", "start_source", "last_command", "is_anonymized"},
    "user_visits": {"id", "user_id", "visit_count", "first_visit", "last_visit"},
    "quiz_results": {"id", "user_id", "house", "created_at", *(f"{k}_score" for k in HOUSE_KEYS)},
    "character_quiz_results": {"id", "user_id", "character", "created_at",
                               *(f"{k}_score" for k in CHARACTER_KEYS)},
    "messages": {"id", "user_id", "role", "content", "timestamp"},
    "user_events": {"id", "user_id", "event", "created_at"},
    "settings": {"setting_key", "setting_value"},
}

_schema_errors: list[str] = []


def _run_each(statements) -> list[str]:
    """Выполняет каждый запрос в отдельной транзакции: одна ошибка не ломает остальные."""
    errors = []
    for sql in statements:
        try:
            _exec(sql)
        except Exception as error:
            short = " ".join(sql.split())[:90]
            log.error("Миграция не выполнена: %s → %s", short, error)
            errors.append(f"{short} → {error}")
    return errors


@_safe(False)
def _load_settings() -> bool:
    _settings_cache.clear()
    _settings_cache.update({k: v for k, v in _all("SELECT setting_key, setting_value FROM settings")})
    return True


@_safe(False)
def _load_banned() -> bool:
    _banned_cache.clear()
    _banned_cache.update(row[0] for row in _all("SELECT user_id FROM users WHERE is_banned"))
    return True


@_safe(False)
def init_db() -> bool:
    """Создаёт и обновляет все таблицы, загружает кэши. Безопасно вызывать при каждом запуске.
    Возвращает False, если хоть что-то не удалось (подробности — в логе и в админ-панели)."""
    errors = _run_each(SCHEMA + MIGRATIONS + DEDUPE + INDEXES)
    _schema_errors[:] = errors
    loaded = _load_settings() and _load_banned()
    log.info("БД инициализирована (ошибок миграции: %d, настроек: %d, забаненных: %d)",
             len(errors), len(_settings_cache), len(_banned_cache))
    return not errors and loaded


@_safe({"ok": False, "error": "нет подключения к БД (см. лог)"})
def check_schema() -> dict:
    """Диагностика для админки: версия сервера, недостающие таблицы/колонки, размеры таблиц."""
    version = _one("SELECT version()")[0]
    present: dict[str, set[str]] = {}
    for table, column in _all(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()"
    ):
        present.setdefault(table, set()).add(column)

    missing = {}
    for table, columns in EXPECTED_COLUMNS.items():
        if table not in present:
            missing[table] = ["(таблицы нет)"]
        elif columns - present[table]:
            missing[table] = sorted(columns - present[table])

    counts = {}
    for table in EXPECTED_COLUMNS:  # имена таблиц — константы, не пользовательский ввод
        if table in present:
            counts[table] = _one(f"SELECT COUNT(*) FROM {table}")[0]
    return {
        "ok": True,
        "version": version.split(",")[0],
        "missing": missing,
        "counts": counts,
        "errors": list(_schema_errors),
    }


@_safe(0)
def cleanup_old_messages(days: int) -> int:
    """Удаляет историю ИИ-чата старше N дней. Возвращает число удалённых сообщений."""
    if days <= 0:
        return 0
    rows = _all("DELETE FROM messages WHERE timestamp < NOW() - INTERVAL '1 day' * %s RETURNING id", (days,))
    return len(rows)
@_safe(0)
def cleanup_old_events(days: int) -> int:
    """Удаляет журнал действий старше N дней. Возвращает число удалённых записей."""
    if days <= 0:
        return 0
    rows = _all("DELETE FROM user_events WHERE created_at < NOW() - INTERVAL '1 day' * %s RETURNING id", (days,))
    return len(rows)

# ==================== НАСТРОЙКИ (меняются из админки) ====================

def get_setting(key: str) -> str:
    return _settings_cache.get(key, DEFAULT_SETTINGS.get(key, ""))


def get_setting_bool(key: str) -> bool:
    return get_setting(key) == "1"


@_safe(False)
def set_setting(key: str, value) -> bool:
    _exec(
        "INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s) "
        "ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value",
        (key, str(value)),
    )
    _settings_cache[key] = str(value)
    return True


# ==================== ПОЛЬЗОВАТЕЛИ ====================

@_safe()
def user_to_db(user_id, username=None, first_name=None, last_name=None, language_code=None,
               is_premium=False, chat_type=None, event=None):
    """Создаёт/обновляет профиль пользователя (данные берутся из Telegram), увеличивает счётчик
    активности и, если передан event (например «cmd:start»), пишет его в журнал действий."""
    last_command = "/" + event[4:] if event and event.startswith("cmd:") else None
    statements = [
        (
            "INSERT INTO users (user_id, username, first_name, last_name, language_code, "
            "is_premium, chat_type, last_command) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username, "
            "first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, "
            "language_code = COALESCE(EXCLUDED.language_code, users.language_code), "
            "is_premium = EXCLUDED.is_premium, chat_type = EXCLUDED.chat_type, "
            "last_command = COALESCE(EXCLUDED.last_command, users.last_command), "
            "updated_at = CURRENT_TIMESTAMP",
            (user_id, username, first_name, last_name, language_code, bool(is_premium), chat_type, last_command),
        ),
        (
            "WITH upd AS (UPDATE user_visits SET visit_count = visit_count + 1, "
            "last_visit = CURRENT_TIMESTAMP WHERE user_id = %s RETURNING 1) "
            "INSERT INTO user_visits (user_id, visit_count) "
            "SELECT %s::bigint, 1 WHERE NOT EXISTS (SELECT 1 FROM upd)",
            (user_id, user_id),
        ),
    ]
    if event:
        statements.append(("INSERT INTO user_events (user_id, event) VALUES (%s, %s)", (user_id, event[:64])))
    _execute(statements)


@_safe()
def log_event(user_id, event):
    """Пишет действие (нажатие кнопки и т.п.) в журнал. Текст сообщений пользователя НЕ сохраняется."""
    if not event:
        return
    _exec(
        "INSERT INTO user_events (user_id, event) "
        "SELECT %s::bigint, %s::text WHERE EXISTS (SELECT 1 FROM users WHERE user_id = %s::bigint)",
        (user_id, event[:64], user_id),
    )


@_safe()
def set_start_source(user_id, source):
    """Запоминает, откуда пришёл пользователь (параметр ссылки t.me/бот?start=источник). Только первый раз."""
    _exec(
        "UPDATE users SET start_source = %s WHERE user_id = %s AND start_source IS NULL",
        (str(source)[:64], user_id),
    )



@_safe([])
def get_all_users_from_db(include_banned: bool = False):
    """Получатели рассылки: реальные пользователи (обезличенные записи не включаются)."""
    sql = "SELECT user_id FROM users WHERE NOT is_anonymized" + ("" if include_banned else " AND NOT is_banned")
    return [row[0] for row in _all(sql)]


@_safe(0)
def count_users() -> int:
    return _one("SELECT COUNT(*) FROM users WHERE NOT is_anonymized")[0]


@_safe()
def remove_user_from_db(user_id):
    _exec("DELETE FROM users WHERE user_id = %s", (user_id,))
    _banned_cache.discard(user_id)

@_safe(False)
def anonymize_user(user_id) -> bool:
    """Удаляет персональные данные пользователя и оставляет только обезличенную статистику.

    Стирается: ID, username, имя, фамилия, память ИИ, история диалогов, последняя команда, тип чата.
    Остаётся без привязки к человеку: результаты квизов, счётчики активности, журнал действий,
    язык, признак Premium, источник перехода, дата регистрации — под случайным служебным ID.
    Всё выполняется одной транзакцией: либо полностью, либо никак."""
    # Служебный ID заведомо больше любого реального ID Telegram и положительный (не похож на чат).
    fake_id = secrets.randbelow(4 * 10**18) + 5 * 10**18
    _execute([
        (
            "INSERT INTO users (user_id, language_code, is_premium, start_source, created_at, is_anonymized) "
            "SELECT %s::bigint, language_code, is_premium, start_source, created_at, TRUE "
            "FROM users WHERE user_id = %s::bigint",
            (fake_id, user_id),
        ),
        ("UPDATE user_visits SET user_id = %s WHERE user_id = %s", (fake_id, user_id)),
        ("UPDATE quiz_results SET user_id = %s WHERE user_id = %s", (fake_id, user_id)),
        ("UPDATE character_quiz_results SET user_id = %s WHERE user_id = %s", (fake_id, user_id)),
        ("UPDATE user_events SET user_id = %s WHERE user_id = %s", (fake_id, user_id)),
        ("DELETE FROM users WHERE user_id = %s", (user_id,)),  # каскадом удалит историю диалогов
    ])
    _banned_cache.discard(user_id)
    return True

@_safe(False)
def delete_user_data(user_id) -> bool:
    """Полностью удаляет пользователя и все связанные данные (каскадом): профиль, историю, результаты, журнал."""
    _exec("DELETE FROM users WHERE user_id = %s", (user_id,))
    _banned_cache.discard(user_id)
    return True

@_safe([])
def get_users_page(limit: int, offset: int):
    """Страница пользователей для админки: (user_id, username, first_name, is_banned)."""
    return _all(
        "SELECT user_id, username, first_name, is_banned FROM users WHERE NOT is_anonymized "
        "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
        (limit, offset),
    )


@_safe()
def get_user_info(user_id):
    user = _one(
        "SELECT user_id, username, first_name, last_name, language_code, is_premium, chat_type, "
        "start_source, last_command, is_banned, created_at FROM users WHERE user_id = %s",
        (user_id,),
    )
    if not user:
        return None
    visits = _one(
        "SELECT visit_count, last_visit FROM user_visits WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    messages = _one("SELECT COUNT(*) FROM messages WHERE user_id = %s", (user_id,))
    keys = ("user_id", "username", "first_name", "last_name", "language_code", "is_premium",
            "chat_type", "start_source", "last_command", "is_banned", "created_at")
    info = dict(zip(keys, user))
    info["is_banned"] = bool(info["is_banned"])
    info["is_premium"] = bool(info["is_premium"])
    info["visits"] = visits[0] if visits and visits[0] is not None else 0
    info["last_visit"] = visits[1] if visits else None
    info["messages"] = messages[0] if messages else 0
    return info


@_safe([])
def get_user_events(user_id, limit: int = 5):
    return _all(
        "SELECT event, created_at FROM user_events WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )


@_safe([])
def get_users_export():
    """Все пользователи для выгрузки в CSV."""
    return _all(
        "SELECT u.user_id, u.username, u.first_name, u.last_name, u.language_code, u.is_premium, "
        "u.chat_type, u.start_source, u.last_command, u.is_banned, u.created_at, "
        "COALESCE(v.visit_count, 0), v.last_visit "
        "FROM users u LEFT JOIN user_visits v ON v.user_id = u.user_id "
        "WHERE NOT u.is_anonymized ORDER BY u.created_at"
    )


def is_banned(user_id: int) -> bool:
    return user_id in _banned_cache


@_safe(False)
def set_user_banned(user_id: int, banned: bool) -> bool:
    _exec("UPDATE users SET is_banned = %s WHERE user_id = %s", (banned, user_id))
    if banned:
        _banned_cache.add(user_id)
    else:
        _banned_cache.discard(user_id)
    return True


@_safe({})
def get_bot_stats() -> dict:
    row = _one(
        "SELECT (SELECT COUNT(*) FROM users WHERE NOT is_anonymized), "
        "(SELECT COUNT(*) FROM users WHERE is_banned), "
        "(SELECT COUNT(*) FROM user_visits v JOIN users u ON u.user_id = v.user_id "
        " WHERE NOT u.is_anonymized AND v.last_visit > NOW() - INTERVAL '24 hours'), "
        "(SELECT COUNT(*) FROM messages), "
        "(SELECT COUNT(*) FROM quiz_results), "
        "(SELECT COUNT(*) FROM character_quiz_results), "
        "(SELECT COUNT(*) FROM users WHERE is_anonymized)"
    )
    keys = ("users", "banned", "active_24h", "messages", "house_quizzes", "character_quizzes", "anonymized")
    return dict(zip(keys, row))

@_safe({})
def get_extra_stats() -> dict:
    """Расширенная статистика: Premium, новые за неделю, языки, популярные действия за 7 дней."""
    row = _one(
        "SELECT (SELECT COUNT(*) FROM users WHERE is_premium AND NOT is_anonymized), "
        "(SELECT COUNT(*) FROM users WHERE NOT is_anonymized AND created_at > NOW() - INTERVAL '7 days')"
    )
    languages = _all(
        "SELECT COALESCE(language_code, '?'), COUNT(*) FROM users WHERE NOT is_anonymized "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
    )
    events = _all(
        "SELECT event, COUNT(*) FROM user_events WHERE created_at > NOW() - INTERVAL '7 days' "
        "GROUP BY event ORDER BY 2 DESC LIMIT 8"
    )
    return {
        "premium": row[0],
        "new_7d": row[1],
        "languages": [(lang, count) for lang, count in languages],
        "events": [(event, count) for event, count in events],
    }


# ==================== ВИЗИТЫ ====================

@_safe()
def get_user_visits(user_id):
    return _one(
        "SELECT visit_count, first_visit, last_visit FROM user_visits WHERE user_id = %s", (user_id,)
    )


@_safe([])
def get_all_visits():
    return _all(
        "SELECT u.user_id, u.username, u.first_name, v.visit_count, v.first_visit, v.last_visit "
        "FROM users u LEFT JOIN user_visits v ON u.user_id = v.user_id "
        "ORDER BY v.visit_count DESC NULLS LAST"
    )


# ==================== КВИЗЫ ====================

def _scores_insert(table: str, winner_column: str, keys: tuple[str, ...]) -> str:
    # keys — константы из question_answer, не пользовательский ввод.
    columns = ", ".join(f"{k}_score" for k in keys)
    placeholders = ", ".join(["%s"] * (len(keys) + 2))
    return f"INSERT INTO {table} (user_id, {winner_column}, {columns}) VALUES ({placeholders})"


_HOUSE_INSERT = _scores_insert("quiz_results", "house", HOUSE_KEYS)
_CHARACTER_INSERT = _scores_insert("character_quiz_results", "character", CHARACTER_KEYS)


@_safe(False)
def save_quiz_result(user_id, house, results) -> bool:
    _exec(_HOUSE_INSERT, (user_id, house, *[results.get(k, 0) for k in HOUSE_KEYS]))
    return True


@_safe(False)
def save_character_quiz_result(user_id, character, results) -> bool:
    _exec(_CHARACTER_INSERT, (user_id, character, *[results.get(k, 0) for k in CHARACTER_KEYS]))
    return True


@_safe([])
def get_user_quiz_results(user_id, limit: int = 5):
    return _all(
        "SELECT house, created_at FROM quiz_results WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )


@_safe([])
def get_user_character_quiz_results(user_id, limit: int = 5):
    return _all(
        "SELECT character, created_at FROM character_quiz_results "
        "WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )


@_safe({})
def get_quiz_stats() -> dict:
    return {house: count for house, count in _all(
        "SELECT house, COUNT(*) FROM quiz_results GROUP BY house ORDER BY COUNT(*) DESC")}


@_safe({})
def get_character_quiz_stats() -> dict:
    return {character: count for character, count in _all(
        "SELECT character, COUNT(*) FROM character_quiz_results GROUP BY character ORDER BY COUNT(*) DESC")}


# ==================== ПАМЯТЬ И ИСТОРИЯ ИИ ====================

@_safe(False)
def update_user_memory(user_id, preferences=None, memory=None) -> bool:
    """Обновляет только переданные поля (None — оставить как есть)."""
    _exec(
        "UPDATE users SET preferences = COALESCE(%s, preferences), memory = COALESCE(%s, memory), "
        "updated_at = CURRENT_TIMESTAMP WHERE user_id = %s",
        (preferences, memory, user_id),
    )
    return True


@_safe({"preferences": {}, "memory": ""})
def get_user_memory(user_id) -> dict:
    row = _one("SELECT preferences, memory FROM users WHERE user_id = %s", (user_id,))
    if not row:
        return {"preferences": {}, "memory": ""}
    try:
        prefs = json.loads(row[0]) if row[0] else {}
    except json.JSONDecodeError:
        prefs = {}
    return {"preferences": prefs, "memory": row[1] or ""}


@_safe(False)
def save_message(user_id, role, content) -> bool:
    content = "" if content is None else str(content)
    if not content.strip():
        return False
    _exec("INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)", (user_id, role, content))
    return True


@_safe([])
def get_recent_messages(user_id, limit=10):
    rows = _all(
        "SELECT role, content FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )
    return [{"role": role, "content": content} for role, content in reversed(rows)]


@_safe(False)
def clear_user_memory(user_id) -> bool:
    _execute([
        ("DELETE FROM messages WHERE user_id = %s", (user_id,)),
        ("UPDATE users SET preferences = NULL, memory = NULL WHERE user_id = %s", (user_id,)),
    ])
    return True