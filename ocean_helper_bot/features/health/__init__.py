from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    message = update.effective_message
    if message is not None:
        await message.reply_text("运行正常。")


def register(application: Application) -> None:
    application.add_handler(CommandHandler("health", health))
