import ast
import importlib.util
import json
import os
import sys
import types
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "rss-tray.py")


def load_module():
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = types.ModuleType("gi.repository")
    repository.Gtk = types.SimpleNamespace()
    repository.GLib = types.SimpleNamespace()
    repository.Gdk = types.SimpleNamespace()
    repository.Pango = types.SimpleNamespace()
    repository.Wnck = types.SimpleNamespace()
    gi.repository = repository
    cairo = types.ModuleType("cairo")
    cairo.FORMAT_ARGB32 = 0

    feedparser = types.ModuleType("feedparser")
    feedparser.parse = mock.Mock()
    feedparser.FeedParserDict = dict

    with mock.patch.dict(
        sys.modules,
        {"gi": gi, "gi.repository": repository, "cairo": cairo, "feedparser": feedparser},
    ):
        spec = importlib.util.spec_from_file_location("rss_tray_under_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class PureBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_source_parses(self):
        with open(SOURCE, encoding="utf-8") as fh:
            ast.parse(fh.read())

    def test_entry_id_fallback_is_feed_scoped(self):
        entry = {"title": "same", "published": "today"}
        self.assertNotEqual(
            self.mod.entry_id(entry, "https://one.example/feed"),
            self.mod.entry_id(entry, "https://two.example/feed"),
        )

    def test_timer_format(self):
        self.assertEqual(
            self.mod.format_timer_duration(3661),
            "1 Hours, 01 Minutes and 01 Seconds",
        )

    def test_weather_glyph(self):
        self.assertEqual(self.mod.weather_code_glyph(0), "\u2600")
        self.assertEqual(self.mod.weather_code_glyph(45), None)
        self.assertEqual(self.mod.weather_code_glyph(95), "\u26a1")

    def test_update_scan_returns_none_on_failure(self):
        completed = types.SimpleNamespace(returncode=1, stdout="", stderr="failure")
        with mock.patch.object(self.mod.subprocess, "run", return_value=completed):
            self.assertIsNone(self.mod.list_all_updates())

    def test_update_scan_deduplicates_packages(self):
        completed = types.SimpleNamespace(
            returncode=0,
            stdout=(
                "foo-1.0_1 update x86_64 repo 1 1\n"
                "foo-1.0_1 update x86_64 repo 1 1\n"
                "bar-2.0_1 update x86_64 repo 1 1\n"
            ),
            stderr="",
        )
        with mock.patch.object(self.mod.subprocess, "run", return_value=completed):
            self.assertEqual(self.mod.list_all_updates(), ["foo", "bar"])

    def test_twitch_failure_is_distinct_from_no_live_channels(self):
        with mock.patch.object(
            self.mod.urllib.request,
            "urlopen",
            side_effect=OSError("offline"),
        ):
            self.assertIsNone(self.mod.check_twitch_live_channels(["channel"]))

        with mock.patch.object(self.mod.urllib.request, "urlopen") as opener:
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = json.dumps([{
                "data": {"user": {"stream": None}}
            }]).encode()
            opener.return_value = response
            self.assertEqual(
                self.mod.check_twitch_live_channels(["channel"]),
                {},
            )


if __name__ == "__main__":
    unittest.main()
