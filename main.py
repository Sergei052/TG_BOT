"""Точка входа: собирает приложение и регистрирует обработчики."""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from admin import admin_callback, admin_command, broadcast_command, users_command
from config import TOKEN
from corner import (
    cancel_command,
    echo,
    error_handler,
    forget_command,
    giga_chat,
    guard,
    handle_quiz_answer,
    handle_time_command,
    help_command,
    menu_callback,
    menu_command,
    myresults_command,
    post_init,
    privacy_callback,
    privacy_command,
    profile_command,
    ran_dom,
    start,
    start_hogwarts_quiz,
    start_quiz,
)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
    )
    # httpx на уровне INFO печатает URL запросов вместе с токеном бота — глушим
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not TOKEN:
        raise RuntimeError("Токен бота не задан: добавьте BOT_TOKEN в .env")

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # group=-1 выполняется первым: бан-проверка и регистрация пользователя
    app.add_handler(TypeHandler(Update, guard), group=-1)

    commands = {
        "start": start,
        "help": help_command,
        "menu": menu_command,
        "time": handle_time_command,
        "random": ran_dom,
        "quiz": start_quiz,
        "hogwarts": start_hogwarts_quiz,
        "ai": giga_chat,
        "profile": profile_command,
        "myresults": myresults_command,
        "privacy": privacy_command,
        "forget": forget_command,
        "cancel": cancel_command,
        "admin": admin_command,
        "users": users_command,
        "broadcast": broadcast_command,
    }
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback))

    app.add_handler(CallbackQueryHandler(handle_quiz_answer, pattern=r"^(house|character):"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(privacy_callback, pattern=r"^priv:"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_error_handler(error_handler)

    logging.getLogger(__name__).info("Бот запускается…")
    app.run_polling()


if __name__ == "__main__":
    main()