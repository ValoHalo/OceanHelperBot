from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from ocean_helper_bot.config import Settings
from ocean_helper_bot.features.github_watcher.handler import (
    cancel,
    delete_integration,
    follow_repository,
    handle_menu_callback,
    list_repositories,
    new_integration,
    start as start_command,
    unfollow_repository,
)
from ocean_helper_bot.features.github_watcher.service import GitHubWatcher

WATCHER_KEY = "github_watcher"


def register(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]
    application.bot_data[WATCHER_KEY] = GitHubWatcher(
        bot=application.bot,
        state_path=settings.github_state_path,
        token=settings.github_token,
        proxy_url=settings.proxy_url,
        interval_seconds=settings.github_watch_interval_seconds,
    )
    application.add_handler(CommandHandler("github_follow", follow_repository))
    application.add_handler(CommandHandler("github_unfollow", unfollow_repository))
    application.add_handler(CommandHandler("github_follows", list_repositories))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("newintegration", new_integration))
    application.add_handler(CommandHandler("listintegrations", list_repositories))
    application.add_handler(CommandHandler("delintegration", delete_integration))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(
        CallbackQueryHandler(handle_menu_callback, pattern=r"^github_menu:")
    )


async def start(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "显示帮助"),
            BotCommand("health", "检查机器人是否在线"),
            BotCommand("newintegration", "选择并关注仓库"),
            BotCommand("listintegrations", "查看当前会话的订阅"),
            BotCommand("delintegration", "选择并取消订阅"),
            BotCommand("cancel", "取消当前操作"),
        ]
    )
    watcher: GitHubWatcher = application.bot_data[WATCHER_KEY]
    watcher.start()


async def stop(application: Application) -> None:
    watcher: GitHubWatcher = application.bot_data[WATCHER_KEY]
    await watcher.stop()
