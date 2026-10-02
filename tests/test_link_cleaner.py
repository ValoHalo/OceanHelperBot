import html
import json
import unittest
from types import SimpleNamespace

import httpx
from telegram import Bot, Message, Update
from telegram.request import BaseRequest

from ocean_helper_bot.features.link_cleaner.handler import handle_message
from ocean_helper_bot.features.link_cleaner.processors import BilibiliProcessor
from ocean_helper_bot.features.link_cleaner.service import clean_links


VIDEO_URL = "https://www.bilibili.com/video/BV1SGad6WEWn/"


class RecordingTelegramRequest(BaseRequest):
    def __init__(self):
        self.sent_messages = []

    @property
    def read_timeout(self):
        return 5.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        if url.endswith("/getMe"):
            result = {"id": 123, "is_bot": True, "first_name": "Offline test bot"}
        elif url.endswith("/sendMessage"):
            payload = json.loads(request_data.json_payload)
            self.sent_messages.append(payload)
            result = {
                "message_id": 11,
                "date": 0,
                "chat": {"id": int(payload["chat_id"]), "type": "supergroup"},
                "text": payload["text"],
            }
        else:
            raise AssertionError(f"Unexpected Telegram method: {url.rsplit('/', 1)[-1]}")
        return 200, json.dumps({"ok": True, "result": result}).encode()


class LinkCleanerTests(unittest.IsolatedAsyncioTestCase):
    async def _reply_payloads(self, text):
        request = RecordingTelegramRequest()
        async with Bot(
            "123:offline-test-token",
            request=request,
            get_updates_request=RecordingTelegramRequest(),
        ) as bot:
            message = Message.de_json(
                {
                    "message_id": 10,
                    "date": 0,
                    "chat": {"id": -100123, "type": "supergroup"},
                    "from": {"id": 1, "is_bot": False, "first_name": "Test user"},
                    "text": text,
                    "message_thread_id": 27,
                    "is_topic_message": True,
                },
                bot,
            )
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"proxy_url": None})
            )
            await handle_message(Update(1, message=message), context)
        return request.sent_messages

    async def test_clean_video_links_need_no_reply(self):
        for url in (
            VIDEO_URL,
            VIDEO_URL + "?t=90&p=2",
            VIDEO_URL + "?t=0&p=1&custom=&custom=one#reply",
        ):
            with self.subTest(url=url):
                self.assertEqual(await clean_links([url]), [])

    async def test_tracking_cleanup_preserves_playback_and_other_functional_parameters(self):
        cases = (
            (
                VIDEO_URL + "?spm_id_from=333.1007&vd_source=tracking&t=90&p=2#reply",
                VIDEO_URL + "?t=90&p=2#reply",
            ),
            (
                "http://m.bilibili.com/video/BV1SGad6WEWn"
                "?t=0&utm_source=share&p=1&custom=&custom=one#reply",
                VIDEO_URL + "?t=0&p=1&custom=&custom=one#reply",
            ),
            (
                VIDEO_URL + "?t=93.5&p=2&custom=%2Fvalue&share_source=copy",
                VIDEO_URL + "?t=93.5&p=2&custom=%2Fvalue",
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(await clean_links([source]), [expected])

    async def test_missing_video_slash_is_restored(self):
        self.assertEqual(await clean_links([VIDEO_URL.rstrip("/")]), [VIDEO_URL])

    async def test_short_link_redirect_preserves_playback(self):
        def respond(request):
            if request.url.host == "b23.tv":
                return httpx.Response(
                    302,
                    headers={"Location": VIDEO_URL + "?t=90&p=2&vd_source=tracking"},
                )
            return httpx.Response(200)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            result = await BilibiliProcessor().clean("https://b23.tv/test-video", client)
        self.assertEqual(result, VIDEO_URL + "?t=90&p=2")

    async def test_short_link_keeps_the_users_playback_parameters(self):
        def respond(request):
            if request.url.host == "b23.tv":
                return httpx.Response(
                    302,
                    headers={"Location": VIDEO_URL + "?t=99&p=2&vd_source=tracking"},
                )
            return httpx.Response(200)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            result = await BilibiliProcessor().clean(
                "https://b23.tv/test-video?t=0&p=3", client
            )
        self.assertEqual(result, VIDEO_URL + "?t=0&p=3")

    async def test_short_link_body_fallback_preserves_playback(self):
        target = VIDEO_URL + "?t=90&p=2&custom=a%26b&vd_source=tracking#reply"
        bodies = (
            f'<meta property="og:url" content="{html.escape(target, quote=True)}">',
            json.dumps({"url": target}).replace("/", "\\/"),
        )
        for body in bodies:
            with self.subTest(body=body):
                transport = httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, text=body, headers={"Content-Type": "text/html"}
                    )
                )
                async with httpx.AsyncClient(transport=transport) as client:
                    result = await BilibiliProcessor().clean(
                        "https://b23.tv/test-video", client
                    )
                self.assertEqual(result, VIDEO_URL + "?t=90&p=2&custom=a%26b#reply")

    async def test_other_sites_keep_their_existing_cleaning_behavior(self):
        cases = (
            (
                "https://example.com/page/?utm_source=share&t=17&p=2#part",
                "https://example.com/page/?t=17&p=2#part",
            ),
            (
                "https://item.jd.com/12345678.html?utm_source=share",
                "https://item.jd.com/12345678.html",
            ),
            (
                "https://item.taobao.com/item.htm?id=12345678&spm=tracking",
                "https://item.taobao.com/item.htm?id=12345678",
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(await clean_links([source]), [expected])

    async def test_reply_requests_preview_and_retains_the_topic_and_quote(self):
        payloads = await self._reply_payloads(VIDEO_URL + "?t=90&p=2&vd_source=tracking")
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        expected_url = VIDEO_URL + "?t=90&p=2"
        escaped_url = html.escape(expected_url, quote=True)
        self.assertEqual(payload["text"], f'<a href="{escaped_url}">{escaped_url}</a>')
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(
            json.loads(payload["link_preview_options"]),
            {"is_disabled": False, "url": expected_url},
        )
        self.assertEqual(json.loads(payload["reply_parameters"])["message_id"], 10)
        self.assertEqual(int(payload["message_thread_id"]), 27)

    async def test_multiple_cleaned_links_use_the_first_preview(self):
        payloads = await self._reply_payloads(
            VIDEO_URL + "?vd_source=tracking\n"
            "https://example.com/page/?utm_source=share&t=5"
        )
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(len(payload["text"].splitlines()), 2)
        self.assertIn("https://example.com/page/?t=5", payload["text"])
        preview_options = json.loads(payload["link_preview_options"])
        self.assertEqual(preview_options["url"], VIDEO_URL)
        self.assertFalse(preview_options["is_disabled"])

    async def test_unchanged_video_does_not_send_a_message(self):
        self.assertEqual(await self._reply_payloads(VIDEO_URL + "?t=90&p=2"), [])


if __name__ == "__main__":
    unittest.main()
