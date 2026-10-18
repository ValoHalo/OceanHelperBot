from functools import partial

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from ocean_helper_bot.features.link_cleaner.handler import handle_message
from ocean_helper_bot.features.link_cleaner.management import STORE_KEY, handle_callback, start_command
from ocean_helper_bot.features.link_cleaner.rules import RuleStore


def register(application: Application) -> None:
    settings = application.bot_data["settings"]
    if settings.link_cleaner_state_path.resolve() == settings.github_state_path.resolve():
        raise ValueError("LINK_CLEANER_STATE_PATH must differ from GITHUB_STATE_PATH")
    application.bot_data[STORE_KEY] = RuleStore(settings.link_cleaner_state_path)
    for command in ("addlink", "replacelink", "whitelist", "links"):
        application.add_handler(CommandHandler(command, partial(start_command, mode=command)))
    application.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^lr:"))
    application.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message)
    )
