import logging

import httpx

from ocean_helper_bot.features.link_cleaner.processors import (
    PROCESSORS,
    TRACKING_PARAMETER_PROCESSOR,
    is_social_preview_url,
    to_desktop_url,
)

LOGGER = logging.getLogger(__name__)


async def clean_links(
    source_urls: list[str],
    proxy_url: str | None = None,
) -> list[str]:
    pending_urls = [url for url in source_urls if not is_social_preview_url(url)]
    if not pending_urls:
        return []

    cleaned_urls: list[str] = []
    timeout = httpx.Timeout(15.0, connect=10.0)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    }

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=10,
        timeout=timeout,
        headers=headers,
        proxy=proxy_url,
    ) as client:
        for source_url in pending_urls:
            candidate_url = to_desktop_url(source_url)
            processor = next(
                (candidate for candidate in PROCESSORS if candidate.accepts(candidate_url)),
                None,
            )
            if processor is None:
                continue

            try:
                cleaned_url = await processor.clean(candidate_url, client)
                if cleaned_url is None and candidate_url != source_url:
                    cleaned_url = await TRACKING_PARAMETER_PROCESSOR.clean(candidate_url, client)
            except (httpx.HTTPError, ValueError):
                LOGGER.warning("Could not clean URL from %s", processor.name, exc_info=True)
                continue

            if cleaned_url and cleaned_url != source_url and cleaned_url not in cleaned_urls:
                cleaned_urls.append(cleaned_url)

    return cleaned_urls
