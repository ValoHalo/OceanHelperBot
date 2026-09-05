import asyncio
import html
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import TelegramError

LOGGER = logging.getLogger(__name__)
GITHUB_API_URL = "https://api.github.com"
GITHUB_PAGE_SIZE = 100
TELEGRAM_MESSAGE_LIMIT = 4096


class GitHubAPIError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Subscription:
    chat_id: int
    thread_id: int | None
    repository: str
    last_commit_sha: str | None
    last_release_id: int | None


class GitHubWatcher:
    def __init__(
        self,
        bot: Bot,
        state_path: Path,
        token: str | None,
        proxy_url: str | None,
        interval_seconds: int,
    ) -> None:
        self._bot = bot
        self._state_path = state_path
        self._interval_seconds = interval_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "OceanHelperBot",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_URL,
            headers=headers,
            proxy=proxy_url,
            timeout=httpx.Timeout(20.0, connect=10.0),
        )
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._state_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS github_subscriptions (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0,
                    repository TEXT NOT NULL COLLATE NOCASE,
                    last_commit_sha TEXT,
                    last_release_id INTEGER,
                    PRIMARY KEY (chat_id, thread_id, repository)
                )
                """
            )

    async def follow(
        self,
        chat_id: int,
        thread_id: int | None,
        repository: str,
    ) -> tuple[str, bool]:
        repo_data = await self._get_json(f"/repos/{repository}")
        canonical_name = str(repo_data["full_name"])
        commits = await self._get_json(
            f"/repos/{canonical_name}/commits",
            params={"per_page": 1},
            empty_on_conflict=True,
        )
        releases = await self._get_json(
            f"/repos/{canonical_name}/releases",
            params={"per_page": 100},
        )
        releases = [release for release in releases if not release.get("draft")]
        last_commit_sha = str(commits[0]["sha"]) if commits else None
        last_release_id = int(releases[0]["id"]) if releases else None

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO github_subscriptions
                    (chat_id, thread_id, repository, last_commit_sha, last_release_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, thread_id or 0, canonical_name, last_commit_sha, last_release_id),
            )
        return canonical_name, cursor.rowcount == 1

    def unfollow(self, chat_id: int, thread_id: int | None, repository: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM github_subscriptions
                WHERE chat_id = ? AND thread_id = ? AND repository = ?
                """,
                (chat_id, thread_id or 0, repository),
            )
        return cursor.rowcount == 1

    def list_follows(self, chat_id: int, thread_id: int | None) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT repository FROM github_subscriptions
                WHERE chat_id = ? AND thread_id = ?
                ORDER BY repository COLLATE NOCASE
                """,
                (chat_id, thread_id or 0),
            ).fetchall()
        return [str(row["repository"]) for row in rows]

    async def list_available_repositories(self) -> list[str]:
        repositories: list[str] = []
        page = 1
        while True:
            items = await self._get_json(
                "/user/repos",
                params={
                    "affiliation": "owner,collaborator,organization_member",
                    "per_page": 100,
                    "page": page,
                    "sort": "full_name",
                },
            )
            repositories.extend(str(item["full_name"]) for item in items)
            if len(items) < 100:
                break
            page += 1
        return sorted(set(repositories), key=str.casefold)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="github-watcher")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
        await self._client.aclose()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._poll_all()
            except Exception:
                LOGGER.exception("Unexpected failure while polling GitHub")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                pass

    def _subscriptions(self) -> list[Subscription]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, thread_id, repository, last_commit_sha, last_release_id
                FROM github_subscriptions
                ORDER BY repository COLLATE NOCASE
                """
            ).fetchall()
        return [
            Subscription(
                chat_id=int(row["chat_id"]),
                thread_id=int(row["thread_id"]) or None,
                repository=str(row["repository"]),
                last_commit_sha=row["last_commit_sha"],
                last_release_id=row["last_release_id"],
            )
            for row in rows
        ]

    async def _poll_all(self) -> None:
        subscriptions_by_repository: dict[str, list[Subscription]] = {}
        for subscription in self._subscriptions():
            subscriptions_by_repository.setdefault(subscription.repository, []).append(subscription)

        for repository, subscriptions in subscriptions_by_repository.items():
            try:
                commits = await self._get_items_through_cursors(
                    f"/repos/{repository}/commits",
                    "sha",
                    [subscription.last_commit_sha for subscription in subscriptions],
                    empty_on_conflict=True,
                )
                releases = await self._get_items_through_cursors(
                    f"/repos/{repository}/releases",
                    "id",
                    [subscription.last_release_id for subscription in subscriptions],
                )
                releases = [release for release in releases if not release.get("draft")]
            except GitHubAPIError:
                LOGGER.warning(
                    "Could not fetch GitHub repository %s",
                    repository,
                    exc_info=True,
                )
                continue

            for subscription in subscriptions:
                try:
                    await self._poll_subscription(subscription, commits, releases)
                except TelegramError:
                    LOGGER.warning(
                        "Could not deliver GitHub update %s to chat %s",
                        repository,
                        subscription.chat_id,
                        exc_info=True,
                    )

    async def _poll_subscription(
        self,
        subscription: Subscription,
        commits: list[dict[str, Any]],
        releases: list[dict[str, Any]],
    ) -> None:
        new_commits = list(
            reversed(self._items_after(commits, "sha", subscription.last_commit_sha))
        )
        if new_commits:
            await self._send_commits(subscription, new_commits)
            self._update_cursor(subscription, last_commit_sha=str(new_commits[-1]["sha"]))

        new_releases = list(
            reversed(self._items_after(releases, "id", subscription.last_release_id))
        )
        if new_releases:
            await self._send_releases(subscription, new_releases)
            self._update_cursor(subscription, last_release_id=int(new_releases[-1]["id"]))

    @staticmethod
    def _items_after(items: list[dict[str, Any]], key: str, cursor: Any) -> list[dict[str, Any]]:
        if cursor is None:
            return items[:GITHUB_PAGE_SIZE]
        for index, item in enumerate(items):
            if item.get(key) == cursor:
                return items[:index]
        return items[:GITHUB_PAGE_SIZE]

    async def _get_items_through_cursors(
        self,
        path: str,
        key: str,
        cursors: list[Any],
        *,
        empty_on_conflict: bool = False,
    ) -> list[dict[str, Any]]:
        remaining = {cursor for cursor in cursors if cursor is not None}
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            page_items = await self._get_json(
                path,
                params={"per_page": GITHUB_PAGE_SIZE, "page": page},
                empty_on_conflict=empty_on_conflict,
            )
            items.extend(page_items)
            remaining.difference_update(item.get(key) for item in page_items)
            if not remaining or len(page_items) < GITHUB_PAGE_SIZE:
                return items
            page += 1

    def _update_cursor(
        self,
        subscription: Subscription,
        *,
        last_commit_sha: str | None = None,
        last_release_id: int | None = None,
    ) -> None:
        column = "last_commit_sha" if last_commit_sha is not None else "last_release_id"
        value: str | int | None = (
            last_commit_sha if last_commit_sha is not None else last_release_id
        )
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE github_subscriptions SET {column} = ?
                WHERE chat_id = ? AND thread_id = ? AND repository = ?
                """,
                (value, subscription.chat_id, subscription.thread_id or 0, subscription.repository),
            )

    async def _send_commits(
        self,
        subscription: Subscription,
        commits: list[dict[str, Any]],
    ) -> None:
        if len(commits) > 1:
            displayed_commits = commits[-GITHUB_PAGE_SIZE:]
            omitted_count = len(commits) - len(displayed_commits)
            header = (
                f"<b>GitHub commits ({len(commits)})</b> · "
                f"<code>{html.escape(subscription.repository)}</code>"
            )
            header_text = f"GitHub commits ({len(commits)}) · {subscription.repository}"
            footer_text = (
                f"{omitted_count} earlier commits omitted · view all"
                if omitted_count
                else ""
            )
            line_limit = (
                TELEGRAM_MESSAGE_LIMIT
                - len(header_text)
                - len(displayed_commits)
                - (len(footer_text) + 1 if footer_text else 0)
            ) // len(displayed_commits)
            lines: list[str] = []
            for commit in displayed_commits:
                details = commit.get("commit") or {}
                author = str((details.get("author") or {}).get("name") or "Unknown")
                author_limit = min(80, max(1, (line_limit - 13) // 3))
                author = self._shorten(author, author_limit)
                subject = str(details.get("message") or "").splitlines()[0] or "(no message)"
                subject = self._shorten(subject, max(1, line_limit - 13 - len(author)))
                sha = str(commit["sha"])
                url = str(
                    commit.get("html_url")
                    or f"https://github.com/{subscription.repository}/commit/{sha}"
                )
                lines.append(
                    f'• <a href="{html.escape(url, quote=True)}">'
                    f"<code>{html.escape(sha[:7])}</code></a> "
                    f"{html.escape(subject)} · {html.escape(author)}"
                )
            if footer_text:
                url = f"https://github.com/{subscription.repository}/commits"
                lines.append(
                    f'<a href="{html.escape(url, quote=True)}">'
                    f"{html.escape(footer_text)}</a>"
                )
            await self._send(subscription, f"{header}\n" + "\n".join(lines))
            return

        commit = commits[0]
        details = commit.get("commit") or {}
        author = (details.get("author") or {}).get("name") or "Unknown"
        subject = (str(details.get("message") or "").splitlines()[0] or "(no message)")[:1000]
        sha = str(commit["sha"])
        url = str(commit.get("html_url") or f"https://github.com/{subscription.repository}/commit/{sha}")
        text = (
            f"<b>GitHub commit</b> · <code>{html.escape(subscription.repository)}</code>\n"
            f'<a href="{html.escape(url, quote=True)}"><code>{html.escape(sha[:7])}</code></a> '
            f"{html.escape(subject)}\n"
            f"{html.escape(str(author))}"
        )
        await self._send(subscription, text)

    async def _send_releases(
        self,
        subscription: Subscription,
        releases: list[dict[str, Any]],
    ) -> None:
        if len(releases) > 1:
            displayed_releases = releases[-GITHUB_PAGE_SIZE:]
            omitted_count = len(releases) - len(displayed_releases)
            header = (
                f"<b>GitHub releases ({len(releases)})</b> · "
                f"<code>{html.escape(subscription.repository)}</code>"
            )
            header_text = f"GitHub releases ({len(releases)}) · {subscription.repository}"
            footer_text = (
                f"{omitted_count} earlier releases omitted · view all"
                if omitted_count
                else ""
            )
            line_limit = (
                TELEGRAM_MESSAGE_LIMIT
                - len(header_text)
                - len(displayed_releases)
                - (len(footer_text) + 1 if footer_text else 0)
            ) // len(displayed_releases)
            lines: list[str] = []
            for release in displayed_releases:
                tag = str(release.get("tag_name") or "untagged")
                prerelease = " · prerelease" if release.get("prerelease") else ""
                tag_limit = min(
                    80,
                    max(1, (line_limit - 5 - len(prerelease)) // 3),
                )
                tag = self._shorten(tag, tag_limit)
                title = str(release.get("name") or tag).splitlines()[0] or tag
                title = self._shorten(
                    title,
                    max(1, line_limit - 5 - len(prerelease) - len(tag)),
                )
                url = str(
                    release.get("html_url")
                    or f"https://github.com/{subscription.repository}/releases"
                )
                lines.append(
                    f'• <a href="{html.escape(url, quote=True)}">{html.escape(title)}</a> '
                    f"(<code>{html.escape(tag)}</code>){prerelease}"
                )
            if footer_text:
                url = f"https://github.com/{subscription.repository}/releases"
                lines.append(
                    f'<a href="{html.escape(url, quote=True)}">'
                    f"{html.escape(footer_text)}</a>"
                )
            await self._send(subscription, f"{header}\n" + "\n".join(lines))
            return

        release = releases[0]
        tag = str(release.get("tag_name") or "untagged")
        title = str(release.get("name") or tag)[:1000]
        url = str(release.get("html_url") or f"https://github.com/{subscription.repository}/releases")
        prerelease = " · prerelease" if release.get("prerelease") else ""
        text = (
            f"<b>GitHub release{prerelease}</b> · <code>{html.escape(subscription.repository)}</code>\n"
            f'<a href="{html.escape(url, quote=True)}">{html.escape(title)}</a> '
            f"(<code>{html.escape(tag)}</code>)"
        )
        await self._send(subscription, text)

    @staticmethod
    def _shorten(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        if limit <= 3:
            return value[:limit]
        return f"{value[: limit - 3]}..."

    async def _send(self, subscription: Subscription, text: str) -> None:
        await self._bot.send_message(
            chat_id=subscription.chat_id,
            message_thread_id=subscription.thread_id,
            text=text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _get_json(
        self,
        path: str,
        params: dict[str, str | int] | None = None,
        *,
        empty_on_conflict: bool = False,
    ) -> Any:
        try:
            response = await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise GitHubAPIError("暂时无法连接 GitHub，请稍后重试。") from exc
        if response.status_code == 404:
            raise GitHubAPIError("找不到该 GitHub 仓库，或配置的 token 没有访问权限。")
        if response.status_code == 401:
            raise GitHubAPIError("GITHUB_TOKEN 未配置或无效。")
        if response.status_code in {403, 429}:
            raise GitHubAPIError("GitHub API 请求已受限，请稍后重试或配置 GITHUB_TOKEN。")
        if response.status_code == 409 and empty_on_conflict:
            return []
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubAPIError(f"GitHub API 返回 HTTP {response.status_code}。") from exc
        return response.json()
