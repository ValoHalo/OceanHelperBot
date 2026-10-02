import html
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    async def _reply_payloads(self, text=None, **message_fields):
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
                    **message_fields,
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
            (
                "https://fxtwitter.com.example.org/page?utm_source=share&custom=keep",
                "https://fxtwitter.com.example.org/page?custom=keep",
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(await clean_links([source]), [expected])

    async def test_social_links_are_converted_without_network_requests(self):
        cases = (
            (
                "https://www.pixiv.net/en/artworks/12345678/2",
                "https://www.phixiv.net/en/artworks/12345678/2",
            ),
            (
                "https://pixiv.net/artworks/12345678",
                "https://phixiv.net/artworks/12345678",
            ),
            (
                "https://x.com/user/status/123456789/photo/3",
                "https://fixupx.com/user/status/123456789/photo/3",
            ),
            (
                "https://mobile.x.com/i/status/123456789",
                "https://fixupx.com/i/status/123456789",
            ),
            (
                "https://twitter.com/user/status/123456789/video/1",
                "https://fxtwitter.com/user/status/123456789/video/1",
            ),
            (
                "https://www.twitter.com/user/status/123456789",
                "https://fxtwitter.com/user/status/123456789",
            ),
            (
                "http://M.TWITTER.COM:80/user/status/123456789#reply",
                "http://fxtwitter.com:80/user/status/123456789#reply",
            ),
        )
        with patch(
            "httpx.AsyncClient.send",
            new=AsyncMock(side_effect=AssertionError("Unexpected network request")),
        ) as send:
            for source, expected in cases:
                with self.subTest(source=source):
                    self.assertEqual(await clean_links([source]), [expected])
            send.assert_not_awaited()

    async def test_social_cleanup_preserves_content_parameters(self):
        cases = (
            (
                "https://www.pixiv.net/member_illust.php"
                "?illust_id=12345678&mode=medium&utm_source=share&s=1&t=2#image",
                "https://www.phixiv.net/member_illust.php"
                "?illust_id=12345678&mode=medium&s=1&t=2#image",
            ),
            (
                "https://x.com/user/status/123456789"
                "?s=46&t=share-token&lang=ja&utm_source=share&custom=&custom=a%26b#reply",
                "https://fixupx.com/user/status/123456789"
                "?lang=ja&custom=&custom=a%26b#reply",
            ),
            (
                "https://twitter.com/user/status/123456789?S=20&T=share-token&lang=en",
                "https://fxtwitter.com/user/status/123456789?lang=en",
            ),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(await clean_links([source]), [expected])

    async def test_existing_social_previews_are_skipped_without_reply(self):
        urls = [
            "https://phixiv.net/artworks/12345678?utm_source=share",
            "https://www.phixiv.net/artworks/12345678?utm_source=share",
            "https://fixupx.com/user/status/123456789?s=46&t=share-token&utm_source=share",
            "https://fxtwitter.com/user/status/123456789?s=20&t=share-token&utm_source=share",
            "https://g.fixupx.com/user/status/123456789?utm_source=share",
            "https://d.fxtwitter.com/user/status/123456789?s=20&t=share-token",
            "https://c.phixiv.net/artworks/12345678?utm_source=share",
            "https://I.FXTWITTER.COM./user/status/123456789?utm_source=share",
        ]
        with patch(
            "ocean_helper_bot.features.link_cleaner.service.httpx.AsyncClient",
            side_effect=AssertionError("Preview links must be skipped before processing"),
        ) as client:
            self.assertEqual(await clean_links(urls), [])
            client.assert_not_called()

        self.assertEqual(await self._reply_payloads("\n".join(urls)), [])

    async def test_social_conversion_ignores_other_hosts_and_credentials(self):
        for url in (
            "https://x.com.example.org/user/status/123456789",
            "https://twitter.com.example.org/user/status/123456789",
            "https://pixiv.net.example.org/artworks/12345678",
            "https://notx.com/user/status/123456789",
            "https://api.twitter.com/user/status/123456789",
            "https://example.org/x.com/artworks/pixiv.net",
            "https://example.org/?url=https%3A%2F%2Fx.com%2Fuser%2Fstatus%2F123456789",
            "https://x.com@example.org/user/status/123456789",
            "https://user:secret@x.com/user/status/123456789",
            "ftp://twitter.com/user/status/123456789",
        ):
            with self.subTest(url=url):
                self.assertEqual(await clean_links([url]), [])

    async def test_social_conversion_is_idempotent_and_deduplicates(self):
        expected_urls = [
            "https://phixiv.net/artworks/12345678",
            "https://fixupx.com/user/status/123456789",
            "https://fxtwitter.com/user/status/123456789",
        ]
        self.assertEqual(
            await clean_links(
                [
                    "https://pixiv.net/artworks/12345678",
                    "https://x.com/user/status/123456789?s=46&t=one",
                    "https://www.x.com/user/status/123456789?s=20&t=two",
                    "https://twitter.com/user/status/123456789",
                    *expected_urls,
                    *(url + "?utm_source=share&custom=keep" for url in expected_urls),
                ]
            ),
            expected_urls,
        )
        self.assertEqual(await clean_links(expected_urls), [])

    async def test_social_reply_previews_converted_url_in_the_same_topic(self):
        expected_urls = [
            "https://fixupx.com/user/status/123456789?lang=ja&custom=one",
            "https://www.phixiv.net/artworks/12345678",
            "https://fxtwitter.com/user/status/123456789",
        ]
        payloads = await self._reply_payloads(
            "https://x.com/user/status/123456789?s=46&t=share-token&lang=ja&custom=one\n"
            "https://www.pixiv.net/artworks/12345678\n"
            "https://twitter.com/user/status/123456789"
        )
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(
            payload["text"].splitlines(),
            [
                f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'
                for url in expected_urls
            ],
        )
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(
            json.loads(payload["link_preview_options"]),
            {"is_disabled": False, "url": expected_urls[0]},
        )
        self.assertEqual(json.loads(payload["reply_parameters"])["message_id"], 10)
        self.assertEqual(int(payload["message_thread_id"]), 27)

    async def test_social_links_in_captions_and_hidden_text_links(self):
        source_url = "https://www.pixiv.net/artworks/12345678"
        cases = (
            {
                "caption": source_url,
                "caption_entities": [
                    {"type": "url", "offset": 0, "length": len(source_url)},
                ],
            },
            {
                "text": "作品",
                "entities": [
                    {
                        "type": "text_link",
                        "offset": 0,
                        "length": 2,
                        "url": source_url,
                    },
                ],
            },
        )
        for message_fields in cases:
            with self.subTest(message_fields=message_fields):
                payloads = await self._reply_payloads(**message_fields)
                self.assertEqual(len(payloads), 1)
                self.assertEqual(
                    json.loads(payloads[0]["link_preview_options"]),
                    {"is_disabled": False, "url": "https://www.phixiv.net/artworks/12345678"},
                )

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
