from telegram import Update
from telegram.ext import Application

from ocean_helper_bot.config import Settings
from ocean_helper_bot.features import register_features, start_features, stop_features


def create_application(settings: Settings) -> Application:
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(start_features)
        .post_stop(stop_features)
    )
    if settings.proxy_url:
        builder = builder.proxy(settings.proxy_url).get_updates_proxy(settings.proxy_url)

    application = builder.build()
    application.bot_data["proxy_url"] = settings.proxy_url
    application.bot_data["settings"] = settings
    register_features(application)
    return application


def run(settings: Settings) -> None:
    application = create_application(settings)
    application.run_polling(allowed_updates=Update.ALL_TYPES)
