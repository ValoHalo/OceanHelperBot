import json
import gc
import tempfile
import unittest
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ExtBot
from telegram.request import BaseRequest

from ocean_helper_bot.config import Settings
from ocean_helper_bot.features import register_features
from ocean_helper_bot.features.link_cleaner.management import MENU_KEY, STORE_KEY


class TelegramStub(BaseRequest):
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.counter = 100

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        payload = json.loads(request_data.json_payload) if request_data else {}
        self.calls.append((method, payload))
        if method == "getMe":
            result = {"id": 123, "is_bot": True, "first_name": "Bot", "username": "OfflineBot"}
        elif method in {"sendMessage", "editMessageText"}:
            if method == "sendMessage":
                self.counter += 1
                message_id = self.counter
            else:
                message_id = int(payload["message_id"])
            chat_id = int(payload["chat_id"])
            result = {"message_id": message_id, "date": 0, "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                      "from": {"id": 123, "is_bot": True, "first_name": "Bot"}, "text": payload["text"]}
            if payload.get("reply_markup"):
                markup = json.loads(payload["reply_markup"])
                if "inline_keyboard" in markup:
                    result["reply_markup"] = markup
            self.messages[message_id] = result
        elif method == "answerCallbackQuery":
            result = True
        else:
            raise AssertionError("Unexpected Telegram API method: " + method)
        return 200, json.dumps({"ok": True, "result": result}).encode()


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(gc.collect)
        self.request = TelegramStub()
        bot = ExtBot("123:offline", request=self.request, get_updates_request=TelegramStub())
        self.app = Application.builder().bot(bot).build()
        settings = Settings("123:offline", owner_id=1,
                            github_state_path=Path(self.directory.name) / "github.sqlite3",
                            link_cleaner_state_path=Path(self.directory.name) / "links.sqlite3")
        self.app.bot_data.update(settings=settings, proxy_url=None)
        register_features(self.app)
        self.errors = []
        async def on_error(update, context):
            self.errors.append(context.error)
        self.app.add_error_handler(on_error)
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self.store = self.app.bot_data[STORE_KEY]
        self.update_id = 0

    async def send(self, text, user=1, chat=1, reply=None):
        self.update_id += 1
        message = {"message_id": self.update_id, "date": 0,
                   "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
                   "from": {"id": user, "is_bot": False, "first_name": "User"}, "text": text}
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply:
            message["reply_to_message"] = self.request.messages[reply]
        await self.app.process_update(Update.de_json({"update_id": self.update_id, "message": message}, self.app.bot))
        self.assertEqual(self.errors, [])

    @property
    def menu(self):
        return self.app.user_data[1][MENU_KEY]

    def callback_data(self, label):
        message = self.request.messages[self.menu["message"]]
        for row in message["reply_markup"]["inline_keyboard"]:
            for button in row:
                if label in button["text"]:
                    return button["callback_data"]
        self.fail("Missing button: " + label)

    async def click(self, label=None, data=None, user=1, message_id=None):
        data = data or self.callback_data(label)
        message_id = message_id or self.menu["message"]
        self.update_id += 1
        query = {"id": str(self.update_id), "from": {"id": user, "is_bot": False, "first_name": "User"},
                 "chat_instance": "offline", "message": self.request.messages[message_id], "data": data}
        await self.app.process_update(Update.de_json({"update_id": self.update_id, "callback_query": query}, self.app.bot))
        self.assertEqual(self.errors, [])

    async def test_add_wizard_saves_only_after_preview_and_undo_removes_it(self):
        await self.send("/addlink")
        prompt = self.menu["prompt"]
        await self.send("https://new.example.com/item?id=42&tracking=drop", reply=prompt)
        self.assertIsNone(self.store.find("new.example.com"))
        await self.click("不额外替换")
        await self.click("id")
        await self.click("预览")
        text = self.request.messages[self.menu["message"]]["text"]
        self.assertIn("https://new.example.com/item?id=42", text)
        save_data = self.callback_data("保存规则")
        await self.click(data=save_data)
        self.assertEqual(self.store.find("new.example.com").allowed_params, ("id",))
        revision = self.store.find("new.example.com").revision
        await self.click(data=save_data)
        self.assertEqual(self.store.find("new.example.com").revision, revision)
        await self.click("撤销这次修改")
        self.assertIsNone(self.store.find("new.example.com"))

    async def test_owner_private_gate_and_foreign_callback(self):
        await self.send("/whitelist x.com --clear", user=2, chat=2)
        await self.send("/whitelist x.com --clear", chat=-100)
        self.assertEqual(self.store.find("x.com").allowed_params, ("lang", "q", "f"))
        await self.send("/whitelist x.com --clear")
        await self.click("保存规则", user=2)
        self.assertEqual(self.store.find("x.com").allowed_params, ("lang", "q", "f"))
        await self.click("保存规则")
        self.assertEqual(self.store.find("x.com").allowed_params, ())

    async def test_force_reply_only_consumes_matching_prompt_and_cancel_discards(self):
        await self.send("/replacelink x.com")
        prompt = self.menu["prompt"]
        await self.send("unrelated.example.com")
        self.assertEqual(self.menu["prompt"], prompt)
        await self.send("mirror.example.com", reply=prompt)
        self.assertEqual(self.menu["view"], "preview")
        await self.send("/cancel")
        self.assertNotIn(MENU_KEY, self.app.user_data[1])
        self.assertEqual(self.store.find("x.com").replacement, "fixupx.com")
        await self.send("late.example.com", reply=prompt)
        self.assertEqual(self.store.find("x.com").replacement, "fixupx.com")

    async def test_new_command_invalidates_old_menu_and_prompt(self):
        await self.send("/replacelink x.com")
        old_message = self.menu["message"]
        old_data = self.callback_data("不额外替换")
        old_prompt = self.menu["prompt"]
        await self.send("/links")
        current = self.menu["message"]
        await self.click(data=old_data, message_id=old_message)
        await self.send("mirror.example.com", reply=old_prompt)
        self.assertEqual(self.menu["message"], current)
        self.assertEqual(self.store.find("x.com").replacement, "fixupx.com")

    async def test_command_arguments_persist_and_rules_are_applied_to_group_messages(self):
        await self.send("/whitelist x.com ref")
        await self.click("保存规则")
        await self.send("/replacelink x.com mirror.example.com")
        await self.click("保存规则")
        await self.send("https://x.com/person/status/1?ref=keep&lang=ja", user=2, chat=-100)
        last = [payload for method, payload in self.request.calls if method == "sendMessage"][-1]
        self.assertIn("https://mirror.example.com/person/status/1?ref=keep", last["text"])
        self.assertNotIn("lang=", last["text"])

    async def test_full_list_input_replaces_instead_of_merging_and_clear_is_draft(self):
        await self.send("/whitelist x.com")
        await self.click("手动输入完整名单")
        await self.send("ref\ncustom", reply=self.menu["prompt"])
        self.assertEqual(self.menu["draft"].allowed_params, ("ref", "custom"))
        await self.click("全部清空")
        self.assertEqual(self.menu["draft"].allowed_params, ())
        self.assertEqual(self.store.find("x.com").allowed_params, ("lang", "q", "f"))

    async def test_duplicate_add_opens_existing_rule_and_delete_undo_restores_it(self):
        await self.send("/addlink https://mobile.x.com/person/status/1")
        self.assertEqual(self.menu["draft"].host, "x.com")
        self.assertEqual(self.menu["view"], "detail")
        await self.click("删除规则")
        self.assertIsNotNone(self.store.find("x.com"))
        await self.click("确认删除")
        self.assertIsNone(self.store.find("x.com"))
        await self.click("撤销这次修改")
        self.assertIsNotNone(self.store.find("mobile.x.com"))

    async def test_restore_defaults_and_disable_require_save(self):
        await self.send("/links")
        while "x.com" not in self.request.messages[self.menu["message"]].get("text", ""):
            rows = self.request.messages[self.menu["message"]]["reply_markup"]["inline_keyboard"]
            if any(button["text"].endswith(" x.com") for row in rows for button in row):
                break
            await self.click("下一页")
        await self.click("✓ x.com")
        await self.click("停用")
        self.assertTrue(self.store.find("x.com").enabled)
        await self.click("保存规则")
        self.assertFalse(self.store.find("x.com").enabled)
        await self.send("/addlink https://x.com/person/status/1")
        await self.click("恢复默认")
        await self.click("保存规则")
        self.assertTrue(self.store.find("x.com").enabled)

    async def test_github_cancel_and_help_still_work(self):
        await self.send("/start")
        self.assertIn("/whitelist", self.request.messages[self.request.counter]["text"])
        self.app.user_data[1]["github_repository_menu"] = {"mode": "add"}
        await self.send("/cancel")
        self.assertNotIn("github_repository_menu", self.app.user_data[1])
