import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx

from ocean_helper_bot.features.link_cleaner.rules import (
    DEFAULT_RULES, LinkRule, RuleConflict, RuleStore, apply_rule,
)
from ocean_helper_bot.features.link_cleaner.service import clean_links


class RuleStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "rules.sqlite3"
        self.store = RuleStore(self.path)

    def test_migration_preserves_changes_and_deletions_on_restart(self):
        self.assertEqual(len(self.store.list_rules()), len(DEFAULT_RULES))
        old = self.store.find("x.com")
        saved, _ = self.store.save(replace(old, replacement="mirror.example.com", allowed_params=("q",)))
        removed = self.store.find("www.pixiv.net")
        self.store.delete(removed)
        reopened = RuleStore(self.path)
        self.assertEqual(reopened.find("mobile.x.com"), saved)
        self.assertIsNone(reopened.find("www.pixiv.net"))
        self.assertEqual(len(reopened.list_rules()), len(DEFAULT_RULES) - 1)

    def test_failed_initial_migration_rolls_back_all_tables(self):
        other_path = Path(self.directory.name) / "failed.sqlite3"
        class FailingStore(RuleStore):
            @staticmethod
            def _put(connection, rule):
                RuleStore._put(connection, rule)
                raise RuntimeError("simulated interrupted migration")
        with self.assertRaises(RuntimeError):
            FailingStore(other_path)
        with closing(sqlite3.connect(other_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall(), [])
        self.assertEqual(len(RuleStore(other_path).list_rules()), len(DEFAULT_RULES))

    def test_undo_survives_restart_and_undoes_creations_deletions_and_edits(self):
        new = LinkRule("example.com", sample_url="https://example.com/?a=1")
        saved, creation = self.store.save(new)
        RuleStore(self.path).undo(creation)
        self.assertIsNone(self.store.find("example.com"))
        with self.assertRaises(RuleConflict):
            self.store.undo(creation)
        before = self.store.find("x.com")
        changed, change = self.store.save(replace(before, allowed_params=()))
        self.store.undo(change)
        restored = self.store.find("x.com")
        self.assertEqual(restored.allowed_params, before.allowed_params)
        deletion = self.store.delete(restored)
        self.store.undo(deletion)
        self.assertEqual(self.store.find("x.com").replacement, before.replacement)

    def test_stale_edits_and_undos_do_not_overwrite_newer_changes(self):
        old = self.store.find("x.com")
        saved, first = self.store.save(replace(old, allowed_params=("lang",)))
        self.store.save(replace(saved, allowed_params=("q",)))
        with self.assertRaises(RuleConflict):
            self.store.save(old)
        with self.assertRaises(RuleConflict):
            self.store.undo(first)
        self.assertEqual(self.store.find("x.com").allowed_params, ("q",))

    def test_deleted_seed_cannot_be_recreated_by_stale_draft(self):
        rule = self.store.find("x.com")
        self.store.delete(rule)
        with self.assertRaises(RuleConflict):
            self.store.save(rule)

    def test_alias_collision_and_replacement_loop_are_atomic(self):
        with self.assertRaises(RuleConflict):
            self.store.save(LinkRule("mobile.x.com", sample_url="https://mobile.x.com/test"))
        self.store.save(LinkRule("a.example.com", replacement="b.example.com", sample_url="https://a.example.com/"))
        with self.assertRaises(ValueError):
            self.store.save(LinkRule("b.example.com", replacement="a.example.com", sample_url="https://b.example.com/"))
        self.assertIsNone(self.store.find("b.example.com"))

    def test_whitelist_preserves_values_duplicates_empty_values_and_fragments(self):
        rule = LinkRule("example.com", allowed_params=("ref", "x"))
        self.assertEqual(apply_rule("https://example.com/?ref=a%26b&x=&x=2&other=3#part", rule),
                         "https://example.com/?ref=a%26b&x=&x=2#part")
        self.assertEqual(apply_rule("https://example.com/?x=1#part", replace(rule, allowed_params=())),
                         "https://example.com/#part")

    def test_undo_cannot_create_a_replacement_cycle(self):
        a, _ = self.store.save(LinkRule("a.example.com", replacement="b.example.com", sample_url="https://a.example.com/"))
        _, change = self.store.save(replace(a, replacement="c.example.com"))
        self.store.save(LinkRule("b.example.com", replacement="a.example.com", sample_url="https://b.example.com/"))
        with self.assertRaises(ValueError):
            self.store.undo(change)
        self.assertEqual(self.store.find("a.example.com").replacement, "c.example.com")

    def test_international_domain_is_normalized_when_saving(self):
        from ocean_helper_bot.features.link_cleaner.rules import domain
        host = domain("例子.com")
        saved, _ = self.store.save(LinkRule(host, sample_url="https://例子.com/?q=test"))
        self.assertTrue(saved.sample_url.startswith("https://" + host))


class ConfiguredCleaningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = RuleStore(Path(self.directory.name) / "rules.sqlite3")

    async def test_added_rules_apply_immediately_and_survive_restart(self):
        rule = LinkRule("example.com", replacement="mirror.example.com", allowed_params=("ref",),
                        sample_url="https://example.com/path?ref=useful&unknown=remove")
        self.store.save(rule)
        reopened = RuleStore(self.store.path)
        self.assertEqual(await clean_links([rule.sample_url], store=reopened),
                         ["https://mirror.example.com/path?ref=useful"])

    async def test_custom_whitelist_is_not_preempted_by_builtin_cleaners(self):
        cases = (
            ("www.bilibili.com", "https://www.bilibili.com/video/BV1SGad6WEWn/?t=1&ref=keep&utm_source=keep&drop=1",
             "https://www.bilibili.com/video/BV1SGad6WEWn/?ref=keep&utm_source=keep"),
            ("item.taobao.com", "https://item.taobao.com/item.htm?id=12345678&ref=keep&utm_source=keep&drop=1",
             "https://item.taobao.com/item.htm?ref=keep&utm_source=keep"),
            ("item.jd.com", "https://item.jd.com/12345678.html?ref=keep&utm_source=keep&drop=1",
             "https://item.jd.com/12345678.html?ref=keep&utm_source=keep"),
            ("x.com", "https://x.com/user/status/123?ref=keep&utm_source=keep&drop=1",
             "https://fixupx.com/user/status/123?ref=keep&utm_source=keep"),
        )
        for host, source, expected in cases:
            with self.subTest(host=host):
                rule = self.store.find(host)
                self.store.save(replace(rule, allowed_params=("ref", "utm_source")))
                self.assertEqual(await clean_links([source], store=self.store), [expected])

    async def test_disabled_rules_and_deleted_rules_do_not_use_hardcoded_conversion(self):
        rule = self.store.find("x.com")
        self.store.save(replace(rule, enabled=False))
        source = "https://x.com/user/status/123?s=20"
        self.assertEqual(await clean_links([source], store=self.store), [])
        self.store.delete(self.store.find("x.com"))
        self.assertEqual(await clean_links([source], store=self.store), [])

    async def test_empty_whitelist_removes_all_query_parameters(self):
        rule = self.store.find("www.bilibili.com")
        self.store.save(replace(rule, allowed_params=()))
        self.assertEqual(await clean_links(["https://www.bilibili.com/video/BV1SGad6WEWn/?p=2&t=90"], store=self.store),
                         ["https://www.bilibili.com/video/BV1SGad6WEWn/"])

    async def test_taobao_preserves_encoded_values_in_explicit_whitelist(self):
        rule = self.store.find("item.taobao.com")
        self.store.save(replace(rule, allowed_params=("id", "ref")))
        source = "https://item.taobao.com/item.htm?id=12345678&ref=a%26b%3Dc&ref=&drop=1"
        self.assertEqual(await clean_links([source], store=self.store),
                         ["https://item.taobao.com/item.htm?id=12345678&ref=a%26b%3Dc&ref="])

    async def test_short_link_resolution_applies_saved_whitelist_after_resolving(self):
        cases = (
            ("item.taobao.com", "https://e.tb.cn/test", "https://item.taobao.com/item.htm?id=12345678&ref=a%26b&drop=1", "https://item.taobao.com/item.htm?ref=a%26b"),
            ("www.bilibili.com", "https://b23.tv/test", "https://www.bilibili.com/video/BV1SGad6WEWn/?t=90&ref=a%26b&drop=1", "https://www.bilibili.com/video/BV1SGad6WEWn/?ref=a%26b"),
            ("item.jd.com", "https://3.cn/test", "https://item.jd.com/12345678.html?ref=a%26b&drop=1", "https://item.jd.com/12345678.html?ref=a%26b"),
        )
        for host, source, target, expected in cases:
            with self.subTest(host=host):
                self.store.save(replace(self.store.find(host), allowed_params=("ref",)))
                def respond(request):
                    if str(request.url) == source:
                        return httpx.Response(302, headers={"Location": target})
                    return httpx.Response(200)
                def client(_=None):
                    return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True)
                with patch("ocean_helper_bot.features.link_cleaner.service.link_client", client):
                    self.assertEqual(await clean_links([source], store=self.store), [expected])

    async def test_failed_short_link_does_not_reply(self):
        def client(_=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="unresolved")))
        with patch("ocean_helper_bot.features.link_cleaner.service.link_client", client):
            self.assertEqual(await clean_links(["https://b23.tv/test?p=2&ref=1"], store=self.store), [])

    async def test_short_link_keeps_explicit_allowed_source_parameters(self):
        self.store.save(replace(self.store.find("www.bilibili.com"), allowed_params=("ref",)))
        def respond(request):
            if request.url.host == "b23.tv":
                return httpx.Response(302, headers={"Location": "https://www.bilibili.com/video/BV1SGad6WEWn/?t=90"})
            return httpx.Response(200)
        def client(_=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True)
        with patch("ocean_helper_bot.features.link_cleaner.service.link_client", client):
            self.assertEqual(await clean_links(["https://b23.tv/test?ref=a%26b&ref=&drop=1"], store=self.store),
                             ["https://www.bilibili.com/video/BV1SGad6WEWn/?ref=a%26b&ref="])
