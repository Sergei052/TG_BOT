"""Админ-панель внутри Telegram: всё управление выбирается кнопками."""
from __future__ import annotations

import asyncio
import csv
import io
import logging

from telegram import BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from config import AI_STYLES, GIGA_MODELS, HISTORY_OPTIONS, is_admin
from db import (
    get_extra_stats,
    get_user_events,
    check_schema,
    get_users_export,
    init_db,
    clear_user_memory,
    count_users,
    get_all_users_from_db,
    anonymize_user,
    get_bot_stats,
    get_character_quiz_stats,
    get_quiz_stats,
    get_setting,
    get_setting_bool,
    get_user_info,
    get_users_page,
    set_setting,
    set_user_banned,
)
from question_answer import HOGWARTS_CHARACTERS, HOUSES
from utils import run_sync, safe_edit

log = logging.getLogger(__name__)

ADMIN_COMMANDS = [
    BotCommand("admin", "Админ-панель"),
    BotCommand("users", "Пользователи"),
    BotCommand("broadcast", "Рассылка"),
]

USERS_PER_PAGE = 8
TOGGLES = [("ai_enabled", "🤖 ИИ-помощник"), ("quiz_enabled", "🧙 Квизы"), ("echo_enabled", "🔁 Эхо-ответы")]

# Какие значения вообще можно записать в настройки (защита от подделанного callback_data)
ALLOWED_SETTINGS = {
    "ai_enabled": {"0", "1"},
    "quiz_enabled": {"0", "1"},
    "echo_enabled": {"0", "1"},
    "giga_model": set(GIGA_MODELS),
    "history_limit": {str(x) for x in HISTORY_OPTIONS},
    "ai_style": set(AI_STYLES),
}

HOME_TEXT = "🛠 Админ-панель\n\nВыберите раздел:"


def _home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [Btn("📊 Статистика", callback_data="adm:stats"), Btn("👥 Пользователи", callback_data="adm:users:0")],
        [Btn("📢 Рассылка", callback_data="adm:bc"), Btn("⚙️ Настройки", callback_data="adm:settings")],
        [Btn("🏆 Итоги квизов", callback_data="adm:quizstats"), Btn("📥 Экспорт CSV", callback_data="adm:export")],
        [Btn("🩺 Диагностика БД", callback_data="adm:dbcheck")],
        [Btn("✖️ Закрыть", callback_data="adm:close")],
    ])


def _back_kb(target: str = "adm:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn("⬅️ Назад", callback_data=target)]])


# ==================== КОМАНДЫ ====================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
    await update.message.reply_text(HOME_TEXT, reply_markup=_home_kb())


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
    total = await run_sync(count_users)
    await update.message.reply_text(
        f"👥 Всего пользователей в боте: {total}",
        reply_markup=InlineKeyboardMarkup([[Btn("Открыть список", callback_data="adm:users:0")]]),
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/broadcast текст — сразу к предпросмотру; /broadcast без текста — бот попросит текст."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет прав на рассылку.")
        return
    text = " ".join(context.args or []).strip()
    if text:
        await _show_broadcast_preview(update.message, context, text)
    else:
        context.user_data["admin_state"] = "await_broadcast"
        await update.message.reply_text("📢 Отправьте следующим сообщением текст рассылки.\nОтмена: /cancel")


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Вызывается из corner.echo. True — сообщение обработано как текст рассылки."""
    if context.user_data.get("admin_state") != "await_broadcast" or not is_admin(update.effective_user.id):
        return False
    text = update.message.text.strip()
    if len(text) > 4000:
        await update.message.reply_text("⚠️ Слишком длинный текст (максимум 4000 символов). Сократите и отправьте снова.")
        return True
    await _show_broadcast_preview(update.message, context, text)
    return True


# ==================== МАРШРУТИЗАТОР КНОПОК ====================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("❌ Нет прав.", show_alert=True)
        return
    _, section, *args = query.data.split(":")
    handler = SECTIONS.get(section)
    alert = None
    if handler:
        try:
            alert = await handler(query, context, args)
        except (ValueError, IndexError):
            alert = "Некорректные данные кнопки."
    await query.answer(alert, show_alert=bool(alert))


async def _home(query, context, args):
    context.user_data.pop("admin_state", None)
    await safe_edit(query, HOME_TEXT, _home_kb())


async def _close(query, context, args):
    await query.message.delete()


async def _noop(query, context, args):
    return None


# ==================== СТАТИСТИКА ====================

async def _stats(query, context, args):
    s = await run_sync(get_bot_stats)
    if not s:
        return "⚠️ База данных недоступна."
    extra = await run_sync(get_extra_stats)
    lines = [
        "📊 Статистика бота", "",
        f"👥 Пользователей: {s['users']}",
        f"🚫 Заблокировано: {s['banned']}",
        f"🟢 Активны за 24 часа: {s['active_24h']}",
        f"💬 Сообщений ИИ в истории: {s['messages']}",
        f"🏰 Пройдено квизов факультетов: {s['house_quizzes']}",
        f"🪄 Пройдено квизов персонажей: {s['character_quizzes']}",
        f"👻 Обезличенных записей: {s['anonymized']}",
    ]
    if extra:
        lines += ["", f"🆕 Новых за 7 дней: {extra['new_7d']}", f"⭐ С Telegram Premium: {extra['premium']}"]
        if extra["languages"]:
            lines.append("🌍 Языки: " + ", ".join(f"{lang} — {count}" for lang, count in extra["languages"]))
        if extra["events"]:
            lines += ["", "🔥 Популярные действия за 7 дней:"]
            lines += [f"• {event}: {count}" for event, count in extra["events"]]
    await safe_edit(query, "\n".join(lines), _back_kb())


async def _quizstats(query, context, args):
    houses = await run_sync(get_quiz_stats)
    chars = await run_sync(get_character_quiz_stats)
    lines = ["🏆 Итоги квизов", "\nФакультеты:"]
    lines += [f"• {HOUSES.get(k, {}).get('name', k)}: {v}" for k, v in houses.items()] or ["• пока нет данных"]
    lines.append("\nПерсонажи:")
    lines += [f"• {HOGWARTS_CHARACTERS.get(k, {}).get('name', k)}: {v}" for k, v in chars.items()] or ["• пока нет данных"]
    await safe_edit(query, "\n".join(lines), _back_kb())


# ==================== ПОЛЬЗОВАТЕЛИ ====================

async def _users(query, context, args):
    total = await run_sync(count_users)
    pages = max((total + USERS_PER_PAGE - 1) // USERS_PER_PAGE, 1)
    page = min(max(int(args[0]) if args else 0, 0), pages - 1)
    rows = await run_sync(get_users_page, USERS_PER_PAGE, page * USERS_PER_PAGE)

    keyboard = []
    for user_id, username, first_name, banned in rows:
        name = first_name or (f"@{username}" if username else "без имени")
        label = f"{'🚫 ' if banned else ''}{name[:20]} · {user_id}"
        keyboard.append([Btn(label, callback_data=f"adm:user:{user_id}:{page}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️", callback_data=f"adm:users:{page - 1}"))
    nav.append(Btn(f"{page + 1}/{pages}", callback_data="adm:noop"))
    if page < pages - 1:
        nav.append(Btn("➡️", callback_data=f"adm:users:{page + 1}"))
    keyboard += [nav, [Btn("⬅️ В меню", callback_data="adm:home")]]
    await safe_edit(query, f"👥 Пользователи (всего: {total})\nНажмите на пользователя:", InlineKeyboardMarkup(keyboard))


def _fmt(value) -> str:
    return f"{value:%d.%m.%Y %H:%M}" if value else "—"


async def _render_user(query, user_id: int, page: int):
    info = await run_sync(get_user_info, user_id)
    if not info:
        await safe_edit(query, "Пользователь не найден.", _back_kb(f"adm:users:{page}"))
        return
    events = await run_sync(get_user_events, user_id)
    full_name = " ".join(p for p in (info["first_name"], info["last_name"]) if p) or "без имени"
    text = (
        f"👤 {full_name}{' (@' + info['username'] + ')' if info['username'] else ''}\n"
        f"ID: {info['user_id']}\n"
        f"Статус: {'🚫 заблокирован' if info['is_banned'] else '✅ активен'}\n"
        f"Язык: {info['language_code'] or '—'} · Premium: {'да' if info['is_premium'] else 'нет'}\n"
        f"Последний тип чата: {info['chat_type'] or '—'}\n"
        f"Пришёл из: {info['start_source'] or '—'}\n"
        f"Регистрация: {_fmt(info['created_at'])}\n"
        f"Активность (сообщений боту): {info['visits']}\n"
        f"Последняя активность: {_fmt(info['last_visit'])}\n"
        f"Последняя команда: {info['last_command'] or '—'}\n"
        f"Сообщений в истории ИИ: {info['messages']}"
    )
    if events:
        text += "\n\nПоследние действия:\n" + "\n".join(f"• {event} — {created:%d.%m %H:%M}" for event, created in events)
    ban_btn = (Btn("✅ Разблокировать", callback_data=f"adm:unban:{user_id}:{page}") if info["is_banned"]
               else Btn("🚫 Заблокировать", callback_data=f"adm:ban:{user_id}:{page}"))
    keyboard = InlineKeyboardMarkup([
        [ban_btn],
        [Btn("🧹 Очистить память ИИ", callback_data=f"adm:wipe:{user_id}:{page}")],
        [Btn("👻 Обезличить (удалить данные)", callback_data=f"adm:anon:{user_id}:{page}")],
        [Btn("⬅️ К списку", callback_data=f"adm:users:{page}")],
    ])
    await safe_edit(query, text, keyboard)


async def _user(query, context, args):
    await _render_user(query, int(args[0]), int(args[1]) if len(args) > 1 else 0)


async def _ban(query, context, args, banned=True):
    user_id, page = int(args[0]), int(args[1])
    if banned and is_admin(user_id):
        return "Администратора заблокировать нельзя."
    await run_sync(set_user_banned, user_id, banned)
    await _render_user(query, user_id, page)
    return "🚫 Заблокирован" if banned else "✅ Разблокирован"


async def _unban(query, context, args):
    return await _ban(query, context, args, banned=False)


async def _wipe(query, context, args):
    user_id, page = int(args[0]), int(args[1])
    await run_sync(clear_user_memory, user_id)
    await _render_user(query, user_id, page)
    return "🧹 Память пользователя очищена"


# ==================== НАСТРОЙКИ ====================

def _settings_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, label in TOGGLES:
        on = get_setting_bool(key)
        rows.append([Btn(f"{label}: {'✅ вкл' if on else '⛔ выкл'}",
                         callback_data=f"adm:set:{key}:{0 if on else 1}")])

    def choice_row(key, options):
        current = get_setting(key)
        return [Btn(("✅ " if str(value) == current else "") + label, callback_data=f"adm:set:{key}:{value}")
                for value, label in options]

    rows.append([Btn("— Модель GigaChat —", callback_data="adm:noop")])
    rows.append(choice_row("giga_model", [(m, m.replace("GigaChat-", "")) for m in GIGA_MODELS]))
    rows.append([Btn("— Сколько сообщений истории помнить —", callback_data="adm:noop")])
    rows.append(choice_row("history_limit", [(n, str(n)) for n in HISTORY_OPTIONS]))
    rows.append([Btn("— Стиль ответов ИИ —", callback_data="adm:noop")])
    styles = list(AI_STYLES.items())
    for i in range(0, len(styles), 2):
        rows.append(choice_row("ai_style", [(k, v[0]) for k, v in styles[i:i + 2]]))
    rows.append([Btn("⬅️ Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


async def _settings(query, context, args):
    await safe_edit(query, "⚙️ Настройки бота\nИзменения применяются сразу:", _settings_kb())


async def _set(query, context, args):
    key, value = args[0], args[1]
    if value not in ALLOWED_SETTINGS.get(key, set()):
        return "Недопустимое значение."
    if not await run_sync(set_setting, key, value):
        return "⚠️ Не удалось сохранить настройку."
    await safe_edit(query, "⚙️ Настройки бота\nИзменения применяются сразу:", _settings_kb())
    return "Сохранено ✅"


# ==================== РАССЫЛКА ====================

async def _show_broadcast_preview(message, context, text: str):
    context.user_data["admin_state"] = None
    context.user_data["broadcast_draft"] = text
    total = len(await run_sync(get_all_users_from_db))
    keyboard = InlineKeyboardMarkup([[
        Btn(f"✅ Отправить ({total})", callback_data="adm:bc_send"),
        Btn("❌ Отмена", callback_data="adm:bc_cancel"),
    ]])
    await message.reply_text(f"📢 Предпросмотр рассылки:\n\n{text}\n\n— — —\nПолучателей: {total}", reply_markup=keyboard)


async def _bc_ask(query, context, args):
    context.user_data["admin_state"] = "await_broadcast"
    await safe_edit(query, "📢 Рассылка\n\nОтправьте следующим сообщением текст рассылки.\nОтмена: /cancel", _back_kb())


async def _bc_cancel(query, context, args):
    context.user_data.pop("broadcast_draft", None)
    context.user_data.pop("admin_state", None)
    await safe_edit(query, "❌ Рассылка отменена.", _back_kb())


async def _bc_send(query, context, args):
    text = context.user_data.pop("broadcast_draft", None)
    if not text:
        return "Черновик не найден — начните рассылку заново."
    recipients = await run_sync(get_all_users_from_db)
    await safe_edit(query, f"⏳ Рассылка запущена, получателей: {len(recipients)}…")
    # В фоне, чтобы бот не «замирал» на время рассылки
    context.application.create_task(_do_broadcast(context.bot, query.message.chat_id, recipients, text))


async def _do_broadcast(bot, admin_chat_id: int, recipients: list[int], text: str):
    ok = blocked = failed = 0
    for user_id in recipients:
        for attempt in (1, 2):
            try:
                await bot.send_message(user_id, text)
                ok += 1
            except RetryAfter as error:
                delay = error.retry_after
                delay = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
                await asyncio.sleep(delay + 1)
                if attempt == 1:
                    continue
                failed += 1
            except Forbidden:
                blocked += 1
            except TelegramError as error:
                failed += 1
                log.warning("Рассылка: ошибка для %s: %s", user_id, error)
            break
        await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram
    await bot.send_message(
        admin_chat_id,
        f"✅ Рассылка завершена\n📬 Доставлено: {ok}\n🚫 Бот заблокирован: {blocked}\n❌ Ошибок: {failed}",
    )

# ==================== БАЗА ДАННЫХ: ДИАГНОСТИКА И ЭКСПОРТ ====================

async def _dbcheck(query, context, args):
    info = await run_sync(check_schema)
    keyboard = InlineKeyboardMarkup([
        [Btn("🔧 Применить миграции", callback_data="adm:dbfix"), Btn("🔄 Обновить", callback_data="adm:dbcheck")],
        [Btn("⬅️ Назад", callback_data="adm:home")],
    ])
    if not info.get("ok"):
        await safe_edit(query, f"🩺 Диагностика БД\n\n❌ {info.get('error', 'ошибка')}", keyboard)
        return

    lines = ["🩺 Диагностика БД", "", f"✅ Подключение: {info['version']}", ""]
    if info["missing"]:
        lines.append("⚠️ Не хватает в схеме:")
        lines += [f"• {table}: {', '.join(columns)}" for table, columns in info["missing"].items()]
    else:
        lines.append("✅ Все таблицы и колонки на месте")
    lines += ["", "Записей в таблицах:"]
    lines += [f"• {table}: {count}" for table, count in info["counts"].items()]
    if info["errors"]:
        lines += ["", "❌ Ошибки последнего запуска миграций:"]
        lines += [f"• {error[:150]}" for error in info["errors"][:5]]
    await safe_edit(query, "\n".join(lines)[:4000], keyboard)


async def _dbfix(query, context, args):
    ok = await run_sync(init_db)
    await _dbcheck(query, context, args)
    return "✅ Миграции применены" if ok else "⚠️ Часть миграций не удалась — смотрите отчёт"


async def _export(query, context, args):
    rows = await run_sync(get_users_export)
    if not rows:
        return "Нет данных для выгрузки."
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["user_id", "username", "first_name", "last_name", "language", "premium", "chat_type",
                     "source", "last_command", "banned", "registered", "activity", "last_activity"])
    for (user_id, username, first_name, last_name, language, premium, chat_type, source,
         last_command, banned, created, visits, last) in rows:
        writer.writerow([
            user_id, username or "", first_name or "", last_name or "", language or "",
            "yes" if premium else "no", chat_type or "", source or "", last_command or "",
            "yes" if banned else "no",
            f"{created:%Y-%m-%d %H:%M}" if created else "", visits, f"{last:%Y-%m-%d %H:%M}" if last else "",
        ])
    data = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))  # BOM, чтобы Excel не ломал кириллицу
    await context.bot.send_document(
        chat_id=query.message.chat_id, document=data, filename="users.csv", caption=f"📥 Пользователей: {len(rows)}"
    )
    return "Файл отправлен"

async def _anon_ask(query, context, args):
    user_id, page = int(args[0]), int(args[1])
    if is_admin(user_id):
        return "Администратора обезличить нельзя."
    info = await run_sync(get_user_info, user_id)
    if info and info["is_banned"]:
        return "Заблокированного обезличивать нельзя: он вернётся без бана. Сначала разблокируйте."
    await safe_edit(
        query,
        f"👻 Обезличить пользователя {user_id}?\n\n"
        "Будут стёрты ID, username, имя, память и история диалогов с ИИ. Результаты квизов и счётчики "
        "останутся как анонимная статистика. Действие необратимо.",
        InlineKeyboardMarkup([
            [Btn("✅ Да, обезличить", callback_data=f"adm:anon_yes:{user_id}:{page}")],
            [Btn("❌ Отмена", callback_data=f"adm:user:{user_id}:{page}")],
        ]),
    )


async def _anon_yes(query, context, args):
    user_id, page = int(args[0]), int(args[1])
    if is_admin(user_id):
        return "Администратора обезличить нельзя."
    info = await run_sync(get_user_info, user_id)
    if info and info["is_banned"]:
        return "Заблокированного обезличивать нельзя."
    ok = await run_sync(anonymize_user, user_id)
    await safe_edit(query, "👻 Пользователь обезличен." if ok else "⚠️ Не удалось обезличить.", _back_kb(f"adm:users:{page}"))

SECTIONS = {
    "home": _home, "close": _close, "noop": _noop,
    "stats": _stats, "quizstats": _quizstats,
    "users": _users, "user": _user, "ban": _ban, "unban": _unban, "wipe": _wipe,
    "anon": _anon_ask, "anon_yes": _anon_yes,
    "settings": _settings, "set": _set,
    "bc": _bc_ask, "bc_cancel": _bc_cancel, "bc_send": _bc_send,
    "dbcheck": _dbcheck, "dbfix": _dbfix, "export": _export,
}