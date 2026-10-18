import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ocean_helper_bot.features.link_cleaner.processors import (
    BilibiliProcessor, JdProcessor, TaobaoProcessor,
    TRACKING_PARAMETER_PROCESSOR, is_social_preview_url,
)
from ocean_helper_bot.features.link_cleaner.rules import (
    DEFAULT_RULES, LinkRule, RuleStore, apply_rule,
)

LOGGER = logging.getLogger(__name__)
RESOLVERS = {"jd": JdProcessor(), "taobao": TaobaoProcessor(), "bilibili": BilibiliProcessor()}


async def transform_url(url: str, rule: LinkRule, client: httpx.AsyncClient) -> str:
    if not rule.enabled:
        return url
    resolver = RESOLVERS.get(rule.processor)
    candidate = url
    if resolver is not None:
        candidate = await resolver.clean(url, client, preserve_parameters=True)
        if candidate is None:
            if urlsplit(url).hostname in {"3.cn", "e.tb.cn", "m.tb.cn", "b23.tv"}:
                raise ValueError("短链解析失败，请更换链接后重试。")
            candidate = url
        if candidate != url:
            original = [(name, value) for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True) if rule.allows(name)]
            if original:
                parsed = urlsplit(candidate)
                groups = {}
                for name, value in original:
                    groups.setdefault(name, []).append((name, value))
                previous = parse_qsl(parsed.query, keep_blank_values=True)
                # Explicit parameters on the shared URL override redirect defaults.
                # Keep the canonical ordering and preserve repeated values.
                merged, used = [], set()
                for name, value in previous:
                    if name not in groups:
                        merged.append((name, value))
                    elif name not in used:
                        merged.extend(groups[name])
                        used.add(name)
                merged.extend((name, value) for name, value in original if name not in used)
                if merged != previous:
                    candidate = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(merged), parsed.fragment))
    return apply_rule(candidate, rule)


def link_client(proxy_url: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True, max_redirects=10,
        timeout=httpx.Timeout(15.0, connect=10.0), proxy=proxy_url,
        headers={"User-Agent": "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36"},
    )


async def preview_rule(rule: LinkRule, proxy_url: str | None = None) -> str:
    async with link_client(proxy_url) as client:
        return await transform_url(rule.sample_url, rule, client)


async def clean_links(
    source_urls: list[str], proxy_url: str | None = None,
    store: RuleStore | None = None,
) -> list[str]:
    rules = store.list_rules() if store is not None else DEFAULT_RULES
    by_host = {host: rule for rule in rules for host in rule.hosts}
    targets = {rule.replacement for rule in rules if rule.enabled and rule.replacement}
    pending = []
    for url in source_urls:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            if parsed.scheme not in {"http", "https"} or not host:
                continue
            if parsed.username is not None or parsed.password is not None:
                continue
            if host not in by_host and (is_social_preview_url(url) or host in targets):
                continue
            pending.append((url, by_host.get(host)))
        except ValueError:
            continue
    if not pending:
        return []
    cleaned = []
    async with link_client(proxy_url) as client:
        for url, rule in pending:
            try:
                result = await transform_url(url, rule, client) if rule else await TRACKING_PARAMETER_PROCESSOR.clean(url, client)
            except (httpx.HTTPError, ValueError):
                LOGGER.warning("Could not clean a link with processor %s", rule.processor if rule else "generic")
                continue
            if result and result != url and result not in cleaned:
                cleaned.append(result)
    return cleaned
