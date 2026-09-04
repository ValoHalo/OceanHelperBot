import html
import logging
import re
from collections.abc import Iterable

from telegram import LinkPreviewOptions, Message, Update
from telegram.constants import MessageEntityType, ParseMode
from telegram.ext import ContextTypes

from ocean_helper_bot.features.link_cleaner.service import clean_links

LOGGER = logging.getLogger(__name__)
URL_PATTERN = re.compile(r"https?://[^\s<>\[\]`]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:，。！？；：、\"'“”‘’)}"


def _message_urls(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    entities = message.entities if message.text else message.caption_entities
    parse_entity = message.parse_entity if message.text else message.parse_caption_entity
    urls: list[str] = []

    for entity in entities or ():
        if entity.type == MessageEntityType.TEXT_LINK and entity.url:
            urls.append(entity.url)
        elif entity.type == MessageEntityType.URL:
            urls.append(parse_entity(entity))

    urls.extend(match.group(0).rstrip(TRAILING_PUNCTUATION) for match in URL_PATTERN.finditer(text))
    return list(_deduplicate(urls))


def _deduplicate(values: Iterable[str]) -> Iterable[str]:
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            yield value


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or (message.from_user and message.from_user.is_bot):
        return

    source_urls = _message_urls(message)
    if not source_urls:
        return

    try:
        cleaned_urls = await clean_links(
            source_urls,
            proxy_url=context.application.bot_data.get("proxy_url"),
        )
    except Exception:
        LOGGER.exception("Unexpected failure while cleaning message links")
        return

    if not cleaned_urls:
        return

    links = "\n".join(
        f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'
        for url in cleaned_urls
    )
    await message.reply_text(
        links,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        do_quote=True,
        message_thread_id=message.message_thread_id,
    )
