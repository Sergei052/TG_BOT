"""Пользовательские обработчики бота: команды, меню, квизы, ИИ-чат."""
from __future__ import annotations

import logging
import random
import secrets
import time
from datetime import datetime

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import ApplicationHandlerStop, ContextTypes

from admin import ADMIN_COMMANDS, handle_admin_text
from config import ADMIN_IDS, AI_COOLDOWN_SEC, AI_STYLES,EVENTS_RETENTION_DAYS, MESSAGE_RETENTION_DAYS, is_admin
from db import (
    cleanup_old_events,
    anonymize_user,
    log_event,
    set_start_source,
    cleanup_old_messages,
    clear_user_memory,
    count_users,
    get_recent_messages,
    get_setting,
    get_setting_bool,
    get_user_character_quiz_results,
    get_user_memory,
    get_user_quiz_results,
    init_db,
    is_banned,
    save_character_quiz_result,
    save_message,
    save_quiz_result,
    user_to_db,
)
from gigachat_utils import extract_and_update_memory, get_giga_response
from question_answer import (
    CHARACTER_QUESTIONS,
    HOGWARTS_CHARACTERS,
    HOUSE_QUESTIONS,
    HOUSES,
)
from utils import run_sync, safe_edit, split_text

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand("start", "Приветствие"),
    BotCommand("menu", "Главное меню"),
    BotCommand("help", "Справка"),
    BotCommand("quiz", "Квиз: твой факультет"),
    BotCommand("hogwarts", "Квиз: твой персонаж"),
    BotCommand("ai", "Спросить ИИ"),
    BotCommand("profile", "Что бот знает обо мне"),
    BotCommand("myresults", "Мои результаты квизов"),
    BotCommand("forget", "Удалить мою память"),
    BotCommand("privacy", "Какие данные хранит бот"),
    BotCommand("time", "Текущее время"),
    BotCommand("random", "Случайное число и цвет"),
    BotCommand("cancel", "Отменить текущее действие"),
]

COLORS = ["красный", "оранжевый", "жёлтый", "зелёный", "голубой", "синий",
          "фиолетовый", "розовый", "коричневый", "чёрный", "белый", "серый"]


# ==================== СЛУЖЕБНОЕ ====================

def _callback_event(data: str | None) -> str | None:
    """Какие нажатия кнопок попадают в журнал (без служебных админских и без ответов на вопросы)."""
    if not data:
        return None
    prefix = data.split(":")[0]
    if prefix == "menu":
        return data
    if prefix in ("house", "character"):
        return "quiz_answer"
    return None


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполняется перед всеми обработчиками (group=-1):
    блокирует забаненных, обновляет профиль пользователя и пишет действие в журнал."""
    user = update.effective_user
    if user is None:
        return
    if not is_admin(user.id) and is_banned(user.id):
        if update.callback_query:
            await update.callback_query.answer("🚫 Доступ ограничен администратором.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("🚫 Доступ ограничен администратором.")
        raise ApplicationHandlerStop

    if update.message:
        text = update.message.text or ""
        event = None
        if text.startswith("/"):
            event = "cmd:" + text.split()[0].split("@")[0][1:].lower()  # только имя команды, без аргументов
        chat = update.effective_chat
        await run_sync(
            user_to_db, user.id, user.username, user.first_name, user.last_name,
            user.language_code, bool(user.is_premium), chat.type if chat else None, event,
        )
    elif update.callback_query:
        await run_sync(log_event, user.id, _callback_event(update.callback_query.data))

async def post_init(application):
    """Запускается один раз при старте: БД, список команд, уведомление админам."""
    db_ok = await run_sync(init_db)
    if not db_ok:
        log.error("Есть проблемы с БД — см. сообщения выше и админ-панель → «Диагностика БД»")
    removed = await run_sync(cleanup_old_messages, MESSAGE_RETENTION_DAYS)
    if removed:
        log.info("Удалено старых сообщений из истории ИИ: %d", removed)
    removed_events = await run_sync(cleanup_old_events, EVENTS_RETENTION_DAYS)
    if removed_events:
        log.info("Удалено старых записей журнала действий: %d", removed_events)

    await application.bot.set_my_commands(USER_COMMANDS)
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не задан — админ-панель недоступна")
    total = await run_sync(count_users)
    status = "✅ Бот запущен" if db_ok else "⚠️ Бот запущен, но есть проблемы с БД (см. «Диагностика БД»)"
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.set_my_commands(
                USER_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(admin_id)
            )
            await application.bot.send_message(
                admin_id, f"{status}\n👥 Пользователей в БД: {total}\n\nАдмин-панель: /admin"
            )
        except Exception as error:
            log.warning("Не удалось уведомить админа %s: %s", admin_id, error)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Необработанная ошибка", exc_info=context.error)


def _main_menu(ai_mode: bool) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [button("🏰 Квиз: факультет", callback_data="menu:house"),
         button("🪄 Квиз: персонаж", callback_data="menu:character")],
        [button(f"💬 Режим ИИ: {'вкл ✅' if ai_mode else 'выкл'}", callback_data="menu:aimode")],
        [button("🧠 Моя память", callback_data="menu:profile"),
         button("🏆 Мои результаты", callback_data="menu:results")],
        [button("⏰ Время", callback_data="menu:time"),
         button("🎲 Случайное", callback_data="menu:random")],
    ])


# ==================== ОСНОВНЫЕ КОМАНДЫ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Ссылка вида t.me/бот?start=instagram → запоминаем источник (только первый раз)
    if context.args:
        await run_sync(set_start_source, user.id, context.args[0])
    name = user.first_name or "друг"
    await update.message.reply_text(
        f"👋 Привет, {name}!\n\nЯ многофункциональный бот: квизы по Гарри Поттеру, "
        "ИИ-помощник и мелкие утилиты.\nВсё доступно кнопками — жми /menu или смотри /help.\n\n"
        "ℹ️ Бот сохраняет данные вашего профиля Telegram и статистику использования. "
        "Подробности и удаление данных: /privacy",
        reply_markup=_main_menu(bool(context.user_data.get("ai_mode"))),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 Справка по командам:\n\n"
        "/start — приветствие\n"
        "/menu — главное меню с кнопками\n"
        "/quiz — квиз: какой ты факультет\n"
        "/hogwarts — квиз: на какого персонажа ты похож\n"
        "/ai вопрос — спросить ИИ-помощника\n"
        "/profile — что бот помнит о вас\n"
        "/myresults — ваши результаты квизов\n"
        "/forget — удалить вашу память и историю чата с ИИ\n"
        "/privacy — какие данные хранит бот и их удаление\n"
        "/time — текущее время\n"
        "/random — случайное число и цвет\n"
        "/cancel — отменить текущее действие"
    )
    if is_admin(update.effective_user.id):
        text += "\n\n🛠 Для администратора:\n/admin — админ-панель\n/users — пользователи\n/broadcast — рассылка"
    await update.message.reply_text(text)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Главное меню", reply_markup=_main_menu(bool(context.user_data.get("ai_mode")))
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "house":
        await _start_quiz(update, context, "house")
    elif action == "character":
        await _start_quiz(update, context, "character")
    elif action == "profile":
        await profile_command(update, context)
    elif action == "results":
        await myresults_command(update, context)
    elif action == "time":
        await handle_time_command(update, context)
    elif action == "random":
        await ran_dom(update, context)
    elif action == "aimode":
        enabled = not context.user_data.get("ai_mode")
        context.user_data["ai_mode"] = enabled
        await safe_edit(query, "📋 Главное меню", _main_menu(enabled))
        await query.message.reply_text(
            "💬 Режим ИИ включён: просто пишите сообщения, и я отвечу как ИИ." if enabled
            else "💬 Режим ИИ выключен."
        )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_admin_text(update, context):  # ожидаем текст рассылки от админа
        return
    text = update.message.text
    if context.user_data.get("ai_mode"):
        await ai_reply(update, context, text)
    elif get_setting_bool("echo_enabled"):
        await update.message.reply_text(f"Ты написал: {text}")
    else:
        await update.message.reply_text("Не понял 🤔 Попробуйте /menu или /help.")


async def handle_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    await update.effective_message.reply_text(
        f"⏰ Текущее время: {now:%H:%M:%S}\nДата: {now:%d/%m/%Y}"
    )


async def ran_dom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Итак, твоё число 🎲: {random.randint(1, 100)}\nТвой цвет 🎨: {random.choice(COLORS)}"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет квиз и ожидание текста рассылки."""
    context.user_data.pop("quiz", None)
    context.user_data.pop("admin_state", None)
    context.user_data.pop("broadcast_draft", None)
    await update.effective_message.reply_text("❌ Отменено.")


# ==================== КВИЗЫ ====================

LETTERS = ["А", "Б", "В", "Г", "Д", "Е"]


def _question_text(state: dict, title: str | None = None) -> str:
    """Текст вопроса: варианты ответов выписаны целиком, чтобы Telegram их не обрезал."""
    question, answers = state["questions"][state["index"]]
    lines = [title, ""] if title else []
    lines += [f"Вопрос {state['index'] + 1} из {len(state['questions'])}", "", question, ""]
    lines += [f"{LETTERS[i]}) {text}" for i, (text, _) in enumerate(answers)]
    lines += ["", "Выберите вариант кнопкой ниже 👇"]
    return "\n".join(lines)


def _quiz_keyboard(state: dict, answers) -> InlineKeyboardMarkup:
    # На кнопках только буквы (одной строкой), полный текст — в сообщении.
    # callback_data: режим:id_квиза:номер_вопроса:результат (до 64 байт)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            LETTERS[i], callback_data=f"{state['mode']}:{state['id']}:{state['index']}:{result}"
        )
        for i, (_, result) in enumerate(answers)
    ]])


async def _start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    message = update.effective_message
    if not get_setting_bool("quiz_enabled"):
        await message.reply_text("🧙 Квизы сейчас отключены администратором.")
        return

    if mode == "house":
        title, source, keys = "🧙 Квиз факультетов Хогвартса", HOUSE_QUESTIONS, HOUSES
    else:
        title, source, keys = "🪄 Квиз персонажей Хогвартса", CHARACTER_QUESTIONS, HOGWARTS_CHARACTERS

    # Перемешиваем и вопросы, и порядок вариантов внутри каждого вопроса
    questions = [(question, random.sample(answers, len(answers))) for question, answers in source]
    random.shuffle(questions)
    state = {
        "mode": mode,
        "id": secrets.token_hex(2),
        "index": 0,
        "questions": questions,
        "scores": {key: 0 for key in keys},
    }
    context.user_data["quiz"] = state

    await message.reply_text(
        _question_text(state, title), reply_markup=_quiz_keyboard(state, questions[0][1])
    )

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start_quiz(update, context, "house")


async def start_hogwarts_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _start_quiz(update, context, "character")


async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие на вариант ответа."""
    query = update.callback_query
    state = context.user_data.get("quiz")
    if not state:
        await query.answer("Квиз завершён или не найден. Запустите заново: /quiz", show_alert=True)
        return

    try:
        mode, quiz_id, index_text, result = query.data.split(":", 3)
        index = int(index_text)
    except ValueError:
        await query.answer("Некорректный ответ.", show_alert=True)
        return

    if mode != state["mode"] or quiz_id != state["id"] or index != state["index"]:
        await query.answer("Этот вопрос уже устарел.", show_alert=True)
        return
    if result not in state["scores"]:
        await query.answer("Неизвестный вариант ответа.", show_alert=True)
        return

    await query.answer()
    state["scores"][result] += 1
    state["index"] += 1

    if state["index"] < len(state["questions"]):
        _, answers = state["questions"][state["index"]]
        await safe_edit(query, _question_text(state), _quiz_keyboard(state, answers))
        return

    # Квиз закончен: при равенстве очков выбираем случайного лидера
    scores = state["scores"]
    top = max(scores.values())
    winner = random.choice([key for key, value in scores.items() if value == top])
    user_id = query.from_user.id

    if state["mode"] == "house":
        info = HOUSES[winner]
        await run_sync(save_quiz_result, user_id, winner, scores)
        result_text = f"🎉 Квиз завершён!\n\nВаш факультет: {info['name']}\n{info['description']}"
        caption = f"Ваш факультет: {info['name']}"
    else:
        info = HOGWARTS_CHARACTERS[winner]
        await run_sync(save_character_quiz_result, user_id, winner, scores)
        result_text = f"🎉 Квиз завершён!\n\nВы похожи на: {info['emoji']} {info['name']}\n{info['description']}"
        caption = f"Ваш персонаж: {info['emoji']} {info['name']}"

    context.user_data.pop("quiz", None)
    await safe_edit(query, result_text)
    try:
        await context.bot.send_photo(chat_id=query.message.chat_id, photo=info["image"], caption=caption)
    except Exception as error:
        log.warning("Не удалось отправить картинку: %s", error)


async def myresults_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    houses = await run_sync(get_user_quiz_results, user_id)
    characters = await run_sync(get_user_character_quiz_results, user_id)
    if not houses and not characters:
        await update.effective_message.reply_text("Вы ещё не проходили квизы. Попробуйте /quiz или /hogwarts")
        return

    lines = ["🏆 Ваши результаты:"]
    if houses:
        lines.append("\nФакультеты:")
        lines += [f"• {HOUSES.get(h, {}).get('name', h)} — {d:%d.%m.%Y %H:%M}" for h, d in houses]
    if characters:
        lines.append("\nПерсонажи:")
        lines += [f"• {HOGWARTS_CHARACTERS.get(c, {}).get('name', c)} — {d:%d.%m.%Y %H:%M}"
                  for c, d in characters]
    await update.effective_message.reply_text("\n".join(lines))


# ==================== ИИ-ЧАТ И ПАМЯТЬ ====================

def _build_system_prompt(memory_data: dict) -> str:
    style = get_setting("ai_style")
    prompt = AI_STYLES.get(style, AI_STYLES["friendly"])[1]
    if memory_data.get("memory"):
        prompt += f"\nИнформация о пользователе: {memory_data['memory']}"
    if memory_data.get("preferences"):
        prefs = ", ".join(f"{k}: {v}" for k, v in memory_data["preferences"].items())
        prompt += f"\nПредпочтения: {prefs}"
    return prompt


async def ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    message = update.effective_message
    user_id = update.effective_user.id

    if not get_setting_bool("ai_enabled"):
        await message.reply_text("🤖 ИИ-помощник сейчас отключён администратором.")
        return
    if time.monotonic() - context.user_data.get("ai_last", 0.0) < AI_COOLDOWN_SEC:
        await message.reply_text("⏳ Не так быстро, подождите пару секунд.")
        return
    context.user_data["ai_last"] = time.monotonic()

    try:
        limit = int(get_setting("history_limit"))
    except ValueError:
        limit = 6

    # История берётся ДО сохранения текущего вопроса, чтобы он не дублировался
    history = await run_sync(get_recent_messages, user_id, limit)
    memory_data = await run_sync(get_user_memory, user_id)
    system_prompt = _build_system_prompt(memory_data)

    await message.chat.send_action(ChatAction.TYPING)
    response = await run_sync(get_giga_response, query_text, system_prompt, history, get_setting("giga_model"))

    if not response or not response.strip():
        await message.reply_text("⚠️ ИИ сейчас недоступен. Попробуйте позже.")
        return

    await run_sync(save_message, user_id, "user", query_text)
    await run_sync(save_message, user_id, "assistant", response)
    for part in split_text(response):
        await message.reply_text(part)
    await run_sync(extract_and_update_memory, user_id, query_text)


async def giga_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = " ".join(context.args or []).strip()
    if not query_text:
        await update.message.reply_text("Напишите запрос после команды, например: /ai Расскажи анекдот")
        return
    await ai_reply(update, context, query_text)


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await run_sync(get_user_memory, update.effective_user.id)
    prefs, memory_text = data.get("preferences", {}), data.get("memory", "")
    if not prefs and not memory_text:
        await update.effective_message.reply_text("Я пока ничего не знаю о вас.")
        return

    lines = ["🧠 Моя память о вас:"]
    if prefs:
        lines.append("\nПредпочтения:")
        lines += [f"• {key}: {value}" for key, value in prefs.items()]
    if memory_text:
        lines.append(f"\nРезюме: {memory_text}")
    await update.effective_message.reply_text("\n".join(lines))


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет память и историю ИИ-чата самого пользователя."""
    ok = await run_sync(clear_user_memory, update.effective_user.id)
    await update.effective_message.reply_text(
        "🧹 Я удалил всю информацию о вас из своей памяти." if ok else "⚠️ Не удалось очистить память."
    )

# ==================== ПРИВАТНОСТЬ ====================

PRIVACY_TEXT = (
    "🔒 Какие данные хранит бот\n\n"
    "Из вашего профиля Telegram:\n"
    "• ID, username, имя и фамилия\n\n"
    "Номер телефона, геолокация и контакты **не собираются**.\n"
)


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        PRIVACY_TEXT,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить мои данные", callback_data="priv:ask")]]),
    )


async def privacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    if action == "ask":
        await query.answer()
        await safe_edit(
            query,
            "⚠️ Удалить ваши персональные данные: профиль, историю ИИ и память?\n"
            "Результаты квизов останутся только как обезличенная статистика.\n"
            "Это действие необратимо.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, удалить", callback_data="priv:yes"),
                InlineKeyboardButton("❌ Отмена", callback_data="priv:no"),
            ]]),
        )
    elif action == "yes":
        ok = await run_sync(anonymize_user, query.from_user.id)
        context.user_data.clear()
        await query.answer()
        await safe_edit(
            query,
            "🗑 Ваши персональные данные удалены. Остались только обезличенные счётчики без привязки к вам."
            if ok else "⚠️ Не удалось удалить данные. Попробуйте позже.",
        )
    else:
        await query.answer()
        await safe_edit(query, "Отменено. Ваши данные не тронуты.")