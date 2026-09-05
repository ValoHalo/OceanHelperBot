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
        r"(?:https?:)?//(?P<host>detail(?:\.m)?\.tmall\.com|item\.taobao\.com)"
        r"/[^\s\"'<>]*?[?&]id=(?P<id>\d{8,})",
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
            domain = "detail.tmall.com" if "tmall" in match.group("host").lower() else "item.taobao.com"
            return f"https://{domain}/item.htm?id={match.group('id')}"
        return None

    @staticmethod
    def _canonical_from_url(url: str) -> str | None:
        parsed = urlsplit(_decoded(url))
        hostname = (parsed.hostname or "").lower()
        if hostname not in TaobaoProcessor._item_hosts:
            return None
        item_id = dict(parse_qsl(parsed.query)).get("id")
        if not item_id or not item_id.isdigit():
            return None
        domain = "detail.tmall.com" if "tmall" in hostname else "item.taobao.com"
        return f"https://{domain}/item.htm?id={item_id}"


class BilibiliProcessor:
    name = "bilibili"
    _hosts = {"b23.tv", "bilibili.com", "www.bilibili.com", "m.bilibili.com"}
    _bvid_pattern = re.compile(r"/video/(BV[0-9A-Za-z]{10})(?:[/?#]|$)", re.IGNORECASE)

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
            match = self._bvid_pattern.search(urlsplit(candidate).path)
            if match:
                return f"https://www.bilibili.com/video/{match.group(1)}"

        match = self._bvid_pattern.search(_decoded(body))
        if match:
            return f"https://www.bilibili.com/video/{match.group(1)}"
        return None


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


TRACKING_PARAMETER_PROCESSOR = TrackingParameterProcessor()

PROCESSORS: tuple[LinkProcessor, ...] = (
    JdProcessor(),
    TaobaoProcessor(),
    BilibiliProcessor(),
    TRACKING_PARAMETER_PROCESSOR,
)
