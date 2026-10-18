import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class RuleConflict(ValueError):
    pass


def domain(value: str) -> str:
    parsed = urlsplit(value if "://" in value else "https://" + value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None or parsed.password is not None
        or parsed.port is not None or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment
    ):
        raise ValueError("请输入域名，不包含路径、端口或参数。")
    host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    if len(host) > 253 or "." not in host or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
        for part in host.split(".")
    ):
        raise ValueError("请输入有效域名。")
    return host


def validate_url(value: str) -> str:
    if len(value) > 1800:
        raise ValueError("示例链接不能超过 1800 个字符。")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("请输入完整的 HTTP 或 HTTPS 链接。")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("链接不能包含用户名或密码。")
    host = domain(parsed.hostname)
    netloc = host + (f":{parsed.port}" if parsed.port is not None else "")
    if any(char.isspace() for char in value):
        raise ValueError("链接不能包含空白字符。")
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def parameter_names(values) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(values))
    if len(names) > 60 or sum(len(name) for name in names) > 600:
        raise ValueError("白名单最多包含 60 个参数，总长度不能超过 600 个字符。")
    if any(not name or len(name) > 64 or re.search(r"[\s?&#=\x00-\x1f]", name) for name in names):
        raise ValueError("只填写参数名，用空格或换行分隔，不包含问号、等号或参数值。")
    return names


@dataclass(frozen=True)
class LinkRule:
    host: str
    aliases: tuple[str, ...] = ()
    replacement: str | None = None
    allowed_params: tuple[str, ...] = ()
    processor: str = "generic"
    enabled: bool = True
    sample_url: str = ""
    revision: int = 0

    @property
    def hosts(self) -> tuple[str, ...]:
        return (self.host, *self.aliases)

    def allows(self, name: str) -> bool:
        if self.processor == "taobao" and name.lower() == "skuid":
            return any(item.lower() == "skuid" for item in self.allowed_params)
        return name in self.allowed_params


def encode(rule: LinkRule) -> str:
    return json.dumps(asdict(rule), ensure_ascii=False)


def decode(data: str) -> LinkRule:
    values = json.loads(data)
    values["aliases"] = tuple(values["aliases"])
    values["allowed_params"] = tuple(values["allowed_params"])
    return LinkRule(**values)


def apply_rule(url: str, rule: LinkRule) -> str:
    parsed = urlsplit(url)
    original = parse_qsl(parsed.query, keep_blank_values=True)
    kept = [(name, value) for name, value in original if rule.allows(name)]
    query = parsed.query if kept == original else urlencode(kept)
    netloc = parsed.netloc
    if rule.replacement:
        netloc = rule.replacement
        if parsed.port is not None:
            netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


DEFAULT_RULES = (
    LinkRule("item.jd.com", ("item.m.jd.com", "mitem.jd.hk", "3.cn"),
             processor="jd", sample_url="https://item.jd.com/12345678.html?utm_source=share"),
    LinkRule("www.jd.com", ("m.jd.com",), "www.jd.com", ("keyword", "page"),
             sample_url="https://m.jd.com/?keyword=book&utm_source=share"),
    LinkRule("item.taobao.com", ("h5.m.taobao.com", "main.m.taobao.com", "e.tb.cn", "m.tb.cn"),
             allowed_params=("id", "skuid"), processor="taobao",
             sample_url="https://item.taobao.com/item.htm?id=12345678&skuId=9876543210&spm=share"),
    LinkRule("detail.tmall.com", ("detail.m.tmall.com",),
             allowed_params=("id", "skuid"), processor="taobao",
             sample_url="https://detail.tmall.com/item.htm?id=12345678&skuId=9876543210&spm=share"),
    LinkRule("www.taobao.com", ("m.taobao.com",), "www.taobao.com", ("q", "page"),
             sample_url="https://m.taobao.com/?q=book&utm_source=share"),
    LinkRule("www.bilibili.com", ("bilibili.com", "m.bilibili.com", "b23.tv"),
             allowed_params=("p", "t"), processor="bilibili",
             sample_url="https://www.bilibili.com/video/BV1SGad6WEWn/?p=2&t=90&vd_source=share"),
    LinkRule("pixiv.net", replacement="phixiv.net",
             allowed_params=("illust_id", "mode", "id", "p", "word", "lang"),
             sample_url="https://pixiv.net/member_illust.php?illust_id=12345678&mode=medium&utm_source=share"),
    LinkRule("www.pixiv.net", replacement="www.phixiv.net",
             allowed_params=("illust_id", "mode", "id", "p", "word", "lang"),
             sample_url="https://www.pixiv.net/artworks/12345678?utm_source=share"),
    LinkRule("x.com", ("www.x.com", "m.x.com", "mobile.x.com"), "fixupx.com", ("lang", "q", "f"),
             sample_url="https://x.com/example/status/123456789?s=20&t=share&lang=ja"),
    LinkRule("twitter.com", ("www.twitter.com", "m.twitter.com", "mobile.twitter.com"),
             "fxtwitter.com", ("lang", "q", "f"),
             sample_url="https://twitter.com/example/status/123456789?s=20&lang=en"),
)


DEFAULT_BY_HOST = {host: rule for rule in DEFAULT_RULES for host in rule.hosts}


class RuleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("Link rules database requires a newer application version")
            if version == 0:
                connection.execute("CREATE TABLE rules (host TEXT PRIMARY KEY, data TEXT NOT NULL)")
                connection.execute("CREATE TABLE rule_hosts (host TEXT PRIMARY KEY, rule_host TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE changes (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, "
                    "before_data TEXT, after_data TEXT, undone INTEGER NOT NULL DEFAULT 0, "
                    "is_undo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                for rule in DEFAULT_RULES:
                    self._put(connection, replace(rule, revision=-1))
                connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _put(connection, rule: LinkRule):
        connection.execute("INSERT OR REPLACE INTO rules VALUES (?, ?)", (rule.host, encode(rule)))
        connection.execute("DELETE FROM rule_hosts WHERE rule_host = ?", (rule.host,))
        connection.executemany("INSERT INTO rule_hosts VALUES (?, ?)", [(host, rule.host) for host in rule.hosts])

    def list_rules(self) -> list[LinkRule]:
        with self._connect() as connection:
            return [decode(row[0]) for row in connection.execute("SELECT data FROM rules ORDER BY host")]

    def find(self, host: str) -> LinkRule | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT r.data FROM rules r JOIN rule_hosts h ON r.host = h.rule_host WHERE h.host = ?",
                (host.lower().rstrip("."),),
            ).fetchone()
            return decode(row[0]) if row else None

    @staticmethod
    def _check_cycles(connection, rule):
        rules = [decode(item[0]) for item in connection.execute("SELECT data FROM rules")]
        hosts = {host: item for item in rules if item.host != rule.host for host in item.hosts}
        for host in rule.hosts:
            hosts[host] = rule
        current, seen = rule, set()
        while current.enabled and current.replacement and current.replacement not in current.hosts:
            if current.host in seen:
                raise ValueError("域名替换会形成循环，请更换目标域名。")
            seen.add(current.host)
            current = hosts.get(current.replacement)
            if current is None:
                break

    def save(self, rule: LinkRule) -> tuple[LinkRule, int]:
        rule = replace(rule, host=domain(rule.host),
                       aliases=tuple(domain(host) for host in rule.aliases),
                       replacement=domain(rule.replacement) if rule.replacement else None,
                       allowed_params=parameter_names(rule.allowed_params),
                       sample_url=validate_url(rule.sample_url))
        if (urlsplit(rule.sample_url).hostname or "").lower().rstrip(".") not in rule.hosts:
            raise ValueError("示例链接必须属于当前站点。")
        if rule.processor not in {"generic", "jd", "taobao", "bilibili"}:
            raise ValueError("未知的链接解析方式。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT data FROM rules WHERE host = ?", (rule.host,)).fetchone()
            before = decode(row[0]) if row else None
            if (before.revision if before else 0) != rule.revision:
                raise RuleConflict("规则已被修改，请重新打开后编辑。")
            for host in rule.hosts:
                owner = connection.execute("SELECT rule_host FROM rule_hosts WHERE host = ?", (host,)).fetchone()
                if owner and owner[0] != rule.host:
                    raise RuleConflict("这个域名已经属于其他站点规则。")
            self._check_cycles(connection, rule)
            cursor = connection.execute("INSERT INTO changes(host, before_data) VALUES (?, ?)",
                                        (rule.host, encode(before) if before else None))
            rule = replace(rule, revision=cursor.lastrowid)
            self._put(connection, rule)
            connection.execute("UPDATE changes SET after_data = ? WHERE id = ?", (encode(rule), rule.revision))
            return rule, rule.revision

    def delete(self, rule: LinkRule) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT data FROM rules WHERE host = ?", (rule.host,)).fetchone()
            if not row or decode(row[0]).revision != rule.revision:
                raise RuleConflict("规则已被修改，请重新打开后操作。")
            cursor = connection.execute("INSERT INTO changes(host, before_data) VALUES (?, ?)", (rule.host, row[0]))
            connection.execute("DELETE FROM rule_hosts WHERE rule_host = ?", (rule.host,))
            connection.execute("DELETE FROM rules WHERE host = ?", (rule.host,))
            return cursor.lastrowid

    def latest_change(self) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.id FROM changes c WHERE c.undone = 0 AND c.is_undo = 0 "
                "AND c.id = (SELECT MAX(n.id) FROM changes n WHERE n.host = c.host) ORDER BY c.id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

    def undo(self, change_id: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
            if not row or row["undone"] or row["is_undo"]:
                raise RuleConflict("这次修改已经撤销或不存在。")
            latest = connection.execute("SELECT MAX(id) FROM changes WHERE host = ?", (row["host"],)).fetchone()[0]
            if latest != change_id:
                raise RuleConflict("这个站点已有后续修改，不能覆盖它。")
            cursor = connection.execute(
                "INSERT INTO changes(host, before_data, after_data, is_undo) VALUES (?, ?, ?, 1)",
                (row["host"], row["after_data"], row["before_data"]),
            )
            connection.execute("DELETE FROM rule_hosts WHERE rule_host = ?", (row["host"],))
            connection.execute("DELETE FROM rules WHERE host = ?", (row["host"],))
            if row["before_data"]:
                restored = replace(decode(row["before_data"]), revision=cursor.lastrowid)
                self._check_cycles(connection, restored)
                try:
                    self._put(connection, restored)
                except sqlite3.IntegrityError as exc:
                    raise RuleConflict("原域名已被其他规则使用，无法撤销。") from exc
                connection.execute("UPDATE changes SET after_data = ? WHERE id = ?", (encode(restored), cursor.lastrowid))
            connection.execute("UPDATE changes SET undone = 1 WHERE id = ?", (change_id,))
