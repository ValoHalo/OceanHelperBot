import html
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx


_DESKTOP_HOSTS = {
    "item.m.jd.com": "item.jd.com",
    "m.jd.com": "www.jd.com",
    "detail.m.tmall.com": "detail.tmall.com",
    "m.taobao.com": "www.taobao.com",
    "m.bilibili.com": "www.bilibili.com",
}

_SOCIAL_PREVIEW_HOSTS = {"phixiv.net", "fixupx.com", "fxtwitter.com"}


class LinkProcessor(Protocol):
    name: str

    def accepts(self, url: str) -> bool: ...

    async def clean(self, url: str, client: httpx.AsyncClient) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ResolvedPage:
    urls: tuple[str, ...]
    body: str


async def _resolve(url: str, client: httpx.AsyncClient) -> ResolvedPage:
    response = await client.get(url)
    visited = tuple(str(item.url) for item in (*response.history, response))
    content_type = response.headers.get("content-type", "").lower()
    body = response.text[:750_000] if "text" in content_type or "html" in content_type else ""
    return ResolvedPage(urls=visited, body=html.unescape(body))


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_social_preview_url(url: str) -> bool:
    hostname = _host(url)
    return any(
        hostname == host or hostname.endswith(f".{host}")
        for host in _SOCIAL_PREVIEW_HOSTS
    )


def to_desktop_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        source_host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return url
    desktop_host = _DESKTOP_HOSTS.get(source_host)
    if desktop_host is None:
        return url
    return urlunsplit((parsed.scheme, desktop_host, parsed.path, parsed.query, parsed.fragment))


def _decoded(value: str, rounds: int = 2) -> str:
    for _ in range(rounds):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


class JdProcessor:
    name = "jd"
    _hosts = {"3.cn", "item.jd.com", "item.m.jd.com", "mitem.jd.hk"}
    _path_pattern = re.compile(r"/(?:product/)?(\d{8,})\.html", re.IGNORECASE)
    _body_pattern = re.compile(
        r"(?:item\.m?\.jd\.com/(?:product/)?|item\.jd\.hk/product/)(\d{8,})\.html",
        re.IGNORECASE,
    )

    def accepts(self, url: str) -> bool:
        return _host(url) in self._hosts

    async def clean(self, url: str, client: httpx.AsyncClient) -> str | None:
        candidates = (url,)
        body = ""
        if _host(url) == "3.cn":
            page = await _resolve(url, client)
            candidates = page.urls
            body = page.body

        for candidate in reversed(candidates):
            match = self._path_pattern.search(urlsplit(candidate).path)
            if match:
                return f"https://item.jd.com/{match.group(1)}.html"

        match = self._body_pattern.search(_decoded(body))
        if match:
            return f"https://item.jd.com/{match.group(1)}.html"
        return None


class TaobaoProcessor:
    name = "taobao"
    _short_hosts = {"e.tb.cn", "m.tb.cn"}
    _item_hosts = {
        "detail.tmall.com",
        "detail.m.tmall.com",
        "item.taobao.com",
        "h5.m.taobao.com",
        "main.m.taobao.com",
    }
    _body_url_pattern = re.compile(
        r"(?:https?:)?//(?:detail(?:\.m)?\.tmall\.com|item\.taobao\.com)"
        r"/[^\s\"'<>\\]*?[?&]id=\d{8,}[^\s\"'<>\\]*",
        re.IGNORECASE,
    )

    def accepts(self, url: str) -> bool:
        return _host(url) in self._short_hosts | self._item_hosts

    async def clean(self, url: str, client: httpx.AsyncClient) -> str | None:
        candidates = (url,)
        body = ""
        if _host(url) in self._short_hosts:
            page = await _resolve(url, client)
            candidates = page.urls
            body = _decoded(page.body.replace("\\/", "/"))

        for candidate in reversed(candidates):
            result = self._canonical_from_url(candidate)
            if result:
                return result

        match = self._body_url_pattern.search(body)
        if match:
            return self._canonical_from_url(match.group(0))
        return None

    @staticmethod
    def _canonical_from_url(url: str) -> str | None:
        parsed = urlsplit(_decoded(url))
        hostname = (parsed.hostname or "").lower()
        if hostname not in TaobaoProcessor._item_hosts:
            return None
        query = parse_qsl(parsed.query, keep_blank_values=True)
        item_id = dict(query).get("id")
        if not item_id or not item_id.isdigit():
            return None
        domain = "detail.tmall.com" if "tmall" in hostname else "item.taobao.com"
        kept_query = [("id", item_id)]
        kept_query.extend((name, value) for name, value in query if name.lower() == "skuid")
        return f"https://{domain}/item.htm?{urlencode(kept_query)}"


class BilibiliProcessor:
    name = "bilibili"
    _hosts = {"b23.tv", "bilibili.com", "www.bilibili.com", "m.bilibili.com"}
    _bvid_pattern = re.compile(r"/video/(BV[0-9A-Za-z]{10})(?:[/?#]|$)", re.IGNORECASE)
    _body_video_pattern = re.compile(
        r"/video/BV[0-9A-Za-z]{10}(?=[/?#\s\"'<>]|$)[^\s\"'<>\\]*",
        re.IGNORECASE,
    )

    def accepts(self, url: str) -> bool:
        return _host(url) in self._hosts

    async def clean(self, url: str, client: httpx.AsyncClient) -> str | None:
        candidates = (url,)
        body = ""
        if _host(url) == "b23.tv":
            page = await _resolve(url, client)
            candidates = page.urls
            body = page.body

        for candidate in reversed(candidates):
            canonical_url = self._canonical_from_url(candidate, url)
            if canonical_url:
                return await TRACKING_PARAMETER_PROCESSOR.clean(canonical_url, client)

        body = body.replace("\\/", "/")
        match = self._body_video_pattern.search(body) or self._body_video_pattern.search(
            _decoded(body, rounds=1)
        )
        if match:
            canonical_url = self._canonical_from_url(
                f"https://www.bilibili.com{match.group(0)}", url
            )
            if canonical_url:
                return await TRACKING_PARAMETER_PROCESSOR.clean(canonical_url, client)
        return None

    @staticmethod
    def _canonical_from_url(url: str, source_url: str) -> str | None:
        parsed = urlsplit(url)
        match = BilibiliProcessor._bvid_pattern.search(parsed.path)
        if not match:
            return None

        query = parsed.query
        if source_url != url:
            playback_query = [
                (name, value)
                for name, value in parse_qsl(urlsplit(source_url).query, keep_blank_values=True)
                if name in {"t", "p"}
            ]
            if playback_query:
                playback_names = {name for name, _ in playback_query}
                query = urlencode(
                    [
                        (name, value)
                        for name, value in parse_qsl(query, keep_blank_values=True)
                        if name not in playback_names
                    ]
                    + playback_query
                )

        return urlunsplit(
            (
                "https",
                "www.bilibili.com",
                f"/video/{match.group(1)}/",
                query,
                parsed.fragment,
            )
        )


class TrackingParameterProcessor:
    name = "generic"
    _tracking_names = {
        "_ga",
        "_gl",
        "campaign",
        "campaignid",
        "dclid",
        "fbclid",
        "from",
        "from_source",
        "from_spmid",
        "gbraid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "oly_anon_id",
        "oly_enc_id",
        "rb_clickid",
        "ref",
        "referrer",
        "s_cid",
        "scm",
        "si",
        "soc_src",
        "soc_trk",
        "source",
        "spm",
        "spm_id_from",
        "srsltid",
        "ttclid",
        "twclid",
        "trk",
        "vd_source",
        "vero_conv",
        "vero_id",
        "wbraid",
        "wickedid",
        "yclid",
    }
    _tracking_prefixes = ("_hs", "utm_", "share_")

    def accepts(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    async def clean(self, url: str, client: httpx.AsyncClient) -> str | None:
        del client
        parsed = urlsplit(url)
        original_query = parse_qsl(parsed.query, keep_blank_values=True)
        clean_query = [
            (name, value)
            for name, value in original_query
            if name.lower() not in self._tracking_names
            and not name.lower().startswith(self._tracking_prefixes)
        ]
        if clean_query == original_query:
            return url
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(clean_query, doseq=True),
                parsed.fragment,
            )
        )


class SocialPreviewProcessor:
    name = "social-preview"
    _preview_hosts = {
        "pixiv.net": "phixiv.net",
        "www.pixiv.net": "www.phixiv.net",
        "x.com": "fixupx.com",
        "www.x.com": "fixupx.com",
        "m.x.com": "fixupx.com",
        "mobile.x.com": "fixupx.com",
        "twitter.com": "fxtwitter.com",
        "www.twitter.com": "fxtwitter.com",
        "m.twitter.com": "fxtwitter.com",
        "mobile.twitter.com": "fxtwitter.com",
    }
    _twitter_preview_hosts = {"fixupx.com", "fxtwitter.com"}
    _twitter_tracking_names = {"s", "t"}

    def accepts(self, url: str) -> bool:
        return _host(url) in self._preview_hosts and urlsplit(url).scheme in {"http", "https"}

    async def clean(self, url: str, client: httpx.AsyncClient) -> str | None:
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            return None

        preview_host = self._preview_hosts[_host(url)]
        netloc = preview_host
        if parsed.port is not None:
            netloc += f":{parsed.port}"

        query = parsed.query
        if preview_host in self._twitter_preview_hosts:
            original_query = parse_qsl(query, keep_blank_values=True)
            clean_query = [
                (name, value)
                for name, value in original_query
                if name.lower() not in self._twitter_tracking_names
            ]
            if clean_query != original_query:
                query = urlencode(clean_query, doseq=True)

        preview_url = urlunsplit(
            (parsed.scheme, netloc, parsed.path, query, parsed.fragment)
        )
        return await TRACKING_PARAMETER_PROCESSOR.clean(preview_url, client)


TRACKING_PARAMETER_PROCESSOR = TrackingParameterProcessor()

PROCESSORS: tuple[LinkProcessor, ...] = (
    JdProcessor(),
    TaobaoProcessor(),
    BilibiliProcessor(),
    SocialPreviewProcessor(),
    TRACKING_PARAMETER_PROCESSOR,
)
