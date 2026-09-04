from telegram.ext import Application, MessageHandler, filters

from ocean_helper_bot.features.link_cleaner.handler import handle_message


def register(application: Application) -> None:
    application.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message)
    )
