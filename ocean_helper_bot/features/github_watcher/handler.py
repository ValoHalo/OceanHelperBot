import html
import math
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ocean_helper_bot.config import Settings
from ocean_helper_bot.features.github_watcher.service import GitHubAPIError, GitHubWatcher

REPOSITORY_PATTERN = re.compile(
    r"^(?:https?://github\.com/)?(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
WATCHER_KEY = "github_watcher"
MENU_KEY = "github_repository_menu"
PAGE_SIZE = 10
TELEGRAM_MESSAGE_LIMIT = 4096


async def _is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    user = update.effective_user
    if user is not None and user.id == settings.owner_id:
        return True

    if update.callback_query is not None:
        await update.callback_query.answer("此操作仅限机器人所有者。", show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text("此操作仅限机器人所有者。")
    return False


def _repository_argument(arguments: list[str]) -> str | None:
    if len(arguments) != 1:
        return None
    match = REPOSITORY_PATTERN.fullmatch(arguments[0].strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def _destination(update: Update) -> tuple[int, int | None] | None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return None
    return chat.id, message.message_thread_id


async def follow_repository(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    destination = _destination(update)
    if message is None or destination is None:
        return
    repository = _repository_argument(context.args)
    if repository is None:
        await message.reply_text("用法：/github_follow owner/repo")
        return

    watcher: GitHubWatcher = context.application.bot_data[WATCHER_KEY]
    try:
        canonical_name, created = await watcher.follow(*destination, repository)
    except GitHubAPIError as exc:
        await message.reply_text(str(exc))
        return

    escaped_name = html.escape(canonical_name)
    if created:
        text = f"已关注 <code>{escaped_name}</code>，新 commit 和 release 会发送到这里。"
    else:
        text = f"这里已经关注了 <code>{escaped_name}</code>。"
    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def unfollow_repository(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    destination = _destination(update)
    if message is None or destination is None:
        return
    repository = _repository_argument(context.args)
    if repository is None:
        await message.reply_text("用法：/github_unfollow owner/repo")
        return

    watcher: GitHubWatcher = context.application.bot_data[WATCHER_KEY]
    removed = watcher.unfollow(*destination, repository)
    escaped_name = html.escape(repository)
    if removed:
        text = f"已取消关注 <code>{escaped_name}</code>。"
    else:
        text = f"这里没有关注 <code>{escaped_name}</code>。"
    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def list_repositories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    destination = _destination(update)
    if message is None or destination is None:
        return

    watcher: GitHubWatcher = context.application.bot_data[WATCHER_KEY]
    repositories = watcher.list_follows(*destination)
    if not repositories:
        await message.reply_text("这里还没有关注 GitHub 仓库。")
        return
    for text in _repository_list_messages(repositories):
        await message.reply_text(text, parse_mode=ParseMode.HTML)


def _repository_list_messages(repositories: list[str]) -> list[str]:
    messages: list[str] = []
    current = "这里关注的 GitHub 仓库："
    continuation = "GitHub 仓库（续）："
    for repository in repositories:
        line = f"• <code>{html.escape(repository)}</code>"
        candidate = f"{current}\n{line}"
        if len(candidate) > TELEGRAM_MESSAGE_LIMIT:
            messages.append(current)
            current = f"{continuation}\n{line}"
        else:
            current = candidate
    messages.append(current)
    return messages


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "GitHub 通知管理：\n"
        "/health - 检查机器人是否在线\n"
        "/newintegration - 选择并关注仓库\n"
        "/listintegrations - 查看当前会话的订阅\n"
        "/delintegration - 选择并取消订阅\n"
        "/cancel - 取消当前操作"
    )


def _menu_markup(mode: str, repositories: list[str], page: int) -> InlineKeyboardMarkup:
    page_count = max(1, math.ceil(len(repositories) / PAGE_SIZE))
    page = min(max(page, 0), page_count - 1)
    start_index = page * PAGE_SIZE
    buttons = [
        InlineKeyboardButton(
            repository if len(repository) <= 60 else f"{repository[:57]}...",
            callback_data=f"github_menu:{mode}:{index}",
        )
        for index, repository in enumerate(
            repositories[start_index : start_index + PAGE_SIZE],
            start=start_index,
        )
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("上一页", callback_data=f"github_menu:page:{page - 1}")
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton("下一页", callback_data=f"github_menu:page:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("取消", callback_data="github_menu:cancel")])
    return InlineKeyboardMarkup(rows)


async def new_integration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    destination = _destination(update)
    if message is None or destination is None:
        return

    watcher: GitHubWatcher = context.application.bot_data[WATCHER_KEY]
    try:
        available = await watcher.list_available_repositories()
    except GitHubAPIError as exc:
        await message.reply_text(str(exc))
        return

    followed = set(watcher.list_follows(*destination))
    repositories = [repository for repository in available if repository not in followed]
    if not repositories:
        await message.reply_text("没有可添加的 GitHub 仓库。")
        return

    context.user_data[MENU_KEY] = {
        "mode": "add",
        "repositories": repositories,
        "destination": destination,
    }
    await message.reply_text(
        "选择要接收通知的仓库：",
        reply_markup=_menu_markup("add", repositories, 0),
    )


async def delete_integration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    message = update.effective_message
    destination = _destination(update)
    if message is None or destination is None:
        return

    watcher: GitHubWatcher = context.application.bot_data[WATCHER_KEY]
    repositories = watcher.list_follows(*destination)
    if not repositories:
        await message.reply_text("这里还没有关注 GitHub 仓库。")
        return

    context.user_data[MENU_KEY] = {
        "mode": "delete",
        "repositories": repositories,
        "destination": destination,
    }
    await message.reply_text(
        "选择要取消关注的仓库：",
        reply_markup=_menu_markup("delete", repositories, 0),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    context.user_data.pop(MENU_KEY, None)
    if update.effective_message is not None:
        await update.effective_message.reply_text("已取消当前操作。")


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_owner(update, context):
        return
    query = update.callback_query
    destination = _destination(update)
    if query is None or query.data is None or destination is None:
        return

    menu = context.user_data.get(MENU_KEY)
    if not isinstance(menu, dict):
        await query.answer("此菜单已失效，请重新执行命令。", show_alert=True)
        return

    mode = menu.get("mode")
    repositories = menu.get("repositories")
    menu_destination = menu.get("destination")
    if (
        mode not in {"add", "delete"}
        or not isinstance(repositories, list)
        or menu_destination != destination
    ):
        context.user_data.pop(MENU_KEY, None)
        await query.answer("此菜单已失效，请重新执行命令。", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]
    if action == "cancel":
        context.user_data.pop(MENU_KEY, None)
        await query.answer()
        await query.edit_message_text("已取消当前操作。")
        return
    if action == "page" and len(parts) == 3 and parts[2].isdigit():
        page = int(parts[2])
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=_menu_markup(mode, repositories, page))
        return
    if action != mode or len(parts) != 3 or not parts[2].isdigit():
        await query.answer("无效的操作。", show_alert=True)
        return

    index = int(parts[2])
    if index >= len(repositories):
        await query.answer("此菜单已失效，请重新执行命令。", show_alert=True)
        return
    repository = repositories[index]
    watcher: GitHubWatcher = context.application.bot_data[WATCHER_KEY]

    if mode == "add":
        try:
            canonical_name, created = await watcher.follow(*destination, repository)
        except GitHubAPIError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        result = (
            f"已关注 {canonical_name}，新 commit 和 release 会发送到这里。"
            if created
            else f"这里已经关注了 {canonical_name}。"
        )
    else:
        removed = watcher.unfollow(*destination, repository)
        result = (
            f"已取消关注 {repository}。"
            if removed
            else f"这里没有关注 {repository}。"
        )

    context.user_data.pop(MENU_KEY, None)
    await query.answer()
    await query.edit_message_text(result)
