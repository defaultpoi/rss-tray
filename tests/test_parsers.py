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
        self.assertEqual(rt.entry_id({'id': 'x', 'link': 'y'}, 'u'), rt.entry_id({'id': 'x', 'link': 'z'}, 'u'))

    def test_link_fallback(self):
        self.assertEqual(rt.entry_id({'link': 'y'}, 'u'), rt.entry_id({'link': 'y', 'title': 't'}, 'u'))

    def test_title_and_published_fallback(self):
        a = rt.entry_id({'title': 't', 'published': '1'}, 'u')
        b = rt.entry_id({'title': 't', 'published': '2'}, 'u')
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
            {'data': {'user': {'stream': {'type': 'live', 'title': 'hi', 'game': {'name': 'Chess'}}}}},
            {'data': {'user': {'stream': None}}},
            {'data': {'user': None}},
        ]
        with mock.patch.object(rt.urllib.request, 'urlopen', return_value=self._resp(payload)):
            self.assertEqual(rt.check_twitch_live_channels(['a', 'b', 'c']),
                             {'a': {'title': 'hi', 'category': 'Chess'}})

    def test_live_without_game_has_empty_category(self):
        payload = [{'data': {'user': {'stream': {'type': 'live', 'title': 'hi', 'game': None}}}}]
        with mock.patch.object(rt.urllib.request, 'urlopen', return_value=self._resp(payload)):
            self.assertEqual(rt.check_twitch_live_channels(['a']),
                             {'a': {'title': 'hi', 'category': ''}})

    def test_query_requests_the_game_name(self):
        with mock.patch.object(rt.urllib.request, 'urlopen',
                               return_value=self._resp([{'data': {'user': None}}])) as urlopen:
            rt.check_twitch_live_channels(['a'])
        self.assertIn('game { name }', urlopen.call_args[0][0].data.decode())

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


class TestEntryIdFeedScoping(unittest.TestCase):
    def test_fallback_scoped_by_feed_url(self):
        entry = {'title': 't', 'published': 'd'}
        a = rt.entry_id(entry, 'https://a')
        b = rt.entry_id(entry, 'https://b')
        self.assertNotEqual(a, b)

    def test_id_or_link_present_ignores_feed_url(self):
        entry = {'id': 'x'}
        self.assertEqual(rt.entry_id(entry, 'https://a'), rt.entry_id(entry, 'https://b'))


class TestWeatherCoords(TmpConfigCase):
    @staticmethod
    def coords():
        s = rt.load_weather_settings()
        return s['lat'], s['lon']

    def test_no_section_gives_none(self):
        self.assertEqual(self.coords(), (None, None))

    def test_valid_coords_parsed(self):
        self.conf('[weather]\n40.0|-3.7\n')
        self.assertEqual(self.coords(), (40.0, -3.7))

    def test_garbage_gives_none(self):
        self.conf('[weather]\nnot-a-number|also-not\n')
        self.assertEqual(self.coords(), (None, None))

    def test_missing_second_field_gives_none(self):
        self.conf('[weather]\n40.0\n')
        self.assertEqual(self.coords(), (None, None))

    def test_fetch_weather_skips_network_without_coords(self):
        with mock.patch.object(rt.urllib.request, 'urlopen') as urlopen:
            self.assertIsNone(rt.fetch_weather())
        urlopen.assert_not_called()


class TestForecastSegments(unittest.TestCase):
    class Stub:
        build_forecast_weather_segments = rt.RssTray.build_forecast_weather_segments

    def _segments(self, weather_data):
        s = self.Stub()
        s.weather_data = weather_data
        with mock.patch.object(rt.GLib, 'markup_escape_text', side_effect=lambda x: x):
            return s.build_forecast_weather_segments()

    def test_no_data_or_no_days_is_empty(self):
        self.assertEqual(self._segments(None), [])
        self.assertEqual(self._segments({'forecast_days': []}), [])

    def test_one_segment_per_day_with_rain_glyph_only_when_rain_likely(self):
        segs = self._segments({'forecast_days': [
            {'max_temp': 21.4, 'min_temp': 9.6, 'rain_prob': 40},
            {'max_temp': 18.0, 'min_temp': 7.0, 'rain_prob': 0},
        ]})
        self.assertEqual(len(segs), 2)
        self.assertIn('☔', segs[0])
        self.assertIn('21/10°C', segs[0])
        self.assertNotIn('☔', segs[1])
        self.assertIn('18/7°C', segs[1])

    def test_day_without_any_values_is_skipped(self):
        segs = self._segments({'forecast_days': [
            {'max_temp': None, 'min_temp': None, 'rain_prob': None},
            {'max_temp': 5.0, 'min_temp': 1.0, 'rain_prob': None},
        ]})
        self.assertEqual(len(segs), 1)


class TestTodayGlyph(unittest.TestCase):
    class Stub:
        build_today_weather_segments = rt.RssTray.build_today_weather_segments
        _pulse_on_for = rt.RssTray._pulse_on_for
        _alert_pulse_counter = 0

        def __init__(self, alerts=None):
            self.weather_data = {'temp': 12.0, 'weather_code': 71}  # snow
            self._alerts = alerts or {}

        def _segment_alert_colors(self):
            return self._alerts

    def _first(self, stub):
        with mock.patch.object(rt.GLib, 'markup_escape_text', side_effect=lambda x: x):
            return stub.build_today_weather_segments()[0]

    def test_glyph_is_monochrome_dark_without_alert(self):
        seg = self._first(self.Stub())
        self.assertIn('foreground="#2b2b2b"', seg)
        self.assertIn('\u2744\ufe0e', seg)
        self.assertNotIn('\ufe0f', seg)

    def test_alert_on_glyph_uses_alert_color(self):
        seg = self._first(self.Stub({'glyph': 'red'}))
        self.assertTrue('#cc0000' in seg or '#7a1414' in seg)
        self.assertIn('\ufe0e', seg)
        self.assertNotIn('\ufe0f', seg)


class TestGlyphRise(unittest.TestCase):
    def test_cloud_is_lifted_others_are_not(self):
        self.assertGreater(rt.glyph_rise_units('\u2601'), 0)
        for g in ('\u2600', '\u2744', '\u26a1', '\u2614'):
            self.assertEqual(rt.glyph_rise_units(g), 0)

    def test_rise_scales_with_widget_font_size(self):
        widget = mock.MagicMock()
        widget.get_pango_context.return_value.get_font_description.return_value.get_size.return_value = 20 * 1024
        self.assertAlmostEqual(rt.glyph_rise_units('\u2601', widget), 2 * rt.glyph_rise_units('\u2601'), delta=2)

    def test_markup_includes_rise_only_when_nonzero(self):
        self.assertIn('rise="500"', rt.mono_glyph_markup('\u2601', rise=500))
        self.assertNotIn('rise', rt.mono_glyph_markup('\u2600'))

    def test_today_cloud_segment_is_raised(self):
        class Stub(TestTodayGlyph.Stub):
            def __init__(self):
                super().__init__()
                self.weather_data = {'temp': 12.0, 'weather_code': 3}
        with mock.patch.object(rt.GLib, 'markup_escape_text', side_effect=lambda x: x):
            seg = Stub().build_today_weather_segments()[0]
        self.assertIn('rise="', seg)


class TestRiseSpacer(unittest.TestCase):
    def test_spacer_markup(self):
        self.assertEqual(rt.rise_spacer_markup(0), '')
        m = rt.rise_spacer_markup(700)
        self.assertIn('rise="700"', m)
        self.assertIn('\u200b', m)

    def _segments(self, code):
        class Stub(TestTodayGlyph.Stub):
            def __init__(self):
                super().__init__()
                self.weather_data = {
                    'temp': 12.0, 'weather_code': code, 'today_max_temp': 15.0,
                    'today_min_temp': 4.0, 'wind': 10.0, 'today_max_wind': 20.0,
                    'today_rain_prob': 30.0, 'today_precip_sum': 1.2}
        with mock.patch.object(rt.GLib, 'markup_escape_text', side_effect=lambda x: x):
            return Stub().build_today_weather_segments()

    def test_all_values_share_the_raise_when_cloud_is_shown(self):
        segs = self._segments(3)
        self.assertEqual(len(segs), 4)
        for seg in segs:
            self.assertIn('rise="', seg)

    def test_every_value_is_padded_even_without_a_raised_glyph(self):
        for seg in self._segments(71):  # snow: its glyph needs no raise
            self.assertIn('rise="', seg)

    def test_forecast_segments_are_padded_too(self):
        class Stub:
            build_forecast_weather_segments = rt.RssTray.build_forecast_weather_segments
            weather_data = {'forecast_days': [{'max_temp': 5.0, 'min_temp': 1.0, 'rain_prob': 0}]}
        with mock.patch.object(rt.GLib, 'markup_escape_text', side_effect=lambda x: x):
            seg = Stub().build_forecast_weather_segments()[0]
        self.assertIn('rise="', seg)


class TestTodayRainText(unittest.TestCase):
    def _rain(self, prob, precip):
        class Stub(TestTodayGlyph.Stub):
            def __init__(self):
                super().__init__()
                self.weather_data = {'today_rain_prob': prob, 'today_precip_sum': precip}
        with mock.patch.object(rt.GLib, 'markup_escape_text', side_effect=lambda x: x):
            return Stub().build_today_weather_segments()[0]

    def test_zero_chance_shows_only_amount(self):
        seg = self._rain(0.0, 0.0)
        self.assertIn('0.0mm', seg)
        self.assertNotIn('%', seg)

    def test_nonzero_chance_shows_both(self):
        self.assertIn('40%/1.2mm', self._rain(40.0, 1.2))

    def test_chance_without_amount(self):
        seg = self._rain(40.0, None)
        self.assertIn('40%', seg)
        self.assertNotIn('mm', seg)

    def test_zero_chance_without_amount_keeps_percent(self):
        self.assertIn('0%', self._rain(0.0, None))


class TestLiveRowHelpers(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, rt, '_playing', rt._playing)

    def test_window_title_is_bullet_separated_and_skips_empties(self):
        self.assertEqual(rt.stream_window_title('Twitch', 'user', 'My title'), 'Twitch \u00b7 user \u00b7 My title')
        self.assertEqual(rt.stream_window_title('YouTube', 'user', None), 'YouTube \u00b7 user')
        self.assertEqual(rt.stream_window_title('YouTube', 'user', ''), 'YouTube \u00b7 user')

    def test_marker_is_dot_or_text_form_triangle(self):
        self.assertIn('\u25cf', rt.live_marker_markup('#123456', False))
        playing = rt.live_marker_markup('#123456', True)
        self.assertIn('\u25b6\ufe0e', playing)
        self.assertNotIn('\u25cf', playing)
        self.assertIn('#123456', playing)

    def test_is_stream_playing_tracks_site_channel_and_process(self):
        proc = FakeStreamlinkProc([])
        rt._playing = (proc, 'Twitch', 'chan')
        self.assertTrue(rt.is_stream_playing('Twitch', 'chan'))
        self.assertFalse(rt.is_stream_playing('YouTube', 'chan'))
        self.assertFalse(rt.is_stream_playing('Twitch', 'other'))
        proc.terminated = True  # streamlink exited -> no longer playing
        self.assertFalse(rt.is_stream_playing('Twitch', 'chan'))

    def test_nothing_playing(self):
        rt._playing = None
        self.assertFalse(rt.is_stream_playing('Twitch', 'chan'))

    def test_playing_is_recorded_after_successful_load_with_bullet_title(self):
        rt._streamlink_proc = None
        proc = FakeStreamlinkProc(['access with one of:\n'])
        titles = []
        with mock.patch.object(rt.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(rt, '_load_in_mpv', side_effect=lambda u, t: titles.append(t) or True), \
                mock.patch.object(rt.threading, 'Thread'), mock.patch.object(rt.threading, 'Timer'):
            rt._play_in_shared_mpv('twitch.tv/x', 'best', 'Twitch', 'x', 'https://twitch.tv/x', 'Stream title')
        self.assertEqual(titles, ['Twitch \u00b7 x \u00b7 Stream title'])
        self.assertTrue(rt.is_stream_playing('Twitch', 'x'))
        rt._streamlink_proc = None


class TestRunUpdateAll(unittest.TestCase):
    class Stub:
        _run_update_all = rt.RssTray._run_update_all

        def __init__(self):
            self._xbps_lock = rt.threading.Lock()
            self.statuses = []

        def _set_status(self, pkg, status):
            self.statuses.append((pkg, status))

        def _finalize_package_removal(self, pkg):
            pass

        def _end_install(self):
            pass

    def _run(self, pkgnames, versions, returncode=0):
        """versions: successive get_installed_version() results, in call order."""
        stub = self.Stub()
        proc = mock.MagicMock()
        proc.stdout = iter([])
        proc.returncode = returncode
        with mock.patch.object(rt, 'get_installed_version', side_effect=versions), \
                mock.patch.object(rt.subprocess, 'Popen', return_value=proc) as popen, \
                mock.patch.object(rt.threading, 'Timer'), \
                mock.patch.object(rt, 'PRIVILEGE_CMD', []):
            stub._run_update_all(pkgnames)
        return stub, popen

    def test_dependency_updated_by_an_earlier_package_is_done_not_failed(self):
        # start: mesa v1, libfoo v1 | mesa: before v1, after v2 | libfoo: before v2 (changed by mesa)
        stub, popen = self._run(['mesa', 'libfoo'], ['v1', 'v1', 'v1', 'v2', 'v2'])
        self.assertEqual(popen.call_count, 1)  # xbps never ran for libfoo
        self.assertIn(('mesa', 'Done'), stub.statuses)
        self.assertIn(('libfoo', 'Done'), stub.statuses)
        self.assertNotIn(('libfoo', 'Failed'), stub.statuses)

    def test_package_that_really_did_not_update_is_still_failed(self):
        # held/blocked package: xbps exits 0 but the version never changes
        stub, popen = self._run(['held'], ['v1', 'v1', 'v1'])
        self.assertIn(('held', 'Failed'), stub.statuses)

    def test_nonzero_exit_is_failed(self):
        stub, _ = self._run(['pkg'], ['v1', 'v1', 'v2'], returncode=1)
        self.assertIn(('pkg', 'Failed'), stub.statuses)

    def test_normal_update_is_done(self):
        stub, _ = self._run(['pkg'], ['v1', 'v1', 'v2'])
        self.assertIn(('pkg', 'Done'), stub.statuses)


class TestTempIconLayout(unittest.TestCase):
    def test_digits_and_ring_are_centred_together(self):
        left, cx, cy = rt.temp_icon_layout(24, 12.0, 9.0)
        ring_outer = 2 * rt.TEMP_RING_RADIUS + rt.TEMP_RING_LINE_WIDTH
        right_edge = cx + ring_outer / 2
        self.assertAlmostEqual(left, 24 - right_edge, places=6)  # equal margins either side

    def test_ring_follows_the_digits_and_sits_at_their_top(self):
        left, cx, cy = rt.temp_icon_layout(24, 12.0, 9.0)
        self.assertGreater(cx, left + 12.0)             # to the right of the digits
        digits_top = 24 / 2 - 9.0 / 2
        self.assertAlmostEqual(cy - (2 * rt.TEMP_RING_RADIUS + rt.TEMP_RING_LINE_WIDTH) / 2, digits_top, places=6)

    def test_ring_never_leaves_the_icon_top(self):
        _left, _cx, cy = rt.temp_icon_layout(24, 12.0, 40.0)
        self.assertGreaterEqual(cy - rt.TEMP_RING_RADIUS - rt.TEMP_RING_LINE_WIDTH / 2, -1e-9)


class TestDebugLogging(unittest.TestCase):
    def test_silent_by_default(self):
        with mock.patch.dict(rt.os.environ, {}, clear=False), mock.patch('builtins.print') as p:
            rt.os.environ.pop('RSS_TRAY_DEBUG', None)
            rt._log('hello')
        p.assert_not_called()

    def test_prints_when_debug_env_set(self):
        with mock.patch.dict(rt.os.environ, {'RSS_TRAY_DEBUG': '1'}), mock.patch('builtins.print') as p:
            rt._log('hello')
        self.assertIn('hello', p.call_args[0][0])


class TestPopupCss(unittest.TestCase):
    def _rules(self):
        rules = {}
        for block in rt.POPUP_CSS.split('}'):
            if '{' in block:
                selectors, body = block.split('{', 1)
                for sel in selectors.split(','):
                    rules.setdefault(sel.strip(), []).append(body.strip())
        return rules

    def test_list_area_background_uses_the_shorthand_that_resets_theme_images(self):
        rules = self._rules()
        for selector in ('list', 'viewport', 'scrolledwindow', 'overlay', '.popup-content', '.weather-bar', '.timer-bar'):
            self.assertTrue(any('background:' in body for body in rules[selector]), selector)

    def test_no_plain_background_color_left_on_those_widgets(self):
        self.assertNotIn('background-color', rt.POPUP_CSS)

    def test_list_text_is_forced_dark(self):
        rules = self._rules()
        self.assertTrue(any('color: #000000' in b for b in rules['list label']))


class TestEditFileExternally(unittest.TestCase):
    def test_default_application_via_gio_is_used_first(self):
        with mock.patch.object(rt.Gio.AppInfo, 'launch_default_for_uri', return_value=True) as launch, \
                mock.patch.object(rt.subprocess, 'Popen') as popen:
            rt.edit_file_externally('/tmp/x.conf')
        launch.assert_called_once()
        popen.assert_not_called()

    def test_falls_back_to_xdg_open_when_gio_has_no_handler(self):
        class FakeGlibError(Exception):
            message = 'no handler'
        with mock.patch.object(rt.GLib, 'Error', FakeGlibError), \
                mock.patch.object(rt.Gio.AppInfo, 'launch_default_for_uri', side_effect=FakeGlibError()), \
                mock.patch.object(rt.shutil, 'which', side_effect=lambda n: '/usr/bin/xdg-open' if n == 'xdg-open' else None), \
                mock.patch.object(rt.subprocess, 'Popen') as popen:
            rt.edit_file_externally('/tmp/x.conf')
        popen.assert_called_once_with(['xdg-open', '/tmp/x.conf'])

    def test_terminal_editor_is_the_last_resort(self):
        with mock.patch.object(rt.Gio.AppInfo, 'launch_default_for_uri', return_value=False), \
                mock.patch.object(rt.shutil, 'which', side_effect=lambda n: '/usr/bin/xfce4-terminal' if n == 'xfce4-terminal' else None), \
                mock.patch.dict(rt.os.environ, {'EDITOR': 'nvim'}), \
                mock.patch.object(rt.subprocess, 'Popen') as popen:
            rt.edit_file_externally('/tmp/x.conf')
        popen.assert_called_once_with(['xfce4-terminal', '-e', 'nvim "/tmp/x.conf"'])


class TestEmptyAreaClick(unittest.TestCase):
    class Stub:
        on_empty_area_button_press = rt.RssTray.on_empty_area_button_press

        def __init__(self):
            self.popup = mock.MagicMock()

    def test_right_click_closes_the_popup(self):
        stub = self.Stub()
        self.assertTrue(stub.on_empty_area_button_press(None, mock.Mock(button=3)))
        stub.popup.hide.assert_called_once()

    def test_other_buttons_are_left_alone(self):
        stub = self.Stub()
        self.assertFalse(stub.on_empty_area_button_press(None, mock.Mock(button=1)))
        stub.popup.hide.assert_not_called()


class TestNotificationSound(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.sound = os.path.join(d, 'notification.wav')
        patcher = mock.patch.object(rt, 'NOTIFICATION_SOUND_FILE', self.sound)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plays_the_config_dir_file_with_the_first_available_player(self):
        open(self.sound, 'wb').close()
        with mock.patch.object(rt.shutil, 'which', side_effect=lambda p: '/usr/bin/aplay' if p == 'aplay' else None), \
                mock.patch.object(rt.subprocess, 'Popen') as popen:
            rt.play_notification_sound()
        self.assertEqual(popen.call_args[0][0], ['aplay', '-q', self.sound])

    def test_silent_when_the_file_is_missing_no_system_fallback(self):
        with mock.patch.object(rt.shutil, 'which', return_value='/usr/bin/paplay'), \
                mock.patch.object(rt.subprocess, 'Popen') as popen:
            rt.play_notification_sound()
        popen.assert_not_called()

    def test_no_player_installed_is_a_quiet_noop(self):
        open(self.sound, 'wb').close()
        with mock.patch.object(rt.shutil, 'which', return_value=None), \
                mock.patch.object(rt.subprocess, 'Popen') as popen:
            rt.play_notification_sound()
        popen.assert_not_called()


class TestLauncher(TmpConfigCase):
    def test_label_and_command_in_config_order(self):
        self.conf('[launcher]\nFiles|thunar ~\nUpdate|xfce4-terminal -e "sudo xbps-install -Su"\n')
        self.assertEqual(rt.load_launcher_entries(), [
            ('Files', 'thunar ~'), ('Update', 'xfce4-terminal -e "sudo xbps-install -Su"')])

    def test_command_may_contain_pipes_and_equals(self):
        self.conf('[launcher]\nBusy|ps aux | grep -c FOO=1\n')
        self.assertEqual(rt.load_launcher_entries(), [('Busy', 'ps aux | grep -c FOO=1')])

    def test_bare_line_is_label_and_command(self):
        self.conf('[launcher]\nmousepad\n')
        self.assertEqual(rt.load_launcher_entries(), [('mousepad', 'mousepad')])

    def test_empty_commands_and_comments_are_skipped(self):
        self.conf('[launcher]\n# note\nNothing|\n\nReal|true\n')
        self.assertEqual(rt.load_launcher_entries(), [('Real', 'true')])

    def test_no_section_is_empty(self):
        self.conf('[feeds]\n')
        self.assertEqual(rt.load_launcher_entries(), [])

    def test_run_command_is_detached_through_the_shell_from_home(self):
        with mock.patch.object(rt.subprocess, 'Popen') as popen, mock.patch.object(rt.threading, 'Thread'):
            self.assertTrue(rt.run_launcher_command('thunar ~ && echo hi'))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ['sh', '-c', 'thunar ~ && echo hi'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['cwd'], rt.os.path.expanduser('~'))
        self.assertIs(kwargs['stdout'], rt.subprocess.DEVNULL)

    def test_run_command_failure_is_reported_not_raised(self):
        with mock.patch.object(rt.subprocess, 'Popen', side_effect=OSError('nope')), mock.patch.object(rt, '_log'):
            self.assertFalse(rt.run_launcher_command('x'))

    def test_popup_stays_open_while_the_launch_list_has_focus(self):
        class Stub:
            on_popup_focus_out = rt.RssTray.on_popup_focus_out
            _launcher_open = True
        win = mock.MagicMock()
        Stub().on_popup_focus_out(win, None)
        win.hide.assert_not_called()
        Stub._launcher_open = False
        Stub().on_popup_focus_out(win, None)
        win.hide.assert_called_once()


class TestConnectivityState(unittest.TestCase):
    def test_success_is_online_immediately_and_resets_failures(self):
        self.assertEqual(rt.next_connectivity(False, 5, True), (True, 0))

    def test_one_failed_probe_does_not_go_offline(self):
        self.assertEqual(rt.next_connectivity(True, 0, False), (True, 1))

    def test_consecutive_failures_go_offline_and_stay_offline(self):
        online, failures = True, 0
        for _ in range(rt.OFFLINE_AFTER_FAILED_PROBES):
            online, failures = rt.next_connectivity(online, failures, False)
        self.assertFalse(online)
        self.assertEqual(rt.next_connectivity(online, failures, False)[0], False)


class TestOfflineBehavior(unittest.TestCase):
    class Stub:
        on_row_activated = rt.RssTray.on_row_activated
        _on_probe_result = rt.RssTray._on_probe_result
        _set_online = rt.RssTray._set_online
        _offline_tooltip = rt.RssTray._offline_tooltip
        probe_connectivity = rt.RssTray.probe_connectivity

        def __init__(self, online=True):
            self.online = online
            self._probe_failures = 0
            self._probing = True
            self._confirm_pending = True   # no real timers in unit tests
            self.offline_detail = ''
            self.offline_since = None
            self.offline_banner = mock.MagicMock()
            self.start_check_thread = mock.Mock()
            self.start_weather_fetch = mock.Mock()
            self.start_twitch_check = mock.Mock()
            self.start_youtube_check = mock.Mock()
            self.removed = []
            self.updates = 0
            self.refreshes = 0

        def _remove_unread(self, item_id):
            self.removed.append(item_id)

        def update_icon(self):
            self.updates += 1

        def refresh_list(self):
            self.refreshes += 1

    def _news_row(self):
        return mock.Mock(spec=['entry_id', 'link'], entry_id='id1', link='https://example.com/a')

    def test_online_click_opens_and_marks_read(self):
        stub = self.Stub(online=True)
        with mock.patch.object(rt.webbrowser, 'open') as wb_open:
            stub.on_row_activated(None, self._news_row())
        wb_open.assert_called_once_with('https://example.com/a')
        self.assertEqual(stub.removed, ['id1'])

    def test_offline_left_click_on_a_news_item_does_nothing(self):
        stub = self.Stub(online=False)
        with mock.patch.object(rt.webbrowser, 'open') as wb_open:
            stub.on_row_activated(None, self._news_row())
        wb_open.assert_not_called()
        self.assertEqual(stub.removed, [])
        self.assertEqual((stub.updates, stub.refreshes), (0, 0))

    def test_offline_feed_header_click_does_nothing(self):
        stub = self.Stub(online=False)
        stub.mark_feed_read = mock.Mock()
        stub.on_row_activated(None, mock.Mock(spec=['header_feed_url'], header_feed_url='https://a'))
        stub.mark_feed_read.assert_not_called()

    def test_online_feed_header_click_marks_the_feed_read(self):
        stub = self.Stub(online=True)
        stub.mark_feed_read = mock.Mock()
        stub.on_row_activated(None, mock.Mock(spec=['header_feed_url'], header_feed_url='https://a'))
        stub.mark_feed_read.assert_called_once_with('https://a')

    def test_offline_install_all_header_does_nothing(self):
        stub = self.Stub(online=False)
        stub.install_all_updates = mock.Mock()
        stub.on_row_activated(None, mock.Mock(spec=['install_all_header'], install_all_header=True))
        stub.install_all_updates.assert_not_called()

    def test_online_install_all_header_installs(self):
        stub = self.Stub(online=True)
        stub.install_all_updates = mock.Mock()
        stub.on_row_activated(None, mock.Mock(spec=['install_all_header'], install_all_header=True))
        stub.install_all_updates.assert_called_once()

    def test_offline_live_channel_rows_do_nothing(self):
        stub = self.Stub(online=False)
        stub.popup = mock.MagicMock()
        with mock.patch.object(rt, 'open_twitch_stream') as twitch, mock.patch.object(rt, 'open_youtube_stream') as yt:
            stub.on_row_activated(None, mock.Mock(spec=['twitch_channel'], twitch_channel='chan'))
            stub.on_row_activated(None, mock.Mock(spec=['youtube_channel'], youtube_channel='@h'))
        twitch.assert_not_called()
        yt.assert_not_called()
        stub.popup.hide.assert_not_called()

    def test_online_live_channel_rows_play(self):
        stub = self.Stub(online=True)
        stub.popup = mock.MagicMock()
        with mock.patch.object(rt, 'open_twitch_stream') as twitch:
            stub.on_row_activated(None, mock.Mock(spec=['twitch_channel'], twitch_channel='chan'))
        twitch.assert_called_once()

    def test_offline_right_click_still_dismisses(self):
        stub = self.Stub(online=False)
        stub.on_listbox_button_press = rt.RssTray.on_listbox_button_press.__get__(stub)
        stub.on_mark_read_clicked = rt.RssTray.on_mark_read_clicked.__get__(stub)
        listbox = mock.Mock()
        listbox.get_row_at_y.return_value = self._news_row()
        event = mock.Mock(button=3, y=10.0)
        self.assertTrue(stub.on_listbox_button_press(listbox, event))
        self.assertEqual(stub.removed, ['id1'])

    @staticmethod
    def probe(ok, detail=''):
        return {'online': ok, 'cause': 'ok' if ok else 'internet', 'detail': detail}

    def test_probe_results_drive_the_banner(self):
        stub = self.Stub(online=True)
        stub._on_probe_result(self.probe(False, 'no route'))
        stub.offline_banner.set_visible.assert_not_called()   # first failure: still online
        stub._on_probe_result(self.probe(False, 'no route'))
        self.assertFalse(stub.online)
        stub.offline_banner.set_visible.assert_called_with(True)
        stub._on_probe_result(self.probe(True))
        self.assertTrue(stub.online)
        stub.offline_banner.set_visible.assert_called_with(False)

    def test_probe_does_not_overlap_itself(self):
        stub = self.Stub()
        stub._probing = True
        with mock.patch.object(rt.threading, 'Thread') as thread:
            self.assertTrue(stub.probe_connectivity())
        thread.assert_not_called()
        stub._probing = False
        with mock.patch.object(rt.threading, 'Thread') as thread:
            stub.probe_connectivity()
        thread.assert_called_once()

    def test_banner_css_uses_the_shorthand(self):
        self.assertIn('.offline-banner { background:', rt.POPUP_CSS)


class TestRunWhenOnline(unittest.TestCase):
    def test_waits_in_the_background_until_online_then_runs(self):
        done = rt.threading.Event()
        with mock.patch.object(rt, 'is_online', side_effect=[False, False, True]) as probe, \
                mock.patch.object(rt.time_module, 'sleep') as sleep:
            rt.run_when_online(done.set)
            self.assertTrue(done.wait(timeout=5))
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(rt.NETWORK_RETRY_SECONDS)

    def test_main_thread_returns_immediately(self):
        class Stub:
            initial_check = rt.RssTray.initial_check
            initial_twitch_check = rt.RssTray.initial_twitch_check
            initial_youtube_check = rt.RssTray.initial_youtube_check
            initial_weather_check = rt.RssTray.initial_weather_check
        stub = Stub()
        stub.start_check_thread = stub.start_twitch_check = stub.start_youtube_check = stub.start_weather_fetch = mock.Mock()
        with mock.patch.object(rt, 'run_when_online') as run, mock.patch.object(rt, 'is_online') as probe:
            for name in ('initial_check', 'initial_twitch_check', 'initial_youtube_check', 'initial_weather_check'):
                self.assertFalse(getattr(stub, name)())
        self.assertEqual(run.call_count, 4)
        probe.assert_not_called()  # no blocking network probe on the calling (GTK) thread


class TestReconnectCatchUp(unittest.TestCase):
    def test_coming_back_online_triggers_every_refresh_once(self):
        stub = TestOfflineBehavior.Stub(online=False)
        stub._set_online(True)
        for name in ('start_check_thread', 'start_weather_fetch', 'start_twitch_check', 'start_youtube_check'):
            getattr(stub, name).assert_called_once()

    def test_going_offline_or_staying_online_triggers_nothing(self):
        stub = TestOfflineBehavior.Stub(online=True)
        stub._set_online(True)
        stub._set_online(False)
        stub.start_check_thread.assert_not_called()
        stub.start_weather_fetch.assert_not_called()


class TestFeedCheckRobustness(TmpConfigCase):
    class Stub:
        check_feeds = rt.RssTray.check_feeds

        def __init__(self, online=True):
            self.online = online
            self.lock = rt.threading.Lock()
            self.state = {'seen': {}, 'unread': [], 'last_checked': {}}
            self.on_new_items = mock.Mock()
            self.note_request_failure = mock.Mock()

    def test_offline_unforced_check_does_nothing(self):
        self.conf('[feeds]\nhttps://a.example/feed\n')
        stub = self.Stub(online=False)
        with mock.patch.object(rt.feedparser, 'parse') as parse, mock.patch.object(rt, 'save_state'):
            stub.check_feeds()
        parse.assert_not_called()
        self.assertEqual(stub.state['last_checked'], {})

    def test_forced_check_still_runs_offline(self):
        self.conf('[feeds]\nhttps://a.example/feed\n')
        stub = self.Stub(online=False)
        parsed = mock.Mock(bozo=False, entries=[])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed) as parse, mock.patch.object(rt, 'save_state'):
            stub.check_feeds(force=True)
        parse.assert_called_once()

    def test_failed_fetch_is_retried_soon_not_after_a_full_interval(self):
        self.conf('[feeds]\nhttps://a.example/feed||30\n')  # 30 minute interval
        stub = self.Stub()
        failed = mock.Mock(bozo=True, entries=[])
        with mock.patch.object(rt.feedparser, 'parse', return_value=failed), \
                mock.patch.object(rt.time_module, 'time', return_value=1_000_000.0), \
                mock.patch.object(rt, 'save_state'):
            stub.check_feeds()
        due_again_in = 30 * 60 - (1_000_000.0 - stub.state['last_checked']['https://a.example/feed'])
        self.assertAlmostEqual(due_again_in, rt.FEED_RETRY_SECONDS, delta=1)

    def test_successful_fetch_counts_as_checked_now(self):
        self.conf('[feeds]\nhttps://a.example/feed\n')
        stub = self.Stub()
        ok = mock.Mock(bozo=False, entries=[])
        with mock.patch.object(rt.feedparser, 'parse', return_value=ok), \
                mock.patch.object(rt.time_module, 'time', return_value=1_000_000.0), \
                mock.patch.object(rt, 'save_state'):
            stub.check_feeds()
        self.assertEqual(stub.state['last_checked']['https://a.example/feed'], 1_000_000.0)


class TestPopupRebuildRules(unittest.TestCase):
    class Stub:
        show_popup = rt.RssTray.show_popup

        def __init__(self, visible):
            self.popup = mock.MagicMock()
            self.popup.get_visible.return_value = visible
            self.refresh_list = mock.Mock()
            self.probe_connectivity = mock.Mock()
            self.position_popup = mock.Mock()
            self._update_scroller_max_height = mock.Mock()
            self._launcher_open = True
            self.old_popup = self.popup

        def build_popup_window(self):
            self.popup = mock.MagicMock()

    def test_auto_show_on_a_visible_popup_only_refreshes_the_list(self):
        stub = self.Stub(visible=True)
        with mock.patch.object(rt, 'is_fullscreen_active', return_value=False):
            stub.show_popup(auto=True)
        stub.refresh_list.assert_called_once()
        stub.old_popup.destroy.assert_not_called()
        self.assertTrue(stub._launcher_open is True)

    def test_manual_show_rebuilds_and_clears_the_launch_guard(self):
        stub = self.Stub(visible=True)
        with mock.patch.object(rt, 'is_fullscreen_active', return_value=False):
            stub.show_popup()
        stub.old_popup.destroy.assert_called_once()
        self.assertFalse(stub._launcher_open)
        stub.popup.show_all.assert_called_once()

    def test_auto_show_on_a_hidden_popup_rebuilds(self):
        stub = self.Stub(visible=False)
        with mock.patch.object(rt, 'is_fullscreen_active', return_value=False):
            stub.show_popup(auto=True)
        stub.old_popup.destroy.assert_called_once()


class TestUnreadCap(TmpConfigCase):
    def test_oldest_unread_beyond_the_cap_are_dropped_but_stay_seen(self):
        self.conf('[feeds]\nhttps://a.example/feed\n')

        class Stub:
            check_feeds = rt.RssTray.check_feeds

            def __init__(self):
                self.online = True
                self.lock = rt.threading.Lock()
                old = [{'id': f'old{i}', 'title': 't', 'link': '', 'feed_url': 'u'} for i in range(rt.MAX_UNREAD_ITEMS)]
                self.state = {'seen': {}, 'unread': old, 'last_checked': {}}
                self.on_new_items = mock.Mock()
        stub = Stub()
        entries = [{'id': f'new{i}', 'title': 'x', 'link': f'l{i}'} for i in range(3)]
        parsed = mock.Mock(bozo=False, entries=entries)
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed), \
                mock.patch.object(rt, 'save_state'), mock.patch.object(rt.GLib, 'idle_add'):
            stub.check_feeds()
        unread = stub.state['unread']
        self.assertEqual(len(unread), rt.MAX_UNREAD_ITEMS)
        self.assertEqual([e['title'] for e in unread[:3]], ['x', 'x', 'x'])     # newest kept, at the front
        self.assertNotIn(f'old{rt.MAX_UNREAD_ITEMS - 1}', [e['id'] for e in unread])  # oldest dropped
        self.assertEqual(len(stub.state['seen']), 3)                             # and never resurfaces


class TestPulseRedraws(unittest.TestCase):
    class Stub:
        _alert_pulse_tick = rt.RssTray._alert_pulse_tick
        _pulse_signature = rt.RssTray._pulse_signature
        _pulse_on_for = rt.RssTray._pulse_on_for
        has_active_alerts = rt.RssTray.has_active_alerts

        def __init__(self, colors):
            self.active_alerts = [{'severity_color': c} for c in colors]
            self._alert_pulse_counter = 0
            self._last_pulse_signature = None
            self.updates = 0
            self.popup = None

        def update_icon(self):
            self.updates += 1

    def test_yellow_redraws_only_when_its_phase_flips(self):
        stub = self.Stub(['yellow'])
        for _ in range(8):
            stub._alert_pulse_tick()
        self.assertEqual(stub.updates, 3)  # ticks 1, 4 and 8, not all eight

    def test_red_flips_every_tick(self):
        stub = self.Stub(['red'])
        for _ in range(8):
            stub._alert_pulse_tick()
        self.assertEqual(stub.updates, 8)

    def test_no_alerts_no_redraws(self):
        stub = self.Stub([])
        for _ in range(8):
            stub._alert_pulse_tick()
        self.assertEqual(stub.updates, 0)


class TestTwitchBatching(unittest.TestCase):
    def test_channels_are_split_into_batches(self):
        sizes = []

        def batch(channels):
            sizes.append(len(channels))
            return {c: {'title': '', 'category': ''} for c in channels[:1]}
        with mock.patch.object(rt, '_check_twitch_batch', side_effect=batch):
            live = rt.check_twitch_live_channels([f'c{i}' for i in range(45)])
        self.assertEqual(sizes, [20, 20, 5])
        self.assertEqual(set(live), {'c0', 'c20', 'c40'})

    def test_any_failed_batch_means_no_result(self):
        with mock.patch.object(rt, '_check_twitch_batch', side_effect=[{}, None, {}]):
            self.assertIsNone(rt.check_twitch_live_channels([f'c{i}' for i in range(45)]))

    def test_no_channels_is_an_empty_dict_without_a_request(self):
        with mock.patch.object(rt, '_check_twitch_batch') as batch:
            self.assertEqual(rt.check_twitch_live_channels([]), {})
        batch.assert_not_called()


class TestConfigEncoding(TmpConfigCase):
    def test_non_utf8_bytes_do_not_crash_the_loaders(self):
        with open(rt.CONFIG_FILE, 'wb') as f:
            f.write(b'[feeds]\nhttps://a.example/feed|Caf\xe9\n')
        feeds = rt.load_feeds()
        self.assertEqual(feeds[0][0], 'https://a.example/feed')


class TestUnixSignalAdd(unittest.TestCase):
    def test_prefers_glibunix_signal_add(self):
        unix = mock.Mock(spec=['signal_add', 'signal_add_full'])
        with mock.patch.object(rt, 'GLibUnix', unix), mock.patch.object(rt.GLib, 'unix_signal_add') as old:
            rt._unix_signal_add(15, print)
        unix.signal_add.assert_called_once_with(rt.GLib.PRIORITY_DEFAULT, 15, print)
        unix.signal_add_full.assert_not_called()
        old.assert_not_called()

    def test_uses_signal_add_full_where_plain_one_is_not_exposed(self):
        unix = mock.Mock(spec=['signal_add_full'])
        with mock.patch.object(rt, 'GLibUnix', unix), mock.patch.object(rt.GLib, 'unix_signal_add') as old:
            rt._unix_signal_add(15, print)
        unix.signal_add_full.assert_called_once_with(rt.GLib.PRIORITY_DEFAULT, 15, print)
        old.assert_not_called()

    def test_falls_back_to_the_old_glib_function(self):
        with mock.patch.object(rt, 'GLibUnix', None), mock.patch.object(rt.GLib, 'unix_signal_add') as old:
            rt._unix_signal_add(15, print)
        old.assert_called_once_with(rt.GLib.PRIORITY_DEFAULT, 15, print)

    def test_namespace_without_either_function_falls_back_too(self):
        with mock.patch.object(rt, 'GLibUnix', mock.Mock(spec=[])), \
                mock.patch.object(rt.GLib, 'unix_signal_add') as old:
            rt._unix_signal_add(2, print)
        old.assert_called_once()


def ts(weekday, hour, minute=0, second=0):
    """Timestamp of a local time on the given weekday (0=Mon) of the reference week."""
    return rt.datetime(2026, 10, 5 + weekday, hour, minute, second).timestamp()


class TestScheduleLogic(unittest.TestCase):
    S = dict(rt.NEW_SCHEDULE, enabled=True, minute=30, warn=60, snooze=30, grace=120)

    def status(self, now, fired=None, override=None, **settings):
        settings = dict(self.S, **settings)
        if fired is None:  # as in the app: occurrences long past are already handled
            fired = rt._schedule_occurrences(now - 1000, settings['days'], settings['minute'])[0] or 0
        return rt.schedule_status(now, settings, fired, override)

    def test_occurrences_are_the_latest_past_and_first_future(self):
        previous, upcoming = rt._schedule_occurrences(ts(2, 12), rt._ALL_DAYS, 30)
        self.assertEqual((previous, upcoming), (ts(2, 0, 30), ts(3, 0, 30)))

    def test_weekday_filter(self):
        wed_only = frozenset({2})
        previous, upcoming = rt._schedule_occurrences(ts(0, 12), wed_only, 30)   # Monday noon
        self.assertEqual(upcoming, ts(2, 0, 30))
        self.assertEqual(previous, ts(2, 0, 30) - 7 * 86400)

    def test_far_from_the_time_is_idle(self):
        self.assertEqual(self.status(ts(1, 12))[0], 'idle')

    def test_warning_starts_warn_seconds_before_with_the_seconds_left(self):
        occurrence = ts(2, 0, 30)
        self.assertEqual(self.status(occurrence - 61)[0], 'idle')
        self.assertEqual(self.status(occurrence - 60), ('warn', occurrence, 60))
        kind, _occ, left = self.status(occurrence - 10)
        self.assertEqual((kind, left), ('warn', 10))

    def test_fires_at_the_time_and_within_the_grace(self):
        occurrence = ts(2, 0, 30)
        self.assertEqual(self.status(occurrence), ('fire', occurrence, 0))
        self.assertEqual(self.status(occurrence + 120)[0], 'fire')

    def test_missed_by_more_than_the_grace_is_skipped_not_run_late(self):
        occurrence = ts(2, 0, 30)
        self.assertEqual(self.status(occurrence + 121), ('skip', occurrence, 0))

    def test_handled_occurrence_is_not_repeated(self):
        occurrence = ts(2, 0, 30)
        self.assertEqual(self.status(occurrence + 5, fired=occurrence)[0], 'idle')

    def test_no_warning_when_warn_is_zero(self):
        occurrence = ts(2, 0, 30)
        self.assertEqual(self.status(occurrence - 5, warn=0)[0], 'idle')
        self.assertEqual(self.status(occurrence, warn=0)[0], 'fire')

    def test_days_restrict_the_runs(self):
        thursday_0030 = ts(3, 0, 30)
        self.assertEqual(self.status(thursday_0030, days=frozenset({2}))[0], 'idle')

    def test_snooze_moves_the_fire_time_for_that_occurrence_only(self):
        occurrence = ts(2, 0, 30)
        override = {'occ': occurrence, 'fire': occurrence + 30 * 60}
        done = occurrence - 86400
        self.assertEqual(self.status(occurrence + 5, fired=done, override=override)[0], 'idle')      # snoozed
        self.assertEqual(self.status(occurrence + 30 * 60 - 40, fired=done, override=override)[0], 'warn')
        self.assertEqual(self.status(occurrence + 30 * 60, fired=done, override=override)[0], 'fire')

    def test_snooze_override_counts_from_the_fire_time_or_from_now(self):
        occurrence = ts(2, 0, 30)
        done = occurrence - 86400                                                    # yesterday's run is handled
        before = rt.schedule_snooze_override(occurrence - 3600, self.S, done, None)   # pressed early
        self.assertEqual(before, {'occ': occurrence, 'fire': occurrence + 30 * 60})
        during = rt.schedule_snooze_override(occurrence - 20, self.S, done, None)     # pressed in the warning
        self.assertEqual(during['fire'], occurrence + 30 * 60)
        again = rt.schedule_snooze_override(occurrence + 100, self.S, done, during)   # pressed again later
        self.assertEqual(again['fire'], occurrence + 60 * 60)
        late = rt.schedule_snooze_override(occurrence + 3 * 3600, self.S, done, again)  # that fire time is long gone
        self.assertEqual(late['occ'], ts(3, 0, 30))                                   # so it snoozes tomorrow's run

    def test_a_stale_unhandled_occurrence_is_not_snoozed(self):
        stale = rt.schedule_snooze_override(ts(2, 12), self.S, 0, None)               # 0:30 passed 11.5 h ago, unhandled
        self.assertEqual(stale['occ'], ts(3, 0, 30))                                  # snoozes the upcoming one instead

    def test_nothing_pending_means_no_override(self):
        s = dict(self.S, days=frozenset())
        self.assertIsNone(rt.schedule_snooze_override(ts(2, 12), s, 0, None))


class TestActionsAndScheduleConfig(TmpConfigCase):
    def test_actions_default_to_suspend_and_power_off(self):
        self.assertEqual([a[0] for a in rt.load_actions()], ['Suspend', 'Power off'])
        self.assertEqual(dict(rt.load_actions())['Suspend'], 'loginctl suspend')

    def test_actions_from_config(self):
        self.conf('[actions]\nHibernate|loginctl hibernate\nLock|loginctl lock-session\n')
        self.assertEqual(rt.load_actions(), [('Hibernate', 'loginctl hibernate'), ('Lock', 'loginctl lock-session')])

    def test_format_days(self):
        self.assertEqual(rt.format_days(range(7)), 'daily')
        self.assertEqual(rt.format_days(range(5)), 'weekdays')
        self.assertEqual(rt.format_days({5, 6}), 'weekends')
        self.assertEqual(rt.format_days({0, 2, 4}), 'mon,wed,fri')
        for days in ({0, 2, 4}, {6}, set(range(7)), set(range(5))):  # round-trips through the parser
            self.assertEqual(set(rt._parse_days(rt.format_days(days))), days)

    def test_defaults_without_a_section(self):
        s = rt.load_schedule_settings()
        self.assertEqual((s['warn'], s['snooze'], s['grace'], s['schedules']), (60, 30, 120, []))

    def test_several_schedules_and_shared_settings(self):
        self.conf('[schedule]\nwarn=0\nsnooze=45 # min\ngrace=30\n'
                  'on|00:30|daily|Suspend\noff|07:00|weekdays|Lock screen\non|23.15|mon,wed|Power off\n')
        s = rt.load_schedule_settings()
        self.assertEqual((s['warn'], s['snooze'], s['grace']), (0, 45, 30))
        self.assertEqual([(e['enabled'], e['minute'], rt.format_days(e['days']), e['action']) for e in s['schedules']],
                         [(True, 30, 'daily', 'Suspend'), (False, 420, 'weekdays', 'Lock screen'),
                          (True, 23 * 60 + 15, 'mon,wed', 'Power off')])

    def test_unreadable_lines_are_skipped_and_missing_fields_defaulted(self):
        self.conf('[schedule]\nnonsense\non|25:99|daily|Suspend\non|funday|x\non|08:00\n')
        entries = rt.load_schedule_settings()['schedules']
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['minute'], entries[0]['action'], set(entries[0]['days'])), (480, 'Suspend', set(range(7))))

    def test_identical_lines_are_collapsed(self):
        self.conf('[schedule]\non|00:30|daily|Suspend\noff|00:30|daily|Suspend\n')
        self.assertEqual(len(rt.load_schedule_settings()['schedules']), 1)

    def test_original_single_schedule_keys_are_still_read(self):
        self.conf('[schedule]\nenabled=true\ntime=00:30\ndays=weekdays\naction=Power off\nwarn=20\n')
        s = rt.load_schedule_settings()
        self.assertEqual(s['warn'], 20)
        self.assertEqual(len(s['schedules']), 1)
        e = s['schedules'][0]
        self.assertEqual((e['enabled'], e['minute'], rt.format_days(e['days']), e['action']), (True, 30, 'weekdays', 'Power off'))

    def test_save_replaces_entries_keeps_comments_settings_and_other_sections_and_drops_old_keys(self):
        self.conf('[feeds]\nhttps://a\n[schedule]\n# my note\nwarn=20\nenabled=true\ntime=00:30\n'
                  'on|01:00|daily|Suspend\n[twitch]\nchan\n')
        entries = [dict(rt.NEW_SCHEDULE, enabled=True, minute=7 * 60 + 5, days=frozenset({0, 1}), action='Lock screen'),
                   dict(rt.NEW_SCHEDULE)]
        rt.save_schedules(entries)
        with open(rt.CONFIG_FILE) as f:
            text = f.read()
        self.assertIn('# my note', text)
        self.assertIn('warn=20', text)
        self.assertNotIn('enabled=true', text)
        self.assertNotIn('time=00:30', text)
        self.assertNotIn('01:00', text)
        s = rt.load_schedule_settings()
        self.assertEqual([(e['enabled'], e['minute'], rt.format_days(e['days']), e['action']) for e in s['schedules']],
                         [(True, 425, 'mon,tue', 'Lock screen'), (False, 30, 'daily', 'Suspend')])
        self.assertEqual(rt.load_twitch_channels(), ['chan'])

    def test_save_creates_the_section(self):
        self.conf('[feeds]\nhttps://a\n')
        rt.save_schedules([dict(rt.NEW_SCHEDULE, enabled=True)])
        self.assertEqual(len(rt.load_schedule_settings()['schedules']), 1)

    def test_schedule_key_changes_when_the_schedule_does(self):
        a = dict(rt.NEW_SCHEDULE)
        self.assertEqual(rt.schedule_key(a), rt.schedule_key(dict(a, enabled=True)))  # on/off keeps identity
        self.assertNotEqual(rt.schedule_key(a), rt.schedule_key(dict(a, minute=31)))
        self.assertNotEqual(rt.schedule_key(a), rt.schedule_key(dict(a, action='Power off')))


class TestScheduleRuntime(TmpConfigCase):
    class Stub:
        _schedule_settings = rt.RssTray._schedule_settings
        _prune_schedule_state = rt.RssTray._prune_schedule_state
        _schedule_state = rt.RssTray._schedule_state
        _schedule_tick = rt.RssTray._schedule_tick
        _mark_schedule_handled = rt.RssTray._mark_schedule_handled
        _skip_missed_schedule = rt.RssTray._skip_missed_schedule
        _fire_schedule = rt.RssTray._fire_schedule
        _set_schedule_warning = rt.RssTray._set_schedule_warning
        on_schedule_cancel = rt.RssTray.on_schedule_cancel
        on_schedule_snooze = rt.RssTray.on_schedule_snooze
        on_schedule_snooze_selected = rt.RssTray.on_schedule_snooze_selected
        _snooze_schedule = rt.RssTray._snooze_schedule
        _selected_schedule = rt.RssTray._selected_schedule
        _schedule_summary = rt.RssTray._schedule_summary
        _refresh_schedule_ui = rt.RssTray._refresh_schedule_ui

        def __init__(self):
            self.lock = rt.threading.Lock()
            self.state = {}
            self.schedule_warning = None
            self._schedule_flash_on = False
            self._last_schedule_tick = None
            self._schedule_cache = (None, None)
            self._schedule_resync = True
            self.timer_mode = 'countdown'
            self.timer_label = self.schedule_banner = self.sched = None
            self.sched_index = 0
            self.popup = None
            self.shown = 0
            self.icon_updates = 0

        def show_popup(self, auto=False):
            self.shown += 1

        def update_icon(self):
            self.icon_updates += 1

    NIGHT = '[schedule]\nwarn=60\nsnooze=30\ngrace=120\non|00:30|daily|Suspend\n'

    def setUp(self):
        super().setUp()
        self.conf(self.NIGHT)
        self.stub = self.Stub()
        patches = [
            mock.patch.object(rt, 'save_state'),
            mock.patch.object(rt, 'run_launcher_command'),
            mock.patch.object(rt, 'play_notification_sound'),
        ]
        self.save, self.run, self.sound = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def edit_config(self, text):
        self.conf(text)
        st = rt.os.stat(rt.CONFIG_FILE)
        rt.os.utime(rt.CONFIG_FILE, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # a visible edit

    def tick(self, now):
        with mock.patch.object(rt.time_module, 'time', return_value=now):
            self.stub._schedule_tick()

    def walk(self, start, end):
        for now in range(int(start), int(end) + 1):  # one tick per second, like the real timer
            self.tick(now)

    KEY = '0030|daily|Suspend'

    def test_runs_the_configured_action_exactly_once_at_the_time(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 90, occurrence + 30)
        self.run.assert_called_once_with('loginctl suspend')
        self.assertEqual(self.stub.state['schedule_fired'][self.KEY], occurrence)

    def test_warning_flow_sound_popup_and_seconds_left(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 90, occurrence - 55)
        self.assertEqual(self.stub.schedule_warning['left'], 55)
        self.assertEqual(self.sound.call_count, 1)    # once, when the warning starts
        self.assertEqual(self.stub.shown, 1)          # popup forced open once
        self.run.assert_not_called()

    def test_icon_flashes_every_second_during_the_warning(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 59, occurrence - 50)
        self.assertEqual(self.stub.icon_updates, 10)

    def test_cancel_skips_this_run_but_not_the_next(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 90, occurrence - 30)
        self.stub.on_schedule_cancel()
        self.assertIsNone(self.stub.schedule_warning)
        self.walk(occurrence - 29, occurrence + 300)
        self.run.assert_not_called()
        self.walk(ts(3, 0, 29), ts(3, 0, 31))
        self.run.assert_called_once()

    def test_snooze_postpones_by_the_configured_minutes_then_runs(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 90, occurrence - 30)
        with mock.patch.object(rt.time_module, 'time', return_value=occurrence - 30):
            self.stub.on_schedule_snooze()
        self.assertEqual(self.stub.state['schedule_override'][self.KEY], {'occ': occurrence, 'fire': occurrence + 1800})
        self.walk(occurrence - 29, occurrence + 1800 - 61)
        self.run.assert_not_called()
        self.walk(occurrence + 1800 - 60, occurrence + 1800 + 5)
        self.run.assert_called_once()
        self.assertNotIn(self.KEY, self.stub.state['schedule_override'])   # consumed

    def test_slide_snooze_postpones_the_selected_schedule_before_the_warning(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 7200, occurrence - 7190)
        with mock.patch.object(rt.time_module, 'time', return_value=occurrence - 7190):
            self.stub.on_schedule_snooze_selected()
        self.assertEqual(self.stub.state['schedule_override'][self.KEY]['fire'], occurrence + 1800)

    def test_a_missed_time_is_not_run_late_after_waking_from_sleep(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 200, occurrence - 100)           # running normally
        self.tick(occurrence + 60)                               # machine slept through, wakes 60 s after
        self.walk(occurrence + 61, occurrence + 130)
        self.run.assert_not_called()

    def test_starting_the_app_just_after_the_time_does_not_run_it(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence + 20, occurrence + 60)
        self.run.assert_not_called()

    def test_off_schedules_never_run(self):
        self.conf('[schedule]\noff|00:30|daily|Suspend\n')
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 90, occurrence + 30)
        self.run.assert_not_called()
        self.assertIsNone(self.stub.schedule_warning)

    def test_turning_a_schedule_on_after_its_time_passed_does_not_run_it(self):
        self.conf('[schedule]\noff|00:30|daily|Suspend\n')
        occurrence = ts(2, 0, 30)
        self.walk(occurrence + 10, occurrence + 20)
        self.edit_config('[schedule]\nwarn=60\ngrace=120\non|00:30|daily|Suspend\n')
        self.walk(occurrence + 21, occurrence + 60)
        self.run.assert_not_called()

    def test_two_schedules_each_run_their_own_action_at_their_own_time(self):
        self.edit_config('[schedule]\nwarn=0\non|00:30|daily|Suspend\non|07:00|weekdays|Power off\n')
        self.walk(ts(2, 0, 29), ts(2, 0, 31))
        self.run.assert_called_once_with('loginctl suspend')
        self.run.reset_mock()
        self.walk(ts(2, 6, 59), ts(2, 7, 1))
        self.run.assert_called_once_with('loginctl poweroff')

    def test_weekday_only_schedule_does_not_run_on_the_weekend(self):
        self.edit_config('[schedule]\nwarn=0\non|07:00|weekdays|Power off\n')
        self.walk(ts(5, 6, 59), ts(5, 7, 1))   # Saturday
        self.run.assert_not_called()
        self.walk(ts(7, 6, 59), ts(7, 7, 1))   # the following Monday
        self.run.assert_called_once()

    def test_two_schedules_at_the_same_time_both_run(self):
        self.edit_config('[schedule]\nwarn=0\non|00:30|daily|Suspend\non|00:30|mon,tue,wed|Power off\n')
        self.walk(ts(2, 0, 29), ts(2, 0, 31))
        self.assertEqual(sorted(c.args[0] for c in self.run.call_args_list), ['loginctl poweroff', 'loginctl suspend'])

    def test_the_soonest_warning_is_the_one_shown(self):
        self.edit_config('[schedule]\nwarn=60\non|00:30|daily|Suspend\non|00:31|daily|Power off\n')
        self.walk(ts(2, 0, 29, 40), ts(2, 0, 29, 45))
        self.assertEqual(self.stub.schedule_warning['action'], 'Suspend')   # 20 s away beats 80 s away
        self.assertEqual(self.sound.call_count, 1)

    def test_cancel_only_affects_the_schedule_that_was_warning(self):
        self.edit_config('[schedule]\nwarn=60\non|00:30|daily|Suspend\non|00:45|daily|Power off\n')
        self.walk(ts(2, 0, 29, 10), ts(2, 0, 29, 30))
        self.stub.on_schedule_cancel()
        self.walk(ts(2, 0, 29, 31), ts(2, 0, 31))
        self.run.assert_not_called()                                   # 00:30 cancelled
        self.walk(ts(2, 0, 44), ts(2, 0, 46))
        self.run.assert_called_once_with('loginctl poweroff')          # 00:45 untouched

    def test_state_of_removed_schedules_is_pruned(self):
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 5, occurrence + 5)
        self.assertIn(self.KEY, self.stub.state['schedule_fired'])
        self.edit_config('[schedule]\non|08:00|daily|Suspend\n')
        self.tick(occurrence + 10)
        self.assertNotIn(self.KEY, self.stub.state['schedule_fired'])

    def test_state_from_the_single_schedule_version_is_discarded(self):
        self.stub.state = {'schedule_fired': 1234.5, 'schedule_override': {'occ': 1, 'fire': 2}}
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 5, occurrence + 5)
        self.run.assert_called_once()
        self.assertIsInstance(self.stub.state['schedule_fired'], dict)

    def test_unknown_action_is_logged_and_runs_nothing(self):
        self.conf('[schedule]\nwarn=0\non|00:30|daily|Nonsense\n')
        occurrence = ts(2, 0, 30)
        with mock.patch.object(rt, '_log') as log:
            self.walk(occurrence - 5, occurrence + 5)
        self.run.assert_not_called()
        self.assertIn('Nonsense', log.call_args[0][0])

    def test_action_label_match_ignores_case(self):
        self.conf('[schedule]\nwarn=0\non|00:30|daily|power off\n')
        occurrence = ts(2, 0, 30)
        self.walk(occurrence - 5, occurrence + 5)
        self.run.assert_called_once_with('loginctl poweroff')

    def test_summary_shows_the_soonest_of_all_schedules(self):
        self.conf('[schedule]\non|07:00|daily|Power off\non|00:30|daily|Suspend\noff|01:00|daily|Suspend\n')
        with mock.patch.object(rt.time_module, 'time', return_value=ts(2, 12)):
            self.assertEqual(self.stub._schedule_summary(rt.load_schedule_settings()), 'Next: Thu 00:30 Suspend')
        self.stub.schedule_warning = {'key': 'k', 'occ': 1, 'left': 42, 'action': 'Suspend'}
        self.assertEqual(self.stub._schedule_summary(rt.load_schedule_settings()), 'Suspend in 42 s')

    def test_summary_when_everything_is_off_or_empty(self):
        self.conf('[schedule]\noff|00:30|daily|Suspend\n')
        self.assertEqual(self.stub._schedule_summary(rt.load_schedule_settings()), 'Schedule off')
        self.conf('[feeds]\n')
        self.assertEqual(self.stub._schedule_summary(rt.load_schedule_settings()), 'Schedule off')


class TestScheduleIconAndCss(unittest.TestCase):
    def test_warning_css_keeps_button_text_dark(self):
        self.assertIn('.schedule-warning button label { color: #000000', rt.POPUP_CSS)

    def test_template_config_parses_with_no_schedules_by_default(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with mock.patch.object(rt, 'CONFIG_DIR', d), mock.patch.object(rt, 'CONFIG_FILE', os.path.join(d, 'config.conf')):
            rt.ensure_config()
            s = rt.load_schedule_settings()
            self.assertEqual(s['schedules'], [])
            self.assertEqual((s['warn'], s['snooze'], s['grace']), (60, 30, 120))
            self.assertEqual([a[0] for a in rt.load_actions()], ['Suspend', 'Power off'])


class TestConnectivityDiagnosis(unittest.TestCase):
    def diagnose(self, tcp=True, dns=True, gateway='192.168.1.1', answers=True):
        with mock.patch.object(rt, '_tcp_reachable', return_value=tcp), \
                mock.patch.object(rt, '_dns_ok', return_value=dns), \
                mock.patch.object(rt, '_default_gateway', return_value=gateway), \
                mock.patch.object(rt, '_gateway_answers', return_value=answers):
            return rt.diagnose_connectivity()

    def test_everything_works(self):
        self.assertEqual(self.diagnose(), {'online': True, 'cause': 'ok', 'detail': ''})

    def test_dns_only_outage_counts_as_offline(self):
        result = self.diagnose(tcp=True, dns=False)
        self.assertFalse(result['online'])
        self.assertEqual(result['cause'], 'dns')
        self.assertIn('DNS', result['detail'])

    def test_router_answers_but_the_internet_is_down(self):
        result = self.diagnose(tcp=False, answers=True)
        self.assertEqual((result['online'], result['cause']), (False, 'internet'))
        self.assertIn('192.168.1.1', result['detail'])

    def test_router_not_answering(self):
        result = self.diagnose(tcp=False, answers=False)
        self.assertEqual(result['cause'], 'gateway')
        self.assertIn('does not answer', result['detail'])

    def test_no_default_route_means_the_link_is_down(self):
        result = self.diagnose(tcp=False, gateway=None)
        self.assertEqual(result['cause'], 'no-route')

    def test_dns_is_not_even_tried_when_nothing_is_reachable(self):
        with mock.patch.object(rt, '_tcp_reachable', return_value=False), \
                mock.patch.object(rt, '_dns_ok') as dns, \
                mock.patch.object(rt, '_default_gateway', return_value=None):
            rt.diagnose_connectivity()
        dns.assert_not_called()

    def test_is_online_follows_the_diagnosis(self):
        with mock.patch.object(rt, 'diagnose_connectivity', return_value={'online': False, 'cause': 'dns', 'detail': ''}):
            self.assertFalse(rt.is_online())


class TestConnectivityProbes(unittest.TestCase):
    ROUTE = ('Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n'
             'wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n'
             'wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n')

    def test_default_gateway_is_decoded_from_the_route_table(self):
        self.assertEqual(rt._parse_default_gateway(self.ROUTE), '192.168.1.1')

    def test_no_default_route(self):
        table = self.ROUTE.splitlines()[0] + '\n' + self.ROUTE.splitlines()[2] + '\n'
        self.assertIsNone(rt._parse_default_gateway(table))

    def test_down_default_route_is_ignored(self):
        self.assertIsNone(rt._parse_default_gateway(self.ROUTE.replace('0003', '0001', 1)))

    def test_dns_ok_when_a_hostname_resolves(self):
        with mock.patch.object(rt.socket, 'getaddrinfo', side_effect=[OSError(), [('x',)]]):
            self.assertTrue(rt._dns_ok())  # the first name failing is fine, the second resolves

    def test_dns_fails_when_nothing_resolves(self):
        with mock.patch.object(rt.socket, 'getaddrinfo', side_effect=OSError()):
            self.assertFalse(rt._dns_ok())

    def test_a_hanging_resolver_gives_up_after_the_timeout(self):
        release = rt.threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(rt.socket, 'getaddrinfo', side_effect=lambda *a, **k: release.wait(10)):
            started = rt.time_module.monotonic()
            self.assertFalse(rt._dns_ok(timeout=0.2))
        self.assertLess(rt.time_module.monotonic() - started, 2)

    def test_gateway_that_refuses_connections_is_alive(self):
        with mock.patch.object(rt.socket, 'create_connection', side_effect=ConnectionRefusedError()):
            self.assertTrue(rt._gateway_answers('192.168.1.1'))

    def test_gateway_that_times_out_everywhere_does_not_answer(self):
        with mock.patch.object(rt.socket, 'create_connection', side_effect=OSError('timed out')) as connect:
            self.assertFalse(rt._gateway_answers('192.168.1.1'))
        self.assertEqual(connect.call_count, len(rt.GATEWAY_PORTS))

    def test_tcp_probe_succeeds_on_any_target(self):
        calls = []

        def connect(addr, timeout):
            calls.append(addr)
            if len(calls) < 3:
                raise OSError()
            return mock.MagicMock()
        with mock.patch.object(rt.socket, 'create_connection', side_effect=connect):
            self.assertTrue(rt._tcp_reachable())
        self.assertEqual(len(calls), 3)


class TestImmediateProbes(unittest.TestCase):
    class Stub:
        request_probe = rt.RssTray.request_probe
        note_request_failure = rt.RssTray.note_request_failure
        _on_probe_result = rt.RssTray._on_probe_result
        _confirm_probe = rt.RssTray._confirm_probe
        _set_online = rt.RssTray._set_online
        _offline_tooltip = rt.RssTray._offline_tooltip

        def __init__(self, online=True):
            self.online = online
            self._probe_failures = 0
            self._probing = False
            self._confirm_pending = False
            self._last_probe_request = 0.0
            self.offline_detail = ''
            self.offline_since = None
            self.offline_banner = None
            self.probes = 0

        def probe_connectivity(self):
            self.probes += 1
            return True

    def test_request_failure_probes_right_away_on_the_main_loop(self):
        stub = self.Stub()
        with mock.patch.object(rt.GLib, 'idle_add') as idle:
            stub.note_request_failure()
        idle.assert_called_once_with(stub.request_probe)

    def test_failure_probes_are_throttled(self):
        stub = self.Stub()
        with mock.patch.object(rt.time_module, 'monotonic', side_effect=[100.0, 101.0, 106.0]):
            stub.request_probe()
            stub.request_probe()   # 1 s later: ignored
            stub.request_probe()   # 6 s after the first: allowed
        self.assertEqual(stub.probes, 2)

    def test_a_failed_probe_while_online_schedules_one_quick_confirmation(self):
        stub = self.Stub(online=True)
        failure = {'online': False, 'cause': 'internet', 'detail': 'x'}
        with mock.patch.object(rt.GLib, 'timeout_add_seconds') as timer:
            stub._on_probe_result(failure)
            stub._on_probe_result(failure)   # already pending: not scheduled twice
        timer.assert_called_once_with(rt.CONFIRM_PROBE_SECONDS, stub._confirm_probe)
        # (a second failure flipped it offline)
        self.assertFalse(stub.online)

    def test_confirmation_runs_a_probe_and_clears_the_pending_flag(self):
        stub = self.Stub()
        stub._confirm_pending = True
        stub._confirm_probe()
        self.assertEqual(stub.probes, 1)
        self.assertFalse(stub._confirm_pending)

    def test_no_confirmation_needed_for_a_healthy_probe(self):
        stub = self.Stub()
        with mock.patch.object(rt.GLib, 'timeout_add_seconds') as timer:
            stub._on_probe_result({'online': True, 'cause': 'ok', 'detail': ''})
        timer.assert_not_called()

    def test_tooltip_shows_the_cause_and_since_when(self):
        stub = self.Stub(online=True)
        stub.offline_banner = mock.MagicMock()
        stub.offline_detail = 'DNS lookups fail'
        with mock.patch.object(rt.time_module, 'time', return_value=rt.datetime(2026, 10, 5, 3, 14).timestamp()), \
                mock.patch.object(rt, '_log') as log:
            stub._set_online(False)
        text = stub.offline_banner.set_tooltip_text.call_args[0][0]
        self.assertIn('DNS lookups fail', text)
        self.assertIn('03:14', text)
        self.assertIn('DNS lookups fail', log.call_args[0][0])   # also in the debug log

    def test_tooltip_is_cleared_when_back_online(self):
        stub = self.Stub(online=False)
        stub.offline_banner = mock.MagicMock()
        for name in ('start_check_thread', 'start_weather_fetch', 'start_twitch_check', 'start_youtube_check'):
            setattr(stub, name, mock.Mock())
        with mock.patch.object(rt, '_log'):
            stub._set_online(True)
        stub.offline_banner.set_tooltip_text.assert_called_with(None)
        self.assertIsNone(stub.offline_since)


class TestFailureTriggers(TmpConfigCase):
    def test_a_failed_twitch_check_asks_for_a_probe(self):
        self.conf('[twitch]\nchan\n')

        class Stub:
            _check_twitch_guarded = rt.RssTray._check_twitch_guarded

            def __init__(self):
                self.lock = rt.threading.Lock()
                self.state = {}
                self._twitch_lock = rt.threading.Lock()
                self._twitch_lock.acquire()
                self.note_request_failure = mock.Mock()
                self._on_twitch_checked = mock.Mock()
        stub = Stub()
        with mock.patch.object(rt, 'check_twitch_live_channels', return_value=None):
            stub._check_twitch_guarded()
        stub.note_request_failure.assert_called_once()

    def test_a_successful_twitch_check_does_not(self):
        self.conf('[twitch]\nchan\n')

        class Stub:
            _check_twitch_guarded = rt.RssTray._check_twitch_guarded

            def __init__(self):
                self.lock = rt.threading.Lock()
                self.state = {}
                self._twitch_lock = rt.threading.Lock()
                self._twitch_lock.acquire()
                self.note_request_failure = mock.Mock()
                self._on_twitch_checked = mock.Mock()
        stub = Stub()
        with mock.patch.object(rt, 'check_twitch_live_channels', return_value={}), mock.patch.object(rt.GLib, 'idle_add'):
            stub._check_twitch_guarded()
        stub.note_request_failure.assert_not_called()

    def test_weather_failure_only_counts_when_weather_is_configured(self):
        class Stub:
            _fetch_weather_bg = rt.RssTray._fetch_weather_bg

            def __init__(self):
                self.note_request_failure = mock.Mock()
                self._fetch_alerts_bg = lambda: []
                self._on_weather_fetched = mock.Mock()
        self.conf('[weather]\n10.0|20.0\n')
        configured = Stub()
        with mock.patch.object(rt, 'fetch_weather', return_value=None), mock.patch.object(rt.GLib, 'idle_add'):
            configured._fetch_weather_bg()
        configured.note_request_failure.assert_called_once()
        self.conf('[feeds]\n')
        unconfigured = Stub()
        with mock.patch.object(rt, 'fetch_weather', return_value=None), mock.patch.object(rt.GLib, 'idle_add'):
            unconfigured._fetch_weather_bg()
        unconfigured.note_request_failure.assert_not_called()


class TestNetworkReset(TmpConfigCase):
    def test_reset_command_from_config(self):
        self.assertEqual(rt.load_network_reset_command(), '')
        self.conf('[network]\nreset = ~/.local/bin/reset-network.sh --force\n')
        self.assertEqual(rt.load_network_reset_command(), '~/.local/bin/reset-network.sh --force')

    def test_click_runs_the_command_and_locks_the_button_for_a_while(self):
        self.conf('[network]\nreset=reset-network.sh\n')

        class Stub:
            on_network_reset_clicked = rt.RssTray.on_network_reset_clicked
            _end_network_reset = rt.RssTray._end_network_reset
            _probe_after_reset = rt.RssTray._probe_after_reset

            def __init__(self):
                self.reset_btn = mock.MagicMock()
                self.offline_banner_label = mock.MagicMock()
                self.probe_connectivity = mock.Mock()
        stub = Stub()
        with mock.patch.object(rt, 'run_launcher_command') as run, mock.patch.object(rt.GLib, 'timeout_add_seconds') as timer:
            stub.on_network_reset_clicked(None)
        run.assert_called_once_with('reset-network.sh')
        stub.reset_btn.set_sensitive.assert_called_with(False)
        self.assertIn('Resetting', stub.offline_banner_label.set_text.call_args[0][0])
        self.assertEqual({c.args[0] for c in timer.call_args_list}, {6, rt.NETWORK_RESET_COOLDOWN_SECONDS})
        stub._end_network_reset()
        stub.reset_btn.set_sensitive.assert_called_with(True)
        stub.offline_banner_label.set_text.assert_called_with(rt.OFFLINE_BANNER_TEXT)

    def test_without_a_command_the_click_does_nothing(self):
        class Stub:
            on_network_reset_clicked = rt.RssTray.on_network_reset_clicked
        with mock.patch.object(rt, 'run_launcher_command') as run:
            Stub().on_network_reset_clicked(None)
        run.assert_not_called()

    def test_offline_banner_css_keeps_button_text_dark(self):
        self.assertIn('.offline-banner button label { color: #000000', rt.POPUP_CSS)


class TestMonoGlyph(unittest.TestCase):
    def test_markup(self):
        m = rt.mono_glyph_markup('\u2614')
        self.assertIn('\u2614\ufe0e', m)
        self.assertIn('foreground="#2b2b2b"', m)
        self.assertIn('font_family=', m)

    def test_forecast_umbrella_is_monochrome(self):
        class Stub:
            build_forecast_weather_segments = rt.RssTray.build_forecast_weather_segments
            weather_data = {'forecast_days': [{'max_temp': 5.0, 'min_temp': 1.0, 'rain_prob': 50}]}
        with mock.patch.object(rt.GLib, 'markup_escape_text', side_effect=lambda x: x):
            seg = Stub().build_forecast_weather_segments()[0]
        self.assertIn('\u2614\ufe0e', seg)


class TestIntervalDefaults(TmpConfigCase):
    def test_weather_refresh_interval(self):
        self.assertEqual(rt.load_weather_refresh_interval(), 30 * 60)
        self.conf('[weather]\n10.0|20.0\ninterval=10\n')
        self.assertEqual(rt.load_weather_refresh_interval(), 600)
        self.conf('[weather]\ninterval=1\n')
        self.assertEqual(rt.load_weather_refresh_interval(), rt.MIN_WEATHER_REFRESH_MINUTES * 60)
        self.assertIsNone(rt.load_weather_settings()['lat'])  # the setting isn't mistaken for coordinates

    def test_weather_interval_does_not_disturb_the_other_weather_settings(self):
        self.conf('[weather]\n10.0|20.0\ninterval=10\nalerts=true\n')
        s = rt.load_weather_settings()
        self.assertEqual((s['lat'], s['lon'], s['alerts_enabled']), (10.0, 20.0, True))

    def test_documented_defaults(self):
        self.assertEqual(rt.load_twitch_check_interval(), 15 * 60)
        self.assertEqual(rt.load_youtube_check_interval(), 15 * 60)
        self.assertEqual(rt.load_updates_check_interval(), 12 * 3600)  # twice a day

    def test_updates_interval_is_in_hours_with_a_one_hour_floor(self):
        self.conf('[updates]\ninterval=6\n')
        self.assertEqual(rt.load_updates_check_interval(), 6 * 3600)
        self.conf('[updates]\ninterval=0\n')
        self.assertEqual(rt.load_updates_check_interval(), 3600)
        self.conf('[updates]\ninterval=soon\n')
        self.assertEqual(rt.load_updates_check_interval(), 12 * 3600)


class TestUpdatesDue(TmpConfigCase):
    class Stub:
        check_updates_if_due = rt.RssTray.check_updates_if_due

        def __init__(self, last_checked):
            self.lock = rt.threading.Lock()
            self._xbps_lock = rt.threading.Lock()
            self.state = {'updates_last_checked': last_checked}

    def _scans(self, last_checked_ago_hours, force=False):
        now = 1_000_000_000.0
        stub = self.Stub(now - last_checked_ago_hours * 3600)
        with mock.patch.object(rt.time_module, 'time', return_value=now), \
                mock.patch.object(rt, 'list_all_updates', return_value=None) as scan, \
                mock.patch.object(rt, 'save_state'):
            stub.check_updates_if_due(force=force)
        return scan.call_count

    def test_default_is_twice_a_day(self):
        self.assertEqual(self._scans(11.9), 0)
        self.assertEqual(self._scans(12.1), 1)

    def test_configured_interval_is_used(self):
        self.conf('[updates]\ninterval=24\n')
        self.assertEqual(self._scans(13), 0)
        self.assertEqual(self._scans(24.1), 1)

    def test_force_ignores_the_interval(self):
        self.assertEqual(self._scans(0.1, force=True), 1)


class TestLiveCheckIntervals(TmpConfigCase):
    def test_defaults_without_setting(self):
        self.conf('[twitch]\nchan\n[youtube]\n@h\n')
        self.assertEqual(rt.load_twitch_check_interval(), rt.TWITCH_CHECK_INTERVAL_SECONDS)
        self.assertEqual(rt.load_youtube_check_interval(), rt.YOUTUBE_CHECK_INTERVAL_SECONDS)

    def test_defaults_without_config_file(self):
        self.assertEqual(rt.load_twitch_check_interval(), rt.TWITCH_CHECK_INTERVAL_SECONDS)

    def test_minutes_are_converted_to_seconds(self):
        self.conf('[twitch]\ninterval=10\nchan\n[youtube]\ninterval = 15 # note\n@h\n')
        self.assertEqual(rt.load_twitch_check_interval(), 600)
        self.assertEqual(rt.load_youtube_check_interval(), 900)

    def test_low_values_are_raised_to_the_minimum(self):
        self.conf('[twitch]\ninterval=0\n[youtube]\ninterval=1\n')
        self.assertEqual(rt.load_twitch_check_interval(), rt.MIN_TWITCH_CHECK_MINUTES * 60)
        self.assertEqual(rt.load_youtube_check_interval(), rt.MIN_YOUTUBE_CHECK_MINUTES * 60)

    def test_garbage_falls_back_to_default(self):
        self.conf('[twitch]\ninterval=often\n')
        self.assertEqual(rt.load_twitch_check_interval(), rt.TWITCH_CHECK_INTERVAL_SECONDS)

    def test_setting_is_per_section(self):
        self.conf('[twitch]\ninterval=7\n')
        self.assertEqual(rt.load_twitch_check_interval(), 420)
        self.assertEqual(rt.load_youtube_check_interval(), rt.YOUTUBE_CHECK_INTERVAL_SECONDS)

    def test_interval_lines_are_not_channels(self):
        self.conf('[twitch]\ninterval=10\nchan|720p60\n[youtube]\ninterval=20\n@h|480p\n')
        self.assertEqual(rt.load_twitch_channels(), ['chan'])
        self.assertEqual(rt.load_twitch_qualities(), {'chan': '720p60'})
        self.assertEqual(rt.load_youtube_channels(), ['@h'])
        self.assertEqual(rt.load_youtube_qualities(), {'@h': '480p'})


def dt(weekday, hour, minute=0):
    """A datetime on the given weekday (0=Mon) of a fixed reference week."""
    return rt.datetime(2026, 10, 5 + weekday, hour, minute)  # 2026-10-05 is a Monday


class TestParseSchedule(unittest.TestCase):
    def test_single_day_with_range(self):
        (days, start, end), = rt.parse_schedule('wed 18:00-23:30')
        self.assertEqual((set(days), start, end), ({2}, 18 * 60, 23 * 60 + 30))

    def test_dot_separator_and_full_day_names(self):
        (days, start, end), = rt.parse_schedule('Wednesday 18.00-20.15')
        self.assertEqual((set(days), start, end), ({2}, 18 * 60, 20 * 60 + 15))

    def test_open_end_means_until_midnight(self):
        for text in ('tue-sun 13:00-', 'tue-sun 13:00'):
            (days, start, end), = rt.parse_schedule(text)
            self.assertEqual((set(days), start, end), ({1, 2, 3, 4, 5, 6}, 13 * 60, 24 * 60))

    def test_day_lists_ranges_and_keywords(self):
        self.assertEqual(set(rt.parse_schedule('mon,wed,fri 10:00')[0][0]), {0, 2, 4})
        self.assertEqual(set(rt.parse_schedule('fri-mon 10:00')[0][0]), {4, 5, 6, 0})  # wraps
        self.assertEqual(set(rt.parse_schedule('weekdays 10:00')[0][0]), {0, 1, 2, 3, 4})
        self.assertEqual(set(rt.parse_schedule('weekends 10:00')[0][0]), {5, 6})
        self.assertEqual(set(rt.parse_schedule('daily 10:00')[0][0]), set(range(7)))
        self.assertEqual(set(rt.parse_schedule('10:00-12:00')[0][0]), set(range(7)))  # no days

    def test_spaces_inside_a_day_list(self):
        self.assertEqual(set(rt.parse_schedule('mon, wed 10:00')[0][0]), {0, 2})

    def test_days_only_means_the_whole_day(self):
        (days, start, end), = rt.parse_schedule('tue-sun')
        self.assertEqual((set(days), start, end), ({1, 2, 3, 4, 5, 6}, 0, 24 * 60))

    def test_several_windows(self):
        windows = rt.parse_schedule('wed 18:00-20:00; sat 10:00-12:00')
        self.assertEqual([set(w[0]) for w in windows], [{2}, {5}])

    def test_invalid_input_raises(self):
        for bad in ('', ';', 'funday 10:00', 'wed 25:00', 'wed 10:75', 'wed 10:00-99:00',
                    'wed,,thu 10:00', 'wed 10', 'mo 10:00'):
            with self.assertRaises(ValueError, msg=bad):
                rt.parse_schedule(bad)


class TestScheduleActive(unittest.TestCase):
    def test_wednesday_evening_only(self):
        w = rt.parse_schedule('wed 18:00-23:00')
        self.assertTrue(rt.schedule_active(w, dt(2, 18, 0)))
        self.assertTrue(rt.schedule_active(w, dt(2, 22, 59)))
        self.assertFalse(rt.schedule_active(w, dt(2, 17, 59)))
        self.assertFalse(rt.schedule_active(w, dt(2, 23, 0)))   # end is exclusive
        self.assertFalse(rt.schedule_active(w, dt(3, 18, 30)))  # Thursday

    def test_every_day_except_monday_from_13(self):
        w = rt.parse_schedule('tue-sun 13:00-')
        self.assertFalse(rt.schedule_active(w, dt(0, 15)))      # Monday
        self.assertFalse(rt.schedule_active(w, dt(1, 12, 59)))
        for day in range(1, 7):
            self.assertTrue(rt.schedule_active(w, dt(day, 13)))
            self.assertTrue(rt.schedule_active(w, dt(day, 23, 59)))

    def test_window_past_midnight_spills_into_the_next_day(self):
        w = rt.parse_schedule('fri 22:00-02:00')
        self.assertTrue(rt.schedule_active(w, dt(4, 23)))
        self.assertTrue(rt.schedule_active(w, dt(5, 1, 59)))    # Saturday early morning
        self.assertFalse(rt.schedule_active(w, dt(5, 2, 0)))
        self.assertFalse(rt.schedule_active(w, dt(4, 21)))
        self.assertFalse(rt.schedule_active(w, dt(6, 1)))       # Sunday early: no Saturday window

    def test_sunday_window_past_midnight_wraps_to_monday(self):
        w = rt.parse_schedule('sun 23:00-01:00')
        self.assertTrue(rt.schedule_active(w, dt(0, 0, 30)))

    def test_any_of_several_windows(self):
        w = rt.parse_schedule('wed 18:00-19:00; sat 10:00-11:00')
        self.assertTrue(rt.schedule_active(w, dt(5, 10, 30)))
        self.assertFalse(rt.schedule_active(w, dt(5, 18, 30)))


class TestChannelsDue(unittest.TestCase):
    def test_unscheduled_always_scheduled_only_in_window_live_always(self):
        schedules = {'weekly': rt.parse_schedule('wed 18:00-23:00'),
                     'daily': rt.parse_schedule('tue-sun 13:00-')}
        channels = ['plain', 'weekly', 'daily']
        self.assertEqual(rt.channels_due(channels, schedules, set(), dt(2, 19)), ['plain', 'weekly', 'daily'])
        self.assertEqual(rt.channels_due(channels, schedules, set(), dt(0, 19)), ['plain'])
        self.assertEqual(rt.channels_due(channels, schedules, {'weekly'}, dt(0, 19)), ['plain', 'weekly'])

    def test_scheduled_active_channels(self):
        schedules = {'weekly': rt.parse_schedule('wed 18:00-23:00')}
        self.assertEqual(rt.scheduled_active_channels(schedules, dt(2, 18)), {'weekly'})
        self.assertEqual(rt.scheduled_active_channels(schedules, dt(2, 12)), set())


class TestScheduleConfig(TmpConfigCase):
    def test_schedule_is_the_third_field_and_quality_may_be_empty(self):
        self.conf('[twitch]\nplain\nweekly||wed 18:00-23:00\nboth|720p60|tue-sun 13:00-; sat 10:00-12:00\n'
                  '[youtube]\n@Handle|480p|mon 20:00-\n')
        self.assertEqual(rt.load_twitch_channels(), ['plain', 'weekly', 'both'])
        self.assertEqual(rt.load_twitch_qualities(), {'both': '720p60'})
        schedules = rt.load_twitch_schedules()
        self.assertEqual(set(schedules), {'weekly', 'both'})
        self.assertEqual(len(schedules['both']), 2)
        self.assertEqual(rt.load_youtube_channels(), ['@Handle'])
        self.assertEqual(rt.load_youtube_qualities(), {'@Handle': '480p'})
        self.assertEqual(set(rt.load_youtube_schedules()), {'@Handle'})

    def test_invalid_schedule_means_always_checked(self):
        self.conf('[twitch]\nbroken||someday 99:99\nfine||wed 18:00\n')
        self.assertEqual(set(rt.load_twitch_schedules()), {'fine'})
        self.assertEqual(rt.channels_due(rt.load_twitch_channels(), rt.load_twitch_schedules(), set(), dt(0, 3)),
                         ['broken'])

    def test_first_line_wins_for_duplicates(self):
        self.conf('[twitch]\nChan|720p60\nchan|480p||wed 18:00\n')
        self.assertEqual(rt.load_twitch_channels(), ['chan'])
        self.assertEqual(rt.load_twitch_qualities(), {'chan': '720p60'})
        self.assertEqual(rt.load_twitch_schedules(), {})


class TestLiveScheduleTick(unittest.TestCase):
    class Stub:
        _live_schedule_tick = rt.RssTray._live_schedule_tick
        _current_scheduled_active = staticmethod(rt.RssTray._current_scheduled_active)

        def __init__(self, before):
            self._scheduled_active = before
            self.twitch_checks = 0
            self.youtube_checks = 0

        def start_twitch_check(self):
            self.twitch_checks += 1

        def start_youtube_check(self):
            self.youtube_checks += 1

    def _tick(self, before, now_active):
        stub = self.Stub(before)
        with mock.patch.object(rt.RssTray, '_current_scheduled_active', staticmethod(lambda now=None: now_active)):
            stub._current_scheduled_active = rt.RssTray._current_scheduled_active
            self.assertTrue(stub._live_schedule_tick())
        return stub

    def test_window_opening_triggers_one_immediate_check_for_that_platform(self):
        stub = self._tick({'twitch': set(), 'youtube': set()}, {'twitch': {'weekly'}, 'youtube': set()})
        self.assertEqual((stub.twitch_checks, stub.youtube_checks), (1, 0))
        self.assertEqual(stub._scheduled_active['twitch'], {'weekly'})

    def test_staying_inside_or_leaving_a_window_does_not_trigger(self):
        stub = self._tick({'twitch': {'weekly'}, 'youtube': {'yt'}}, {'twitch': {'weekly'}, 'youtube': set()})
        self.assertEqual((stub.twitch_checks, stub.youtube_checks), (0, 0))


class TestWeatherSettings(TmpConfigCase):
    def test_defaults(self):
        s = rt.load_weather_settings()
        self.assertIsNone(s['lat'])
        self.assertIsNone(s['lon'])
        self.assertFalse(s['alerts_enabled'])
        self.assertIsNone(s['country'])
        self.assertIsNone(s['region'])

    def test_full_section(self):
        self.conf('[weather]\n40.0|-3.7\nalerts=true\ncountry=Exampleland\nregion=Northshire\n')
        s = rt.load_weather_settings()
        self.assertEqual((s['lat'], s['lon']), (40.0, -3.7))
        self.assertTrue(s['alerts_enabled'])
        self.assertEqual(s['country'], 'Exampleland')
        self.assertEqual(s['region'], 'Northshire')

    def test_alerts_case_insensitive_and_variants(self):
        for val in ('True', 'YES', '1', 'on'):
            self.conf(f'[weather]\nalerts={val}\n')
            self.assertTrue(rt.load_weather_settings()['alerts_enabled'])
        for val in ('false', '0', 'no', ''):
            self.conf(f'[weather]\nalerts={val}\n')
            self.assertFalse(rt.load_weather_settings()['alerts_enabled'])


class TestUpdateWeatherRegionCache(TmpConfigCase):
    def test_round_trip_preserves_other_content(self):
        self.conf(
            '[feeds]\nhttps://a\n'
            '\n[weather]\n45.0|26.0\nalerts=true\n# a comment\n'
            '\n[twitch]\nsomechannel\n'
        )
        rt._update_weather_region_cache('Northshire', 'Exampleland')
        s = rt.load_weather_settings()
        self.assertEqual((s['country'], s['region']), ('Exampleland', 'Northshire'))
        self.assertEqual((s['lat'], s['lon']), (45.0, 26.0))
        self.assertTrue(s['alerts_enabled'])
        self.assertEqual(rt.load_twitch_channels(), ['somechannel'])
        self.assertEqual([f[0] for f in rt.load_feeds()], ['https://a'])

    def test_overwrites_previous_region_values(self):
        self.conf('[weather]\n45.0|26.0\nregion=Old\n')
        rt._update_weather_region_cache('New', 'Newland')
        s = rt.load_weather_settings()
        self.assertEqual((s['country'], s['region']), ('Newland', 'New'))

    def test_country_written_and_replaced(self):
        self.conf('[weather]\n45.0|26.0\ncountry=Old\nregion=Old\n')
        rt._update_weather_region_cache('New', 'Newland')
        s = rt.load_weather_settings()
        self.assertEqual((s['country'], s['region']), ('Newland', 'New'))
        with open(rt.CONFIG_FILE) as f:
            self.assertEqual(f.read().count('country='), 1)

    def test_creates_section_if_missing(self):
        self.conf('[feeds]\nhttps://a\n')
        rt._update_weather_region_cache('Northshire', 'Exampleland')
        self.assertEqual(rt.load_weather_settings()['region'], 'Northshire')


class TestReverseGeocodeRegion(unittest.TestCase):
    def _resp(self, payload):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return resp

    def _geocode(self, payload):
        with mock.patch.object(rt.urllib.request, 'urlopen', return_value=self._resp(payload)):
            return rt.reverse_geocode_region(10.0, 20.0)

    def test_county_and_country_code(self):
        self.assertEqual(
            self._geocode({'address': {'country': 'Exampleland', 'county': 'Northshire', 'country_code': 'xx'}}),
            ('Exampleland', 'Northshire', 'XX'))

    def test_state_used_when_no_county(self):
        self.assertEqual(self._geocode({'address': {'country': 'Exampleland', 'state': 'Westland'}}),
                         ('Exampleland', 'Westland', None))

    def test_missing_region_still_returns_country(self):
        self.assertEqual(self._geocode({'address': {'country': 'Exampleland', 'country_code': 'xx'}}),
                         ('Exampleland', None, 'XX'))

    def test_missing_country_is_none(self):
        self.assertIsNone(self._geocode({'address': {'county': 'Northshire'}}))
        self.assertIsNone(self._geocode({'address': {}}))

    def test_request_error_is_none(self):
        with mock.patch.object(rt.urllib.request, 'urlopen', side_effect=OSError):
            self.assertIsNone(rt.reverse_geocode_region(10.0, 20.0))


class TestDisableWeatherAlerts(TmpConfigCase):
    def test_flips_alerts_and_drops_cache_keeping_rest(self):
        self.conf('[feeds]\nhttps://a\n\n[weather]\n10.0|20.0\nalerts=true\n# note\n'
                  'country=X\nregion=Y\n\n[twitch]\nsomechannel\n')
        rt._disable_weather_alerts()
        s = rt.load_weather_settings()
        self.assertFalse(s['alerts_enabled'])
        self.assertEqual((s['lat'], s['lon']), (10.0, 20.0))
        self.assertIsNone(s['country'])
        self.assertIsNone(s['region'])
        self.assertEqual(rt.load_twitch_channels(), ['somechannel'])
        with open(rt.CONFIG_FILE) as f:
            self.assertIn('# note', f.read())

    def test_adds_alerts_false_when_line_missing(self):
        self.conf('[weather]\n10.0|20.0\n')
        rt._disable_weather_alerts()
        self.assertFalse(rt.load_weather_settings()['alerts_enabled'])
        with open(rt.CONFIG_FILE) as f:
            self.assertIn('alerts=false', f.read())


class TestFetchAlertsBg(TmpConfigCase):
    class Stub:
        _fetch_alerts_bg = rt.RssTray._fetch_alerts_bg

    def test_non_meteoalarm_country_disables_alerts_in_config(self):
        self.conf('[weather]\n10.0|20.0\nalerts=true\n')
        with mock.patch.object(rt, 'reverse_geocode_region', return_value=('Elsewhere', 'Somestate', 'ZZ')), \
                mock.patch.object(rt, 'fetch_meteoalarm_alerts') as fetch:
            self.assertEqual(self.Stub()._fetch_alerts_bg(), [])
        fetch.assert_not_called()
        self.assertFalse(rt.load_weather_settings()['alerts_enabled'])

    def test_covered_country_caches_location_and_fetches(self):
        self.conf('[weather]\n10.0|20.0\nalerts=true\n')
        with mock.patch.object(rt, 'reverse_geocode_region', return_value=('Exampleland', 'Northshire', 'RO')), \
                mock.patch.object(rt, 'fetch_meteoalarm_alerts', return_value=[]) as fetch:
            self.assertEqual(self.Stub()._fetch_alerts_bg(), [])
        fetch.assert_called_once_with('Northshire', 'Exampleland')
        s = rt.load_weather_settings()
        self.assertTrue(s['alerts_enabled'])
        self.assertEqual((s['country'], s['region']), ('Exampleland', 'Northshire'))

    def test_geocode_failure_with_nothing_cached_is_none(self):
        self.conf('[weather]\n10.0|20.0\nalerts=true\n')
        with mock.patch.object(rt, 'reverse_geocode_region', return_value=None):
            self.assertIsNone(self.Stub()._fetch_alerts_bg())
        self.assertTrue(rt.load_weather_settings()['alerts_enabled'])

    def test_cached_location_is_never_looked_up_again(self):
        self.conf('[weather]\n10.0|20.0\nalerts=true\ncountry=Exampleland\nregion=Northshire\n')
        with mock.patch.object(rt, 'reverse_geocode_region') as geocode, \
                mock.patch.object(rt, 'fetch_meteoalarm_alerts', return_value=[]) as fetch:
            self.Stub()._fetch_alerts_bg()
        geocode.assert_not_called()
        fetch.assert_called_once_with('Northshire', 'Exampleland')

    def test_lookup_runs_again_once_the_values_are_deleted(self):
        self.conf('[weather]\n10.0|20.0\nalerts=true\ncountry=Exampleland\n')  # region missing
        with mock.patch.object(rt, 'reverse_geocode_region', return_value=('Exampleland', 'Eastshire', 'RO')) as geocode, \
                mock.patch.object(rt, 'fetch_meteoalarm_alerts', return_value=[]):
            self.Stub()._fetch_alerts_bg()
        geocode.assert_called_once()
        self.assertEqual(rt.load_weather_settings()['region'], 'Eastshire')

    def test_unknown_country_code_does_not_disable(self):
        self.conf('[weather]\n10.0|20.0\nalerts=true\n')
        with mock.patch.object(rt, 'reverse_geocode_region', return_value=('Exampleland', 'Northshire', None)), \
                mock.patch.object(rt, 'fetch_meteoalarm_alerts', return_value=[]):
            self.Stub()._fetch_alerts_bg()
        self.assertTrue(rt.load_weather_settings()['alerts_enabled'])


class TestMeteoalarmFeedUrl(unittest.TestCase):
    def test_simple_country(self):
        self.assertEqual(rt.meteoalarm_feed_url('Exampleland'),
                         'https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-exampleland')

    def test_multi_word_country_is_slugged(self):
        self.assertTrue(rt.meteoalarm_feed_url('United  Example Kingdom').endswith('-united-example-kingdom'))


class TestMeteoalarmTitleParsing(unittest.TestCase):
    def test_parse_title(self):
        self.assertEqual(
            rt.parse_meteoalarm_title('Yellow Wind Warning issued for Exampleland - Eastshire'),
            ('Wind', 'Eastshire'),
        )

    def test_parse_title_multiword_hazard(self):
        self.assertEqual(
            rt.parse_meteoalarm_title('Orange High Temperature Warning issued for Exampleland - Northshire'),
            ('High Temperature', 'Northshire'),
        )

    def test_parse_title_no_match(self):
        self.assertEqual(rt.parse_meteoalarm_title('not a real title'), (None, None))

    def test_hazard_segment_mapping(self):
        self.assertEqual(rt._hazard_to_segment('Wind'), 'wind')
        self.assertEqual(rt._hazard_to_segment('High Temperature'), 'hilo')
        self.assertEqual(rt._hazard_to_segment('Low Temperature'), 'hilo')
        self.assertEqual(rt._hazard_to_segment('Snow/Ice'), 'glyph')
        self.assertEqual(rt._hazard_to_segment('Rain'), 'rain')
        self.assertEqual(rt._hazard_to_segment('Thunderstorm'), 'rain')
        self.assertEqual(rt._hazard_to_segment('Coastal Event'), 'rain')
        self.assertIsNone(rt._hazard_to_segment('Fog'))
        self.assertIsNone(rt._hazard_to_segment('Forest Fire'))


class TestFetchMeteoalarmAlerts(unittest.TestCase):
    def _entry(self, area, title, expires='2099-01-01T00:00:00+00:00'):
        return {'cap_areadesc': area, 'title': title, 'cap_expires': expires}

    def test_filters_by_region_case_insensitive(self):
        parsed = mock.MagicMock(bozo=False, entries=[
            self._entry('Eastshire', 'Yellow Wind Warning issued for Exampleland - Eastshire'),
            self._entry('northshire', 'Orange Rain Warning issued for Exampleland - Northshire'),
        ])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed):
            alerts = rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland')
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['hazard'], 'Rain')
        self.assertEqual(alerts[0]['segment'], 'rain')

    def test_no_match_is_empty_list(self):
        parsed = mock.MagicMock(bozo=False, entries=[
            self._entry('Eastshire', 'Yellow Wind Warning issued for Exampleland - Eastshire'),
        ])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed):
            self.assertEqual(rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland'), [])

    def test_bozo_with_no_entries_is_failure(self):
        parsed = mock.MagicMock(bozo=True, entries=[])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed):
            self.assertIsNone(rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland'))

    def test_uses_country_feed_url(self):
        parsed = mock.MagicMock(bozo=False, entries=[])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed) as parse:
            rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland')
        parse.assert_called_once_with(rt.meteoalarm_feed_url('Exampleland'))

    def test_parse_exception_is_failure(self):
        with mock.patch.object(rt.feedparser, 'parse', side_effect=Exception):
            self.assertIsNone(rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland'))


class TestAlertSeverityAndTiming(unittest.TestCase):
    def test_severity_mapping(self):
        self.assertEqual(rt._severity_to_color('Moderate'), 'yellow')
        self.assertEqual(rt._severity_to_color('Severe'), 'orange')
        self.assertEqual(rt._severity_to_color('Extreme'), 'red')
        self.assertEqual(rt._severity_to_color('Minor'), 'yellow')
        self.assertEqual(rt._severity_to_color(None), 'yellow')

    def _entry(self, effective, onset, expires, severity='Moderate',
               title='Yellow Wind Warning issued for Exampleland - Northshire', area='Northshire'):
        return {
            'cap_areadesc': area, 'title': title, 'cap_effective': effective,
            'cap_onset': onset, 'cap_expires': expires, 'cap_severity': severity,
        }

    def test_expired_entry_filtered_out(self):
        now = rt.datetime(2026, 9, 29, 19, 14, 58, tzinfo=rt.timezone.utc)
        parsed = mock.MagicMock(bozo=False, entries=[
            self._entry('2026-09-28T06:53:00+00:00', '2026-09-28T07:00:00+00:00',
                         '2026-09-28T17:00:00+00:00'),
        ])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed):
            self.assertEqual(rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland', now=now), [])

    def test_future_entry_filtered_out(self):
        now = rt.datetime(2026, 9, 29, 19, 14, 58, tzinfo=rt.timezone.utc)
        parsed = mock.MagicMock(bozo=False, entries=[
            self._entry('2026-09-29T18:00:00+00:00', '2026-09-30T08:00:00+00:00',
                         '2026-09-30T20:00:00+00:00', severity='Extreme'),
        ])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed):
            self.assertEqual(rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland', now=now), [])

    def test_currently_active_entry_kept_with_severity_color(self):
        now = rt.datetime(2026, 9, 29, 19, 14, 58, tzinfo=rt.timezone.utc)
        parsed = mock.MagicMock(bozo=False, entries=[
            self._entry('2026-09-29T06:00:00+00:00', '2026-09-29T10:00:00+00:00',
                         '2026-09-30T10:00:00+00:00', severity='Severe',
                         title='Orange Rain Warning issued for Exampleland - Northshire'),
        ])
        with mock.patch.object(rt.feedparser, 'parse', return_value=parsed):
            alerts = rt.fetch_meteoalarm_alerts('Northshire', 'Exampleland', now=now)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['severity_color'], 'orange')

    def test_pulse_speed_ratios(self):
        class Stub:
            _pulse_on_for = rt.RssTray._pulse_on_for
        s = Stub()
        def flips(color):
            states = []
            for i in range(16):
                s._alert_pulse_counter = i
                states.append(s._pulse_on_for(color))
            return sum(1 for i in range(1, 16) if states[i] != states[i - 1])
        self.assertEqual(flips('red'), 15)
        self.assertIn(flips('orange'), (7, 8))
        self.assertIn(flips('yellow'), (3, 4))


class TestTwitchQuality(TmpConfigCase):
    def test_channels_still_just_names(self):
        self.conf('[twitch]\nfoo\nbar|720p60\n')
        self.assertEqual(rt.load_twitch_channels(), ['foo', 'bar'])

    def test_qualities_parsed(self):
        self.conf('[twitch]\nfoo\nbar|720p60\nbaz|1080p60\n')
        self.assertEqual(rt.load_twitch_qualities(), {'bar': '720p60', 'baz': '1080p60'})

    def test_channel_without_quality_absent_from_qualities(self):
        self.conf('[twitch]\nfoo\n')
        self.assertEqual(rt.load_twitch_qualities(), {})


class FakeStreamlinkProc:
    def __init__(self, output_lines):
        self.stdout = iter(output_lines)
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class TestOpenLiveStream(unittest.TestCase):
    def _which_ok(self):
        return mock.patch.object(rt.shutil, 'which', side_effect=lambda x: '/usr/bin/' + x)

    def test_twitch_starts_worker_thread_with_url_and_quality(self):
        with self._which_ok(), mock.patch.object(rt.threading, 'Thread') as thread, \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt.open_twitch_stream('somechan', '720p60')
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs['target'], rt._play_in_shared_mpv)
        self.assertEqual(thread.call_args.kwargs['args'], (
            'twitch.tv/somechan', '720p60', 'Twitch', 'somechan', 'https://twitch.tv/somechan', None))
        thread.return_value.start.assert_called_once()
        wb_open.assert_not_called()

    def test_twitch_defaults_to_best_quality(self):
        with self._which_ok(), mock.patch.object(rt.threading, 'Thread') as thread, \
                mock.patch.object(rt.webbrowser, 'open'):
            rt.open_twitch_stream('somechan')
        self.assertEqual(thread.call_args.kwargs['args'][1], 'best')

    def test_twitch_falls_back_to_browser_when_streamlink_missing(self):
        with mock.patch.object(rt.shutil, 'which', return_value=None), \
                mock.patch.object(rt.threading, 'Thread') as thread, \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt.open_twitch_stream('somechan')
        thread.assert_not_called()
        wb_open.assert_called_once_with('https://twitch.tv/somechan')

    def test_twitch_falls_back_to_browser_when_thread_start_raises(self):
        with self._which_ok(), mock.patch.object(rt.threading, 'Thread', side_effect=RuntimeError), \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt.open_twitch_stream('somechan')
        wb_open.assert_called_once_with('https://twitch.tv/somechan')

    def test_youtube_starts_worker_thread(self):
        with self._which_ok(), mock.patch.object(rt.threading, 'Thread') as thread, \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt.open_youtube_stream('@somehandle', '720p60')
        self.assertEqual(thread.call_args.kwargs['args'], (
            'https://www.youtube.com/@somehandle/live', '720p60', 'YouTube',
            '@somehandle', 'https://www.youtube.com/@somehandle', None))
        wb_open.assert_not_called()

    def test_youtube_falls_back_to_channel_page_when_mpv_missing(self):
        with mock.patch.object(rt.shutil, 'which', side_effect=lambda x: None if x == 'mpv' else '/x'), \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt.open_youtube_stream('@somehandle')
        wb_open.assert_called_once_with('https://www.youtube.com/@somehandle')


class TestSharedMpv(unittest.TestCase):
    def setUp(self):
        rt._streamlink_proc = None
        self.addCleanup(setattr, rt, '_streamlink_proc', None)

    def test_streamlink_serves_locally_then_loads_into_mpv(self):
        proc = FakeStreamlinkProc(['[cli][info] Starting server, access with one of:\n', ' http://127.0.0.1:1/\n'])
        with mock.patch.object(rt.subprocess, 'Popen', return_value=proc) as popen, \
                mock.patch.object(rt, '_load_in_mpv', return_value=True) as load, \
                mock.patch.object(rt.threading, 'Thread'), \
                mock.patch.object(rt.threading, 'Timer'), \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt._play_in_shared_mpv('twitch.tv/x', '720p60', 'Twitch', 'x', 'https://twitch.tv/x')
        args = popen.call_args[0][0]
        self.assertEqual(args[:5], ['streamlink', '--player-external-http',
                                    '--player-external-http-interface', '127.0.0.1',
                                    '--player-external-http-port'])
        self.assertEqual(args[6:], ['twitch.tv/x', '720p60'])
        # streamlink's log goes to stdout or stderr depending on version: merge them
        self.assertIs(popen.call_args.kwargs['stderr'], rt.subprocess.STDOUT)
        self.assertIs(popen.call_args.kwargs['stdout'], rt.subprocess.PIPE)
        load.assert_called_once_with('http://127.0.0.1:%s/' % args[5], 'Twitch \u00b7 x')
        wb_open.assert_not_called()
        self.assertFalse(proc.terminated)

    def test_never_ready_failure_is_logged_with_last_output(self):
        proc = FakeStreamlinkProc(['error: boom\n'])
        with mock.patch.object(rt.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(rt.webbrowser, 'open'), \
                mock.patch.object(rt, '_log') as log:
            rt._play_in_shared_mpv('twitch.tv/x', 'best', 'Twitch', 'x', 'https://twitch.tv/x')
        self.assertIn('error: boom', log.call_args[0][0])

    def test_browser_fallback_when_streamlink_never_ready(self):
        proc = FakeStreamlinkProc(['error: no plugin\n'])
        with mock.patch.object(rt.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(rt, '_load_in_mpv') as load, \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt._play_in_shared_mpv('twitch.tv/x', 'best', 'Twitch', 'x', 'https://twitch.tv/x')
        load.assert_not_called()
        wb_open.assert_called_once_with('https://twitch.tv/x')
        self.assertTrue(proc.terminated)

    def test_browser_fallback_when_mpv_load_fails(self):
        proc = FakeStreamlinkProc(['access with one of:\n'])
        with mock.patch.object(rt.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(rt, '_load_in_mpv', return_value=False), \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt._play_in_shared_mpv('twitch.tv/x', 'best', 'Twitch', 'x', 'https://twitch.tv/x')
        wb_open.assert_called_once_with('https://twitch.tv/x')

    def test_browser_fallback_when_streamlink_launch_raises(self):
        with mock.patch.object(rt.subprocess, 'Popen', side_effect=OSError), \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt._play_in_shared_mpv('twitch.tv/x', 'best', 'Twitch', 'x', 'https://twitch.tv/x')
        wb_open.assert_called_once_with('https://twitch.tv/x')

    def test_new_click_stops_previous_stream_only_after_mpv_took_the_new_one(self):
        old = FakeStreamlinkProc([])
        rt._streamlink_proc = old
        new = FakeStreamlinkProc(['access with one of:\n'])
        seen = {}

        def load(url, title):
            seen['old_alive_during_load'] = not old.terminated
            return True
        with mock.patch.object(rt.subprocess, 'Popen', return_value=new), \
                mock.patch.object(rt, '_load_in_mpv', side_effect=load), \
                mock.patch.object(rt.threading, 'Thread'), \
                mock.patch.object(rt.threading, 'Timer'):
            rt._play_in_shared_mpv('twitch.tv/y', 'best', 'Twitch', 'y', 'https://twitch.tv/y')
        self.assertTrue(seen['old_alive_during_load'])  # else mpv would quit at EOF
        self.assertTrue(old.terminated)
        self.assertFalse(new.terminated)

    def test_failed_new_click_leaves_previous_stream_playing(self):
        old = FakeStreamlinkProc([])
        rt._streamlink_proc = old
        new = FakeStreamlinkProc(['no marker\n'])
        with mock.patch.object(rt.subprocess, 'Popen', return_value=new), \
                mock.patch.object(rt.webbrowser, 'open'), mock.patch.object(rt, '_log'):
            rt._play_in_shared_mpv('twitch.tv/y', 'best', 'Twitch', 'y', 'https://twitch.tv/y')
        self.assertFalse(old.terminated)
        self.assertTrue(new.terminated)
        self.assertIs(rt._streamlink_proc, old)

    def test_superseded_failure_does_not_open_browser(self):
        def lines():
            rt._streamlink_proc = FakeStreamlinkProc([])  # a newer click took over
            yield 'no marker\n'
        proc = FakeStreamlinkProc([])
        proc.stdout = lines()
        with mock.patch.object(rt.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(rt.webbrowser, 'open') as wb_open:
            rt._play_in_shared_mpv('twitch.tv/x', 'best', 'Twitch', 'x', 'https://twitch.tv/x')
        wb_open.assert_not_called()

    def test_load_in_mpv_sends_title_then_loadfile(self):
        with mock.patch.object(rt, '_ensure_mpv', return_value=True), \
                mock.patch.object(rt, '_mpv_ipc', return_value=True) as ipc:
            self.assertTrue(rt._load_in_mpv('http://127.0.0.1:9/', 'T'))
        ipc.assert_called_once_with([['set_property', 'force-media-title', 'T'],
                                     ['loadfile', 'http://127.0.0.1:9/', 'replace']])

    def test_load_in_mpv_fails_when_mpv_cannot_start(self):
        with mock.patch.object(rt, '_ensure_mpv', return_value=False):
            self.assertFalse(rt._load_in_mpv('u', 'T'))

    def test_load_in_mpv_retries_once(self):
        with mock.patch.object(rt, '_ensure_mpv', return_value=True), \
                mock.patch.object(rt, '_mpv_ipc', side_effect=[False, True]), \
                mock.patch.object(rt.time_module, 'sleep'):
            self.assertTrue(rt._load_in_mpv('u', 'T'))

    def test_ensure_mpv_reuses_running_instance(self):
        with mock.patch.object(rt, '_mpv_ipc', return_value=True), \
                mock.patch.object(rt.subprocess, 'Popen') as popen:
            self.assertTrue(rt._ensure_mpv())
        popen.assert_not_called()

    def test_ensure_mpv_launches_maximized_idle_with_ipc(self):
        mpv = mock.Mock()
        mpv.poll.return_value = None
        with mock.patch.object(rt, '_mpv_ipc', side_effect=[False, True]), \
                mock.patch.object(rt.subprocess, 'Popen', return_value=mpv) as popen, \
                mock.patch.object(rt.time_module, 'sleep'):
            self.assertTrue(rt._ensure_mpv())
        args = popen.call_args[0][0]
        self.assertEqual(args[0], 'mpv')
        self.assertIn('--window-maximized=yes', args)
        self.assertIn('--idle=once', args)
        self.assertIn(f'--input-ipc-server={rt.MPV_IPC_SOCKET}', args)

    def test_ensure_mpv_false_when_mpv_dies_at_startup(self):
        mpv = mock.Mock()
        mpv.poll.return_value = 1
        with mock.patch.object(rt, '_mpv_ipc', return_value=False), \
                mock.patch.object(rt.subprocess, 'Popen', return_value=mpv):
            self.assertFalse(rt._ensure_mpv())


class TestMpvIpc(unittest.TestCase):
    """Runs _mpv_ipc against a real unix socket standing in for mpv."""

    def _serve(self, replies):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'mpv.sock')
        self.received = []
        server = rt.socket.socket(rt.socket.AF_UNIX, rt.socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            conn, _ = server.accept()
            buf = b''
            for reply in replies:
                while b'\n' not in buf:
                    buf += conn.recv(4096)
                line, buf = buf.split(b'\n', 1)
                self.received.append(json.loads(line))
                conn.sendall(b'{"event":"noise"}\n' + (json.dumps(reply) + '\n').encode())
            conn.close()
        rt.threading.Thread(target=run, daemon=True).start()
        patcher = mock.patch.object(rt, 'MPV_IPC_SOCKET', path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_replies(self):
        self._serve([{'error': 'success'}, {'error': 'success'}])
        self.assertTrue(rt._mpv_ipc([['set_property', 'a', 'b'], ['loadfile', 'u', 'replace']]))
        self.assertEqual(self.received[1], {'command': ['loadfile', 'u', 'replace']})

    def test_error_reply_is_false(self):
        self._serve([{'error': 'property unavailable'}])
        self.assertFalse(rt._mpv_ipc([['get_property', 'x']]))

    def test_no_socket_is_false(self):
        with mock.patch.object(rt, 'MPV_IPC_SOCKET', '/nonexistent/none.sock'):
            self.assertFalse(rt._mpv_ipc([['get_property', 'pid']]))


class TestTimerSettings(TmpConfigCase):
    def test_defaults(self):
        s = rt.load_timer_settings()
        self.assertEqual(s['max_seconds'], rt.DEFAULT_TIMER_MAX_MINUTES * 60)
        self.assertEqual(s['step_seconds'], rt.DEFAULT_TIMER_STEP_SECONDS)

    def test_custom_values(self):
        self.conf('[timer]\nmax=90\nstep=5\n')
        s = rt.load_timer_settings()
        self.assertEqual(s['max_seconds'], 90 * 60)
        self.assertEqual(s['step_seconds'], 5)

    def test_inline_comments_ignored(self):
        self.conf('[timer]\nmax=120 #minutes\nstep=5 #seconds\n')
        s = rt.load_timer_settings()
        self.assertEqual(s['max_seconds'], 120 * 60)
        self.assertEqual(s['step_seconds'], 5)

    def test_bad_values_fall_back_to_defaults(self):
        self.conf('[timer]\nmax=abc\nstep=-5\n')
        s = rt.load_timer_settings()
        self.assertEqual(s['max_seconds'], rt.DEFAULT_TIMER_MAX_MINUTES * 60)
        self.assertEqual(s['step_seconds'], rt.DEFAULT_TIMER_STEP_SECONDS)

    def test_partial_section_keeps_other_default(self):
        self.conf('[timer]\nmax=30\n')
        s = rt.load_timer_settings()
        self.assertEqual(s['max_seconds'], 30 * 60)
        self.assertEqual(s['step_seconds'], rt.DEFAULT_TIMER_STEP_SECONDS)


class TestYoutubeChannelsAndQualities(TmpConfigCase):
    def test_channels_case_preserved_dedup_case_insensitive(self):
        self.conf('[youtube]\n@SomeHandle\nchannel/UCxxxx|720p60\n@somehandle\n')
        self.assertEqual(rt.load_youtube_channels(), ['@SomeHandle', 'channel/UCxxxx'])

    def test_qualities_keyed_case_sensitive(self):
        self.conf('[youtube]\n@SomeHandle|1080p60\nchannel/UCxxxx|720p60\n')
        self.assertEqual(rt.load_youtube_qualities(),
                          {'@SomeHandle': '1080p60', 'channel/UCxxxx': '720p60'})

    def test_channel_without_quality_absent_from_qualities(self):
        self.conf('[youtube]\n@foo\n')
        self.assertEqual(rt.load_youtube_qualities(), {})


class TestCheckYoutubeLiveChannels(unittest.TestCase):
    def _fake_run(self, live_substring='live_channel', offline_substring='offline_channel'):
        def run(cmd, capture_output, text, timeout):
            url = cmd[-1]
            result = mock.MagicMock()
            if live_substring in url:
                result.stdout = json.dumps({'metadata': {'id': 'x', 'author': 'A',
                                                           'category': 'Gaming', 'title': 'Live now!'},
                                            'streams': {'best': {}}})
            elif offline_substring in url:
                result.stdout = json.dumps({'error': 'No playable streams found'})
            else:
                raise OSError('boom')
            return result
        return run

    def test_empty_channels_no_subprocess(self):
        with mock.patch.object(rt.subprocess, 'run') as run:
            self.assertEqual(rt.check_youtube_live_channels([]), {})
            run.assert_not_called()

    def test_live_and_offline_distinguished(self):
        with mock.patch.object(rt.subprocess, 'run', side_effect=self._fake_run()):
            live = rt.check_youtube_live_channels(['live_channel', 'offline_channel'])
        self.assertEqual(live, {'live_channel': {'title': 'Live now!', 'category': 'Gaming'}})

    def test_total_failure_returns_none(self):
        with mock.patch.object(rt.subprocess, 'run', side_effect=OSError('no streamlink')):
            self.assertIsNone(rt.check_youtube_live_channels(['a', 'b']))

    def test_partial_failure_keeps_successful_results(self):
        def run(cmd, capture_output, text, timeout):
            if 'bad' in cmd[-1]:
                raise OSError('network blip')
            result = mock.MagicMock()
            result.stdout = json.dumps({'error': 'No playable streams found'})
            return result
        with mock.patch.object(rt.subprocess, 'run', side_effect=run):
            result = rt.check_youtube_live_channels(['bad', 'good'])
        self.assertEqual(result, {})  # good determined not-live; bad simply absent, not a crash

    def test_live_when_streams_present_even_without_title(self):
        def run(cmd, capture_output, text, timeout):
            result = mock.MagicMock()
            result.stdout = json.dumps({'metadata': {'title': None}, 'streams': {'best': {}}})
            return result
        with mock.patch.object(rt.subprocess, 'run', side_effect=run), mock.patch.object(rt, '_log'):
            self.assertEqual(rt.check_youtube_live_channels(['@chan']),
                             {'@chan': {'title': '', 'category': ''}})

    def test_log_lines_before_json_are_tolerated(self):
        def run(cmd, capture_output, text, timeout):
            result = mock.MagicMock()
            result.stdout = '[cli][info] hello\n' + json.dumps({'metadata': {'title': 'T'}, 'streams': {'best': {}}})
            return result
        with mock.patch.object(rt.subprocess, 'run', side_effect=run):
            self.assertEqual(rt.check_youtube_live_channels(['@chan']),
                             {'@chan': {'title': 'T', 'category': ''}})

    def test_errors_are_logged(self):
        def run(cmd, capture_output, text, timeout):
            result = mock.MagicMock()
            result.stdout = json.dumps({'error': 'Unable to open URL: 403 Forbidden'})
            return result
        with mock.patch.object(rt.subprocess, 'run', side_effect=run), mock.patch.object(rt, '_log') as log:
            self.assertEqual(rt.check_youtube_live_channels(['@chan']), {})
        self.assertIn('403 Forbidden', log.call_args[0][0])

    def test_unparseable_json_treated_as_this_channel_failed(self):
        def run(cmd, capture_output, text, timeout):
            result = mock.MagicMock()
            result.stdout = 'not json'
            return result
        with mock.patch.object(rt.subprocess, 'run', side_effect=run):
            self.assertIsNone(rt.check_youtube_live_channels(['a']))


class TestUpdateScanVsInstall(TestUpdatesDue):
    def test_scan_is_skipped_while_an_install_holds_the_xbps_lock(self):
        stub = self.Stub(0)
        stub._xbps_lock.acquire()
        with mock.patch.object(rt.time_module, 'time', return_value=1_000_000_000.0), \
                mock.patch.object(rt, 'list_all_updates') as scan, mock.patch.object(rt, 'save_state'):
            stub.check_updates_if_due(force=True)
        scan.assert_not_called()
        self.assertEqual(stub.state.get('updates_fail_count', 0), 0)  # skipped, not failed

    def test_lock_is_released_after_a_scan(self):
        stub = self.Stub(0)
        with mock.patch.object(rt.time_module, 'time', return_value=1_000_000_000.0), \
                mock.patch.object(rt, 'list_all_updates', return_value=[]), mock.patch.object(rt, 'save_state'):
            stub.check_updates_if_due(force=True)
        self.assertTrue(stub._xbps_lock.acquire(blocking=False))


class TestInstallStatusUpdates(TestRunUpdateAll):
    def test_repeated_phases_are_not_reported_again(self):
        stub = self.Stub()
        proc = mock.MagicMock()
        proc.stdout = iter(['[*] Downloading packages\n', 'file line\n', '[*] Collecting package files\n',
                            '[*] Unpacking packages\n', '[*] Configuring unpacked packages\n'])
        proc.returncode = 0
        with mock.patch.object(rt, 'get_installed_version', side_effect=['v1', 'v1', 'v2']), \
                mock.patch.object(rt.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(rt.threading, 'Timer'), mock.patch.object(rt, 'PRIVILEGE_CMD', []):
            stub._run_update_all(['pkg'])
        statuses = [s for p, s in stub.statuses]
        self.assertEqual(statuses, ['Installing…', 'Downloading…', 'Installing…', 'Done'])


if __name__ == '__main__':
    unittest.main()
