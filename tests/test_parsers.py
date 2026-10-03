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
        for selector in ('list', 'viewport', 'scrolledwindow', 'overlay', '.weather-bar', '.timer-bar'):
            self.assertTrue(any('background:' in body for body in rules[selector]), selector)

    def test_no_plain_background_color_left_on_those_widgets(self):
        self.assertNotIn('background-color', rt.POPUP_CSS)

    def test_list_text_is_forced_dark(self):
        rules = self._rules()
        self.assertTrue(any('color: #000000' in b for b in rules['list label']))


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


if __name__ == '__main__':
    unittest.main()
