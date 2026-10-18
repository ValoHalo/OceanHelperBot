import secrets
import time
from dataclasses import replace
from urllib.parse import parse_qsl, urlsplit

import httpx
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ocean_helper_bot.features.link_cleaner.rules import (
    DEFAULT_BY_HOST, LinkRule, RuleConflict, RuleStore, domain, parameter_names, validate_url,
)
from ocean_helper_bot.features.link_cleaner.service import preview_rule

STORE_KEY = "link_rule_store"
MENU_KEY = "link_rule_menu"
PAGE_SIZE = 8
SESSION_SECONDS = 1800


def _store(context) -> RuleStore:
    return context.application.bot_data[STORE_KEY]


async def _allowed(update, context) -> bool:
    if update.effective_user is None or update.effective_user.id != context.application.bot_data["settings"].owner_id:
        text = "此操作仅限机器人所有者。"
    elif update.effective_chat is None or update.effective_chat.type != "private":
        text = "请在与机器人的私聊中管理链接规则；规则对所有会话生效。"
    else:
        return True
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    return False


def _active(update, context):
    menu = getattr(context, "user_data", {}).get(MENU_KEY)
    if not menu or not update.effective_chat or menu["chat"] != update.effective_chat.id:
        return None
    if time.monotonic() - menu["touched"] > SESSION_SECONDS:
        context.user_data.pop(MENU_KEY, None)
        return None
    return menu


def _summary(rule):
    params = "、".join(rule.allowed_params) or "空（删除全部查询参数）"
    aliases = "、".join(rule.aliases) or "无"
    return (
        f"站点：{rule.host}\n作用范围：所有会话\n"
        f"包含域名：{aliases}\n状态：{'启用' if rule.enabled else '停用'}\n"
        f"替换目标：{rule.replacement or '不额外替换（保留解析后的域名）'}\n"
        f"参数白名单：{params}\n未列出的参数全部删除。"
    )


async def _show(update, context, menu, view, note=""):
    menu.update(view=view, token=secrets.token_hex(4), prompt=None, input=None, touched=time.monotonic())
    def button(label, action):
        return InlineKeyboardButton(label, callback_data=f"lr:{menu['token']}:{action}")
    rows = []
    rule = menu.get("draft")
    if rule:
        menu.setdefault("drafts", {})[rule.host] = rule
    if view == "list":
        rules = _store(context).list_rules()
        menu["choices"] = [item.host for item in rules]
        page = min(max(menu.get("page", 0), 0), max(0, (len(rules) - 1) // PAGE_SIZE))
        menu["page"] = page
        text = "选择站点规则\n作用范围：所有会话"
        for index in range(page * PAGE_SIZE, min(len(rules), (page + 1) * PAGE_SIZE)):
            item = rules[index]
            rows.append([button(f"{'✓' if item.enabled else '停用'} {item.host[:48]}", f"pick:{index}")])
        nav = []
        if page:
            nav.append(button("上一页", f"page:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(rules):
            nav.append(button("下一页", f"page:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("添加站点", "add")])
        change = _store(context).latest_change()
        if change:
            menu["undo"] = change
            rows.append([button("撤销上次修改", "undo")])
    elif view == "detail":
        text = _summary(rule)
        rows = [[button("替换链接", "replace"), button("参数白名单", "params")],
                [button("更换示例", "sample"), button("停用" if rule.enabled else "启用", "enable")]]
        if rule.host in DEFAULT_BY_HOST:
            rows.append([button("恢复默认", "reset")])
        rows.extend([[button("删除规则", "delete")], [button("返回列表", "list")]])
    elif view == "replace":
        text = _summary(rule) + "\n\n设置链接替换。"
        rows = [[button("输入目标域名", "target")], [button("不额外替换", "keep")],
                [button("返回规则", "detail")]]
    elif view == "params":
        names = list(dict.fromkeys([name for name, _ in parse_qsl(urlsplit(rule.sample_url).query, keep_blank_values=True)] + list(rule.allowed_params)))
        menu["parameters"] = names
        page = min(max(menu.get("param_page", 0), 0), max(0, (len(names) - 1) // PAGE_SIZE))
        menu["param_page"] = page
        text = _summary(rule) + "\n\n点击参数切换是否保留。所有修改先保存在草稿中。"
        buttons = [button(f"{'☑' if rule.allows(names[index]) else '☐'} {names[index][:32]}", f"param:{index}")
                   for index in range(page * PAGE_SIZE, min(len(names), (page + 1) * PAGE_SIZE))]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        nav = []
        if page:
            nav.append(button("上一页", f"params_page:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(names):
            nav.append(button("下一页", f"params_page:{page + 1}"))
        if nav:
            rows.append(nav)
        rows += [[button("手动输入完整名单", "names"), button("全部清空", "clear")],
                 [button("预览", "preview"), button("返回规则", "detail")]]
    elif view == "preview":
        menu["can_save"] = False
        try:
            result = await preview_rule(rule, context.application.bot_data.get("proxy_url"))
            source_names = list(dict.fromkeys(name for name, _ in parse_qsl(urlsplit(rule.sample_url).query, keep_blank_values=True)))
            removed = [name for name in source_names if not rule.allows(name)] if rule.enabled else []
            text = (_summary(rule) + f"\n\n原链接：\n{rule.sample_url[:1000]}"
                    + f"\n\n处理结果：\n{result[:1000]}\n\n示例中删除的参数：{'、'.join(removed)[:600] or '无'}")
            if len(rule.sample_url) > 1000 or len(result) > 1000:
                text += "\n（长链接的显示已截断，实际处理使用完整链接。）"
            menu["can_save"] = True
            rows.append([button("保存规则", "save")])
        except (httpx.HTTPError, ValueError):
            text = _summary(rule) + "\n\n无法预览此链接，请更换示例后重试。规则尚未保存。"
        rows += [[button("修改替换", "replace"), button("修改白名单", "params")],
                 [button("更换示例", "sample"), button("返回规则", "detail")]]
    elif view == "delete":
        text = f"删除 {rule.host} 的规则？\n删除后不再执行该站点的专用转换，未配置站点仍使用通用跟踪参数清理。"
        rows = [[button("确认删除", "delete_confirm"), button("返回规则", "detail")]]
    elif view == "saved":
        text = "已保存，对所有会话立即生效。"
        rows = [[button("查看规则", "list"), button("继续添加", "add")], [button("撤销这次修改", "undo")]]
    else:
        text = "发送一条需要添加的原始链接。"
    rows.append([button("取消", "cancel")])
    if note:
        text = note + "\n\n" + text
    if len(text.encode("utf-16-le")) > 7800:
        text = text.encode("utf-16-le")[:7600].decode("utf-16-le", errors="ignore") + "\n…内容过长，显示已截断。"
    markup = InlineKeyboardMarkup(rows)
    kwargs = dict(text=text, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
    if menu.get("message"):
        try:
            await context.bot.edit_message_text(chat_id=menu["chat"], message_id=menu["message"], **kwargs)
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            if "message to edit not found" not in str(exc).lower():
                raise
    sent = await context.bot.send_message(chat_id=menu["chat"], **kwargs)
    menu["message"] = sent.message_id


async def _ask(update, context, menu, kind, error=""):
    prompts = {
        "add": "请回复这条消息，发送一条原始链接。",
        "target": "请回复这条消息，发送目标域名，例如 fixupx.com。",
        "names": "请回复这条消息，发送完整白名单，用空格或换行分隔。新名单会替换草稿中的原名单；清空请点击“全部清空”。",
        "sample": "请回复这条消息，发送当前站点的另一条示例链接。",
    }
    message = await context.bot.send_message(chat_id=menu["chat"], text=(error + "\n" if error else "") + prompts[kind],
                                             reply_markup=ForceReply(selective=True))
    menu.update(prompt=message.message_id, input=kind, touched=time.monotonic())


async def _select(update, context, menu, rule):
    cached = menu.get("drafts", {}).get(rule.host)
    if cached is not None and cached.revision == rule.revision:
        rule = cached
    menu["draft"] = rule
    menu["creating"] = False
    if menu["mode"] == "whitelist":
        await _show(update, context, menu, "params")
    elif menu["mode"] == "replacelink":
        await _show(update, context, menu, "replace")
        await _ask(update, context, menu, "target")
    else:
        await _show(update, context, menu, "detail")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE, *, mode: str):
    if not await _allowed(update, context):
        return
    menu = {"chat": update.effective_chat.id, "message": None, "mode": mode, "page": 0, "touched": time.monotonic()}
    context.user_data[MENU_KEY] = menu
    args = context.args or []
    try:
        if mode == "addlink":
            if len(args) > 1:
                raise ValueError("用法：/addlink 或 /addlink <原始链接>")
            await _show(update, context, menu, "add")
            if args:
                await _new_link(update, context, menu, args[0])
            else:
                await _ask(update, context, menu, "add")
        elif args and mode in {"replacelink", "whitelist"}:
            host = domain(args[0])
            rule = _store(context).find(host)
            if rule is None:
                raise ValueError("这个站点还没有规则，请先使用 /addlink。")
            menu.update(draft=rule, creating=False)
            if mode == "replacelink" and len(args) == 2:
                menu["draft"] = replace(rule, replacement=domain(args[1]))
                await _show(update, context, menu, "preview")
            elif mode == "whitelist" and len(args) > 1:
                names = () if args[1:] == ["--clear"] else parameter_names(args[1:])
                menu["draft"] = replace(rule, allowed_params=names)
                await _show(update, context, menu, "preview")
            elif len(args) == 1:
                await _select(update, context, menu, rule)
            else:
                raise ValueError("用法：/replacelink <原域名> <目标域名>")
        else:
            await _show(update, context, menu, "list")
    except ValueError as exc:
        await _show(update, context, menu, "list", str(exc))


async def _new_link(update, context, menu, value):
    value = validate_url(value)
    host = domain(urlsplit(value).hostname)
    rule = _store(context).find(host)
    menu.update(draft=replace(rule, sample_url=value) if rule else LinkRule(host, sample_url=value), creating=rule is None)
    await _show(update, context, menu, "detail" if rule else "replace",
                "该站点已有规则，已打开现有规则。" if rule else "")


async def handle_input(update, context) -> bool:
    menu = _active(update, context)
    message = update.effective_message
    if not menu or not menu.get("prompt") or not message or not message.reply_to_message:
        return False
    if message.reply_to_message.message_id != menu["prompt"]:
        return False
    if not await _allowed(update, context):
        return True
    kind = menu["input"]
    value = (message.text or "").strip()
    try:
        if kind == "add":
            await _new_link(update, context, menu, value)
        elif kind == "target":
            menu["draft"] = replace(menu["draft"], replacement=domain(value))
            await _show(update, context, menu, "params" if menu.get("creating") else "preview")
        elif kind == "names":
            if not value:
                raise ValueError("请输入参数名；清空请点击“全部清空”。")
            menu["draft"] = replace(menu["draft"], allowed_params=parameter_names(value.split()))
            await _show(update, context, menu, "params")
        elif kind == "sample":
            value = validate_url(value)
            if domain(urlsplit(value).hostname) not in menu["draft"].hosts:
                raise ValueError("示例链接必须属于当前站点或其包含域名。")
            menu["draft"] = replace(menu["draft"], sample_url=value)
            await _show(update, context, menu, "preview")
    except ValueError as exc:
        await _ask(update, context, menu, kind, str(exc))
    return True


async def cancel_menu(update, context) -> bool:
    menu = _active(update, context)
    if not menu:
        return False
    context.user_data.pop(MENU_KEY, None)
    if menu.get("message"):
        try:
            await context.bot.edit_message_text(chat_id=menu["chat"], message_id=menu["message"], text="已关闭菜单，未保存的修改已丢弃。")
        except BadRequest as exc:
            if "message to edit not found" not in str(exc).lower():
                raise
    return True


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _allowed(update, context):
        return
    query = update.callback_query
    menu = _active(update, context)
    parts = (query.data or "").split(":")
    if not menu or len(parts) < 3 or parts[1] != menu.get("token") or query.message.message_id != menu.get("message"):
        await query.answer("菜单已失效，请使用命令重新打开。", show_alert=True)
        return
    action = parts[2]
    await query.answer()
    try:
        if action == "cancel":
            await cancel_menu(update, context)
            return
        if action == "pick":
            rule = _store(context).find(menu["choices"][int(parts[3])])
            if rule is None:
                raise RuleConflict("规则已删除，请重新选择。")
            await _select(update, context, menu, rule)
        elif action in {"page", "params_page"}:
            menu["page" if action == "page" else "param_page"] = int(parts[3])
            await _show(update, context, menu, "list" if action == "page" else "params")
        elif action == "add":
            menu.update(mode="addlink", page=0)
            await _show(update, context, menu, "add")
            await _ask(update, context, menu, "add")
        elif action in {"target", "names", "sample"}:
            await _show(update, context, menu, menu["view"])
            await _ask(update, context, menu, action)
        elif action == "keep":
            menu["draft"] = replace(menu["draft"], replacement=None)
            await _show(update, context, menu, "params" if menu.get("creating") else "preview")
        elif action == "param":
            name = menu["parameters"][int(parts[3])]
            rule = menu["draft"]
            names = tuple(item for item in rule.allowed_params if item != name and not (
                rule.processor == "taobao" and item.lower() == name.lower() == "skuid"
            )) if rule.allows(name) else (*rule.allowed_params, name)
            menu["draft"] = replace(rule, allowed_params=parameter_names(names))
            await _show(update, context, menu, "params")
        elif action == "clear":
            menu["draft"] = replace(menu["draft"], allowed_params=())
            await _show(update, context, menu, "params", "白名单已清空；保存后将删除全部查询参数。")
        elif action == "enable":
            menu["draft"] = replace(menu["draft"], enabled=not menu["draft"].enabled)
            await _show(update, context, menu, "preview")
        elif action == "reset":
            rule = menu["draft"]
            menu["draft"] = replace(DEFAULT_BY_HOST[rule.host], revision=rule.revision)
            await _show(update, context, menu, "preview")
        elif action == "save" and menu["view"] == "preview" and menu.get("can_save"):
            menu["draft"], menu["undo"] = _store(context).save(menu["draft"])
            menu["creating"] = False
            await _show(update, context, menu, "saved")
        elif action == "delete_confirm" and menu["view"] == "delete":
            menu["undo"] = _store(context).delete(menu["draft"])
            await _show(update, context, menu, "saved", "规则已删除。")
        elif action == "undo":
            _store(context).undo(menu["undo"])
            menu.pop("draft", None)
            menu.pop("drafts", None)
            menu["mode"] = "links"
            await _show(update, context, menu, "list", "已撤销上次修改。")
        elif action in {"list", "detail", "replace", "params", "preview", "delete"}:
            if action == "list":
                menu["mode"] = "links"
            await _show(update, context, menu, action)
    except RuleConflict as exc:
        menu.pop("draft", None)
        menu.pop("drafts", None)
        await _show(update, context, menu, "list", str(exc))
    except ValueError as exc:
        await _show(update, context, menu, menu["view"], str(exc))
    except (IndexError, KeyError):
        await _show(update, context, menu, "list", "操作已失效，请重新选择。")
