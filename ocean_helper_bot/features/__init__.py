from telegram.ext import Application

from ocean_helper_bot.features.github_watcher import register as register_github_watcher
from ocean_helper_bot.features.github_watcher import start as start_github_watcher
from ocean_helper_bot.features.github_watcher import stop as stop_github_watcher
from ocean_helper_bot.features.health import register as register_health
from ocean_helper_bot.features.link_cleaner import register as register_link_cleaner


def register_features(application: Application) -> None:
    register_health(application)
    register_link_cleaner(application)
    register_github_watcher(application)


async def start_features(application: Application) -> None:
    await start_github_watcher(application)


async def stop_features(application: Application) -> None:
    await stop_github_watcher(application)
