import importlib.util
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location('rss_tray', ROOT / 'rss-tray.py')
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


class TmpConfigCase(unittest.TestCase):
    """Redirects CONFIG_FILE / STATE_FILE to a temp dir for each test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patcher = mock.patch.multiple(
            rt,
            CONFIG_FILE=os.path.join(self.dir, 'config.conf'),
            STATE_FILE=os.path.join(self.dir, 'state.json'),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def conf(self, text):
        with open(rt.CONFIG_FILE, 'w') as f:
            f.write(text)

    def state_text(self, text):
        with open(rt.STATE_FILE, 'w') as f:
            f.write(text)


class TestFeeds(TmpConfigCase):
    def test_missing_config_gives_no_feeds(self):
        self.assertEqual(rt.load_feeds(), [])

    def test_url_only_uses_default_interval(self):
        self.conf('[feeds]\nhttps://a.example/feed\n')
        self.assertEqual(rt.load_feeds(), [('https://a.example/feed', None, rt.CHECK_INTERVAL)])

    def test_name_and_interval(self):
        self.conf('[feeds]\nhttps://a|My Blog|5\n')
        self.assertEqual(rt.load_feeds(), [('https://a', 'My Blog', 300)])

    def test_empty_name_keeps_interval(self):
        self.conf('[feeds]\nhttps://a||5\n')
        self.assertEqual(rt.load_feeds(), [('https://a', None, 300)])

    def test_bad_interval_falls_back_to_default(self):
        self.conf('[feeds]\nhttps://a|x|abc\n')
        self.assertEqual(rt.load_feeds(), [('https://a', 'x', rt.CHECK_INTERVAL)])

    def test_zero_interval_clamped_to_one_minute(self):
        self.conf('[feeds]\nhttps://a||0\n')
        self.assertEqual(rt.load_feeds()[0][2], 60)

    def test_comments_blanks_and_unknown_sections_ignored(self):
        self.conf('# c\n\n[feeds]\n# x\nhttps://a\n[other]\nhttps://b\n')
        self.assertEqual([f[0] for f in rt.load_feeds()], ['https://a'])

    def test_section_names_case_insensitive(self):
        self.conf('[FEEDS]\nhttps://a\n')
        self.assertEqual(len(rt.load_feeds()), 1)


class TestMute(TmpConfigCase):
    def test_phrases_lowercased_and_pipe_split(self):
        self.conf('[mute]\nSponsored | Giveaway\nAd\n||\n')
        self.assertEqual(rt.load_mute_filters(), ['sponsored', 'giveaway', 'ad'])

    def test_is_muted_case_insensitive_substring(self):
        self.assertTrue(rt.is_muted('Big GIVEAWAY today', ['giveaway']))

    def test_is_muted_no_match_or_no_phrases(self):
        self.assertFalse(rt.is_muted('Hello', ['giveaway']))
        self.assertFalse(rt.is_muted('Hello', []))


class TestTwitchChannels(TmpConfigCase):
    def test_lowercased_deduped_order_preserved(self):
        self.conf('[twitch]\nFoo\nbar\nfoo\n')
        self.assertEqual(rt.load_twitch_channels(), ['foo', 'bar'])


class TestState(TmpConfigCase):
    def test_missing_file_gives_defaults(self):
        s = rt.load_state()
        self.assertEqual(s['seen'], {})
        self.assertEqual(s['unread'], [])
        self.assertEqual(s['available_updates'], [])
        self.assertEqual(s['live_channels'], [])
        self.assertEqual(s['last_checked'], {})

    def test_corrupt_json_gives_defaults(self):
        self.state_text('{not json')
        self.assertEqual(rt.load_state()['unread'], [])

    def test_non_dict_json_gives_defaults(self):
        self.state_text('[1, 2]')
        self.assertEqual(rt.load_state()['seen'], {})

    def test_wrong_shaped_keys_reset(self):
        self.state_text(json.dumps({
            'seen': [], 'unread': 'x', 'available_updates': {},
            'live_channels': 5, 'last_checked': [],
        }))
        s = rt.load_state()
        self.assertEqual(s['seen'], {})
        self.assertEqual(s['unread'], [])
        self.assertEqual(s['available_updates'], [])
        self.assertEqual(s['live_channels'], [])
        self.assertEqual(s['last_checked'], {})

    def test_valid_keys_preserved(self):
        self.state_text(json.dumps({'unread': [{'id': 'a'}], 'seen': {'a': 1}}))
        s = rt.load_state()
        self.assertEqual(s['unread'], [{'id': 'a'}])
        self.assertEqual(s['seen'], {'a': 1})

    def test_save_roundtrip_leaves_no_tmp_file(self):
        rt.save_state({'a': 1})
        with open(rt.STATE_FILE) as f:
            self.assertEqual(json.load(f), {'a': 1})
        self.assertFalse(os.path.exists(rt.STATE_FILE + '.tmp'))


class TestEntryHelpers(unittest.TestCase):
    def test_id_preferred_over_link(self):
        self.assertEqual(rt.entry_id({'id': 'x', 'link': 'y'}), rt.entry_id({'id': 'x', 'link': 'z'}))

    def test_link_fallback(self):
        self.assertEqual(rt.entry_id({'link': 'y'}), rt.entry_id({'link': 'y', 'title': 't'}))

    def test_title_and_published_fallback(self):
        a = rt.entry_id({'title': 't', 'published': '1'})
        b = rt.entry_id({'title': 't', 'published': '2'})
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 40)

    def test_age_none_without_date(self):
        self.assertIsNone(rt.entry_age_seconds({}))

    def test_age_from_published_parsed(self):
        entry = {'published_parsed': time.gmtime(time.time() - 3600)}
        self.assertAlmostEqual(rt.entry_age_seconds(entry), 3600, delta=5)


class TestTimerFormat(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(rt.format_timer_duration(0), '0 Hours, 00 Minutes and 00 Seconds')
        self.assertEqual(rt.format_timer_duration(3661), '1 Hours, 01 Minutes and 01 Seconds')
        self.assertEqual(rt.format_timer_duration(7200), '2 Hours, 00 Minutes and 00 Seconds')


class TestParseXbpsUpdates(unittest.TestCase):
    def test_real_line(self):
        out = 'cryptsetup-2.8.8_1 update x86_64 https://repo-default.voidlinux.org/current 3203607 568523\n'
        self.assertEqual(rt.parse_xbps_updates(out), ['cryptsetup'])

    def test_hyphenated_pkgname(self):
        out = 'python3-feedparser-6.0.11_1 update x86_64 https://r 1 2\n'
        self.assertEqual(rt.parse_xbps_updates(out), ['python3-feedparser'])

    def test_non_update_actions_and_junk_ignored(self):
        out = ('foo-1.0_1 install x86_64 https://r 1 2\n'
               'bar-1.0_1 remove x86_64 https://r 1 2\n'
               '\nshort\n'
               'baz-2.0_1 update x86_64 https://r 1 2\n')
        self.assertEqual(rt.parse_xbps_updates(out), ['baz'])

    def test_empty(self):
        self.assertEqual(rt.parse_xbps_updates(''), [])


class TestListAllUpdates(unittest.TestCase):
    def _proc(self, out='', err='', rc=0):
        p = mock.MagicMock()
        p.communicate.return_value = (out, err)
        p.returncode = rc
        return p

    def test_success(self):
        line = 'cryptsetup-2.8.8_1 update x86_64 https://r 1 2\n'
        with mock.patch.object(rt.subprocess, 'Popen', return_value=self._proc(out=line)):
            self.assertEqual(rt.list_all_updates(), ['cryptsetup'])

    def test_no_updates_is_empty_list_not_none(self):
        with mock.patch.object(rt.subprocess, 'Popen', return_value=self._proc()):
            self.assertEqual(rt.list_all_updates(), [])

    def test_nonzero_exit_is_failure(self):
        with mock.patch.object(rt.subprocess, 'Popen', return_value=self._proc(rc=1)):
            self.assertIsNone(rt.list_all_updates())

    def test_spawn_error_is_failure(self):
        with mock.patch.object(rt.subprocess, 'Popen', side_effect=OSError):
            self.assertIsNone(rt.list_all_updates())

    def test_timeout_kills_group_and_fails(self):
        p = self._proc()
        p.communicate.side_effect = rt.subprocess.TimeoutExpired('xbps-install', 60)
        with mock.patch.object(rt.subprocess, 'Popen', return_value=p), \
                mock.patch.object(rt, '_kill_process_group') as kill:
            self.assertIsNone(rt.list_all_updates())
            kill.assert_called_once_with(p)


class TestTwitchLive(unittest.TestCase):
    def _resp(self, payload):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return resp

    def test_no_channels_no_request(self):
        with mock.patch.object(rt.urllib.request, 'urlopen') as urlopen:
            self.assertEqual(rt.check_twitch_live_channels([]), {})
            urlopen.assert_not_called()

    def test_live_offline_and_missing_user(self):
        payload = [
            {'data': {'user': {'stream': {'type': 'live', 'title': 'hi'}}}},
            {'data': {'user': {'stream': None}}},
            {'data': {'user': None}},
        ]
        with mock.patch.object(rt.urllib.request, 'urlopen', return_value=self._resp(payload)):
            self.assertEqual(rt.check_twitch_live_channels(['a', 'b', 'c']), {'a': 'hi'})

    def test_nobody_live_is_empty_dict_not_none(self):
        payload = [{'data': {'user': {'stream': None}}}]
        with mock.patch.object(rt.urllib.request, 'urlopen', return_value=self._resp(payload)):
            self.assertEqual(rt.check_twitch_live_channels(['a']), {})

    def test_request_error_is_none(self):
        with mock.patch.object(rt.urllib.request, 'urlopen', side_effect=OSError):
            self.assertIsNone(rt.check_twitch_live_channels(['a']))

    def test_non_list_response_is_none(self):
        with mock.patch.object(rt.urllib.request, 'urlopen', return_value=self._resp({'error': 1})):
            self.assertIsNone(rt.check_twitch_live_channels(['a']))


if __name__ == '__main__':
    unittest.main()
