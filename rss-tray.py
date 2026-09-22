#!/usr/bin/env python3
"""Minimal tray RSS/Atom reader with system-wide Void package-update detection
and Twitch live-channel notifications."""
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, Pango
try:
    gi.require_version('Wnck', '3.0')
    from gi.repository import Wnck
except Exception:
    Wnck = None  # fullscreen detection just no-ops if this isn't available
import cairo
import feedparser
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import webbrowser
import hashlib
import calendar
import time as time_module
import urllib.request

socket.setdefaulttimeout(15)  # avoid feed fetches hanging indefinitely on slow/broken servers

CONFIG_DIR = os.path.expanduser('~/.config/rss-tray')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.conf')
STATE_FILE = os.path.join(CONFIG_DIR, 'state.json')
CHECK_INTERVAL = 600  # default per-feed interval (seconds) when none is set in config.conf
SCHEDULER_TICK_SECONDS = 60  # how often we check whether any feed is due
PENDING_CHECK_INTERVAL_SECONDS = 3600  # how often to check for system-wide package updates
TWITCH_CHECK_INTERVAL_SECONDS = 1800  # how often to poll Twitch live status
NETWORK_RETRY_SECONDS = 10  # how often to recheck connectivity if offline at startup
MAX_LIST_ITEMS = 40
MAX_TITLE_LEN = 60
MAX_ITEM_AGE_SECONDS = 24 * 3600  # ignore entries older than this on first sight
SEEN_RETENTION_SECONDS = 30 * 24 * 3600  # prune seen-item records older than this
PRIVILEGE_CMD = ['sudo']  # change to ['doas'] if that's what you use; requires
                          # passwordless (NOPASSWD) rules for xbps-install, since
                          # updates run headlessly with no terminal/tty attached
WINDOW_WIDTH = 456  # 380 * 1.2
UPDATE_TIMEOUT_SECONDS = 1800  # 30 minutes
NOTIFICATION_SOUND_CANDIDATES = [
    os.path.join(CONFIG_DIR, 'notification.wav'),  # QuiteRSS's notification sound, if present
    '/usr/share/sounds/alsa/Front_Center.wav',      # fallback if the above is missing
]
WEATHER_LATITUDE = 45.361698
WEATHER_LONGITUDE = 26.775255
WEATHER_API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={WEATHER_LATITUDE}&longitude={WEATHER_LONGITUDE}"
    "&current=temperature_2m,wind_speed_10m,weather_code"
    "&hourly=wind_speed_10m"
    "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum"
    "&forecast_days=6&timezone=auto"
)
WEATHER_REFRESH_SECONDS = 1800  # 30 minutes
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's own public web-client ID —
                                                          # used by twitch.tv itself for logged-out
                                                          # visitors. Unofficial/undocumented; no
                                                          # app registration or secret needed.


def ensure_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        return
    legacy_feeds = os.path.join(CONFIG_DIR, 'feeds.conf')
    legacy_mute = os.path.join(CONFIG_DIR, 'mute.conf')
    if os.path.exists(legacy_feeds) or os.path.exists(legacy_mute):
        _migrate_legacy_config(legacy_feeds, legacy_mute)
        return
    with open(CONFIG_FILE, 'w') as f:
        f.write(
            "[feeds]\n"
            "# Format: URL|custom display name (optional)|check interval in minutes (optional)\n"
            "# All fields after the URL are optional but positional — leave a field empty\n"
            "# to skip it while still setting a later one, e.g. URL||5\n"
            "# https://example.com/feed.xml\n"
            "# https://example.com/feed.xml|My Blog\n"
            "# https://example.com/feed.xml|My Blog|5\n"
            "\n"
            "[mute]\n"
            "# Items whose title contains any of these phrases (case-insensitive,\n"
            "# substring match) are auto-marked as read and never shown as unread.\n"
            "# One phrase per line, or several separated by | on the same line.\n"
            "# Example:\n"
            "# (P)|Fashion week|Another item\n"
            "\n"
            "[twitch]\n"
            "# Twitch channel login names to watch for live status, one per line.\n"
            "# Uses Twitch's own internal (unofficial) API — no account/app needed.\n"
            "# Clicking a live channel runs: streamlink --player mpv twitch.tv/<name> best\n"
            "# examplechannel\n"
        )


def _migrate_legacy_config(legacy_feeds, legacy_mute):
    """One-time merge of the old separate feeds.conf/mute.conf into the new
    consolidated config.conf. The legacy files are left in place, untouched."""
    lines = ['[feeds]']
    if os.path.exists(legacy_feeds):
        with open(legacy_feeds) as f:
            for line in f:
                line = line.rstrip('\n')
                if line.strip() and not line.strip().startswith('#'):
                    lines.append(line)
    lines.append('')
    lines.append('[mute]')
    if os.path.exists(legacy_mute):
        with open(legacy_mute) as f:
            for line in f:
                line = line.rstrip('\n')
                if line.strip() and not line.strip().startswith('#'):
                    lines.append(line)
    lines.append('')
    lines.append('[twitch]')
    lines.append('# Twitch channel login names to watch for live status, one per line.')
    with open(CONFIG_FILE, 'w') as f:
        f.write('\n'.join(lines) + '\n')


def _read_config_sections():
    """Parses config.conf into {'feeds': [...], 'mute': [...], 'twitch': [...]},
    each a list of raw non-comment, non-empty lines under that [section]."""
    sections = {'feeds': [], 'mute': [], 'twitch': []}
    current = None
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('[') and line.endswith(']'):
                    current = line[1:-1].strip().lower()
                    continue
                if current in sections:
                    sections[current].append(line)
    return sections


def load_feeds():
    """Returns list of (url, custom_name, interval_seconds)."""
    feeds = []
    for line in _read_config_sections()['feeds']:
        parts = [p.strip() for p in line.split('|')]
        url = parts[0]
        custom_name = parts[1] if len(parts) > 1 and parts[1] else None
        interval_seconds = CHECK_INTERVAL
        if len(parts) > 2 and parts[2]:
            try:
                interval_seconds = max(1, int(parts[2])) * 60
            except ValueError:
                pass
        feeds.append((url, custom_name, interval_seconds))
    return feeds


def load_mute_filters():
    """Returns a list of lowercase phrases; a title is muted if it contains any of them."""
    phrases = []
    for line in _read_config_sections()['mute']:
        for phrase in line.split('|'):
            phrase = phrase.strip()
            if phrase:
                phrases.append(phrase.lower())
    return phrases


def load_twitch_channels():
    """Returns a list of lowercase Twitch channel login names to watch."""
    return [line.strip().lower() for line in _read_config_sections()['twitch'] if line.strip()]


def is_muted(title, mute_phrases):
    title_lower = title.lower()
    return any(phrase in title_lower for phrase in mute_phrases)


def is_online():
    """Quick, low-cost check for basic network connectivity."""
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2)
        return True
    except OSError:
        return False


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": {}, "unread": []}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def entry_id(entry):
    raw = entry.get('id') or entry.get('link') or (entry.get('title', '') + entry.get('published', ''))
    return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()


def entry_age_seconds(entry):
    """Returns seconds since publish/update time, or None if no date info available."""
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if not parsed:
        return None
    try:
        entry_time = calendar.timegm(parsed)
        return time_module.time() - entry_time
    except Exception:
        return None


def get_installed_version(pkgname):
    try:
        result = subprocess.run(
            ['xbps-query', '-p', 'pkgver', pkgname],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


def list_all_updates():
    """Read-only, in-memory, system-wide dry run — no root needed, nothing written
    to disk. Returns a list of pkgnames that have a real newer build published.

    Real xbps-install -Mn -u output (per package) looks like:
        cryptsetup-2.8.8_1 update x86_64 https://repo-default.voidlinux.org/current 3203607 568523
    i.e. "<pkgver> <action> <arch> <repo> <dlsize> <instsize>" — no '->' arrow."""
    try:
        result = subprocess.run(
            ['xbps-install', '-Mn', '-u'],
            capture_output=True, text=True, timeout=60
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    output = (result.stdout or '') + (result.stderr or '')
    updates = []
    for line in output.splitlines():
        line = line.strip()
        parts = line.split()
        if len(parts) < 2:
            continue
        pkgver_token, action = parts[0], parts[1]
        if action != 'update':
            continue
        match = re.match(r'^(.+)-[0-9][^-]*$', pkgver_token)
        if match:
            updates.append(match.group(1))
    return updates


def check_twitch_live_channels(channels):
    """Uses Twitch's internal (unofficial) GraphQL API — the same one twitch.tv
    itself uses for logged-out visitors — so no app registration/secret is
    needed. Undocumented; could break if Twitch changes their internal schema.
    Batches every channel into a single POST request (Twitch's GQL endpoint
    accepts a JSON array of operations) instead of one request per channel.
    Returns {channel: title} for whichever channels are currently live;
    a failed/offline channel is simply absent from the result, not marked False."""
    if not channels:
        return {}
    payload = json.dumps([
        {
            "operationName": "StreamMetadata",
            "query": "query StreamMetadata($channelLogin: String!) { "
                     "user(login: $channelLogin) { stream { type title } } }",
            "variables": {"channelLogin": channel},
        }
        for channel in channels
    ]).encode('utf-8')
    req = urllib.request.Request(
        TWITCH_GQL_URL,
        data=payload,
        headers={
            'Client-Id': TWITCH_GQL_CLIENT_ID,
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return {}
    if not isinstance(results, list):
        return {}
    live = {}
    for channel, result in zip(channels, results):
        user = (result.get('data') or {}).get('user')
        if not user:
            continue
        stream = user.get('stream')
        if stream and stream.get('type') == 'live':
            live[channel] = stream.get('title') or ''
    return live


def open_twitch_stream(channel):
    try:
        subprocess.Popen(
            ['streamlink', '--player', 'mpv', f'twitch.tv/{channel}', 'best'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


def play_notification_sound():
    """Best-effort: play a notification sound using whichever player is available.
    Silently does nothing if none are found."""
    for path in NOTIFICATION_SOUND_CANDIDATES:
        if not os.path.exists(path):
            continue
        for player in (['paplay'], ['aplay', '-q'],
                       ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet']):
            if shutil.which(player[0]):
                try:
                    subprocess.Popen(player + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:
                    continue


def is_fullscreen_active():
    """Best-effort check for whether the currently focused window is fullscreen,
    so the popup doesn't interrupt a video/game/presentation. Returns False
    (never suppresses) if the Wnck library isn't available."""
    if Wnck is None:
        return False
    try:
        screen = Wnck.Screen.get_default()
        screen.force_update()
        active = screen.get_active_window()
        return bool(active and active.is_fullscreen())
    except Exception:
        return False


def edit_file_externally(path):
    try:
        subprocess.Popen(['xdg-open', path])
        return
    except Exception:
        pass
    editor = os.environ.get('EDITOR', 'vi')
    if shutil.which('xfce4-terminal'):
        subprocess.Popen(['xfce4-terminal', '-e', f'{editor} "{path}"'])
    else:
        try:
            subprocess.Popen([editor, path])
        except Exception:
            print(f"Couldn't open an editor — edit manually: {path}")


def fetch_weather():
    """Best-effort fetch from Open-Meteo. Returns a dict or None on any failure."""
    try:
        with urllib.request.urlopen(WEATHER_API_URL, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None
    try:
        current = data.get('current', {})
        hourly = data.get('hourly', {})
        daily = data.get('daily', {})
        winds = hourly.get('wind_speed_10m', [])
        hourly_times = hourly.get('time', [])
        current_time = current.get('time')

        current_hour_idx = hourly_times.index(current_time) if current_time in hourly_times else 0
        remaining_winds = [w for w in winds[current_hour_idx:24] if w is not None]
        today_max_wind = max(remaining_winds) if remaining_winds else None

        daily_max = daily.get('temperature_2m_max', [])
        daily_min = daily.get('temperature_2m_min', [])
        daily_rain_prob = daily.get('precipitation_probability_max', [])
        daily_precip_sum = daily.get('precipitation_sum', [])

        forecast_days = []
        for i in range(1, 6):
            if i < len(daily_max) and i < len(daily_min):
                forecast_days.append({
                    'max_temp': daily_max[i],
                    'min_temp': daily_min[i],
                    'rain_prob': daily_rain_prob[i] if i < len(daily_rain_prob) else None,
                })

        return {
            'temp': current.get('temperature_2m'),
            'weather_code': current.get('weather_code'),
            'wind': current.get('wind_speed_10m'),
            'today_max_wind': today_max_wind,
            'today_max_temp': daily_max[0] if len(daily_max) > 0 else None,
            'today_min_temp': daily_min[0] if len(daily_min) > 0 else None,
            'today_rain_prob': daily_rain_prob[0] if len(daily_rain_prob) > 0 else None,
            'today_precip_sum': daily_precip_sum[0] if len(daily_precip_sum) > 0 else None,
            'forecast_days': forecast_days,
        }
    except Exception:
        return None


def weather_code_glyph(code):
    """Maps a WMO weather_code to a plain (non-color-emoji) Unicode glyph.
    Returns None for codes without a good simple symbol (e.g. fog), so the
    icon is just omitted rather than showing something misleading."""
    if code is None:
        return None
    if code == 0:
        return '\u2600'  # clear sky
    if code in (1, 2, 3):
        return '\u2601'  # mainly clear / partly cloudy / overcast
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return '\u2614'  # drizzle / rain / rain showers
    if code in (71, 73, 75, 77, 85, 86):
        return '\u2744'  # snow
    if code in (95, 96, 99):
        return '\u26a1'  # thunderstorm
    return None  # e.g. fog (45, 48) — no reliable simple glyph


def format_timer_duration(total_seconds):
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h} Hours, {m:02d} Minutes and {s:02d} Seconds"


class RssTray:
    def __init__(self):
        ensure_config()
        self.state = load_state()
        self.lock = threading.Lock()
        self._check_lock = threading.Lock()  # guards overlapping feed-check cycles only
        self._xbps_lock = threading.Lock()   # guards xbps db access (scans + installs), separately
        self._twitch_lock = threading.Lock()  # guards overlapping Twitch-check cycles
        self.popup = None
        self.listbox = None
        self.scroller = None
        self.weather_data = None
        self.weather_label = None
        self.weather_box = None
        self.weather_view = 'today'
        self.active_installs = 0
        self.install_status = {}  # pkgname -> 'Waiting…'/'Downloading…'/'Installing…'/'Done'/'Failed'
        self.timer_remaining_seconds = 0
        self.timer_running = False
        self._timer_updating_ui = False
        self.timer_scale = None
        self.timer_label = None
        self.timer_box = None
        self.timer_visible = False

        self._apply_compact_css()

        self.status_icon = Gtk.StatusIcon()
        self.status_icon.connect('activate', self.toggle_popup)
        self.status_icon.connect('popup-menu', self.toggle_popup)
        self.update_icon()

        GLib.timeout_add_seconds(1, self.initial_check)
        GLib.timeout_add_seconds(SCHEDULER_TICK_SECONDS, self.periodic_check)
        GLib.timeout_add(800, self.maybe_auto_show_startup)
        GLib.timeout_add_seconds(2, self.initial_weather_check)
        GLib.timeout_add_seconds(WEATHER_REFRESH_SECONDS, self.periodic_weather_check)
        GLib.timeout_add_seconds(3, self.initial_twitch_check)
        GLib.timeout_add_seconds(TWITCH_CHECK_INTERVAL_SECONDS, self.periodic_twitch_check)
        GLib.timeout_add_seconds(1, self._timer_tick)

    def maybe_auto_show_startup(self):
        if self.has_anything_to_show():
            self.show_popup(auto=True)
        return False

    def initial_check(self):
        if not is_online():
            GLib.timeout_add_seconds(NETWORK_RETRY_SECONDS, self.initial_check)
            return False
        self.start_check_thread(force=True)
        return False

    def periodic_check(self):
        self.start_check_thread()
        return True

    def start_check_thread(self, force=False):
        if not self._check_lock.acquire(blocking=False):
            return  # a check is already in flight — skip this tick rather than overlap
        threading.Thread(target=self._check_feeds_guarded, args=(force,), daemon=True).start()

    def _check_feeds_guarded(self, force):
        try:
            self.check_feeds(force)
            self.check_updates_if_due(force=force)
        finally:
            self._check_lock.release()

    def check_feeds(self, force=False):
        feeds = load_feeds()
        mute_phrases = load_mute_filters()
        new_items = []
        now = time_module.time()
        with self.lock:
            seen_ids = set(self.state.get('seen', {}).keys())
            last_checked = dict(self.state.get('last_checked', {}))
        newly_seen_ids = set()
        due_urls = []
        for url, _custom_name, interval_seconds in feeds:
            last = last_checked.get(url, 0)
            if force or (now - last) >= interval_seconds:
                due_urls.append(url)
        for url in due_urls:
            try:
                parsed = feedparser.parse(url)
            except Exception:
                continue
            last_checked[url] = now
            for entry in parsed.entries:
                eid = entry_id(entry)
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                newly_seen_ids.add(eid)
                title = entry.get('title', '(untitled)')
                if is_muted(title, mute_phrases):
                    continue  # matches mute filters — mark as seen, don't surface
                age = entry_age_seconds(entry)
                if age is not None and age > MAX_ITEM_AGE_SECONDS:
                    continue  # too old — mark as seen, don't surface
                new_items.append({
                    'id': eid,
                    'title': title,
                    'link': entry.get('link', ''),
                    'feed_url': url,
                    'pkg_match': None,
                })
        with self.lock:
            merged_seen = dict(self.state.get('seen', {}))
            for eid in newly_seen_ids:
                merged_seen.setdefault(eid, now)
            cutoff = now - SEEN_RETENTION_SECONDS
            merged_seen = {eid: ts for eid, ts in merged_seen.items() if ts >= cutoff}

            merged_last_checked = dict(self.state.get('last_checked', {}))
            merged_last_checked.update(last_checked)

            self.state['seen'] = merged_seen
            self.state['last_checked'] = merged_last_checked

            if new_items:
                existing_ids = {e['id'] for e in self.state.get('unread', [])}
                new_items = [e for e in new_items if e['id'] not in existing_ids]
                if new_items:
                    self.state['unread'] = new_items + self.state.get('unread', [])
            save_state(self.state)
        if new_items:
            GLib.idle_add(self.on_new_items)

    def check_updates_if_due(self, force=False):
        now = time_module.time()
        with self.lock:
            last = self.state.get('updates_last_checked', 0)
        if not force and (now - last) < PENDING_CHECK_INTERVAL_SECONDS:
            return
        pkgnames = list_all_updates()
        with self.lock:
            existing = {e['pkgname']: e for e in self.state.get('available_updates', [])}
            promoted_new = []
            for pkgname in pkgnames:
                if pkgname not in existing:
                    entry = {
                        'id': hashlib.sha1(f"update:{pkgname}".encode()).hexdigest(),
                        'title': f"Update available for {pkgname}",
                        'link': '',
                        'pkgname': pkgname,
                    }
                    existing[pkgname] = entry
                    promoted_new.append(entry)
            self.state['available_updates'] = [existing[p] for p in pkgnames if p in existing]
            self.state['updates_last_checked'] = now
            save_state(self.state)
        if promoted_new:
            GLib.idle_add(self.on_new_items)  # reuse: sound + auto-popup + icon refresh

    def on_new_items(self):
        self.update_icon()
        play_notification_sound()
        self.show_popup(auto=True)  # auto-open whenever new unread items, updates, or live channels arrive
        return False

    def initial_twitch_check(self):
        if not is_online():
            GLib.timeout_add_seconds(NETWORK_RETRY_SECONDS, self.initial_twitch_check)
            return False
        self.start_twitch_check()
        return False

    def periodic_twitch_check(self):
        self.start_twitch_check()
        return True

    def start_twitch_check(self):
        if not self._twitch_lock.acquire(blocking=False):
            return  # a check is already in flight — skip this tick
        threading.Thread(target=self._check_twitch_guarded, daemon=True).start()

    def _check_twitch_guarded(self):
        try:
            channels = load_twitch_channels()
            if channels:
                live_now = check_twitch_live_channels(channels)
                GLib.idle_add(self._on_twitch_checked, live_now)
        finally:
            self._twitch_lock.release()

    def _on_twitch_checked(self, live_now):
        with self.lock:
            was_live = {e['channel'] for e in self.state.get('live_channels', [])}
            self.state['live_channels'] = [
                {'channel': ch, 'title': live_now[ch]} for ch in sorted(live_now)
            ]
            save_state(self.state)
        newly_live = set(live_now) - was_live
        if newly_live:
            self.on_new_items()
        else:
            self.update_icon()
            if self.popup and self.popup.get_visible():
                self.refresh_list()
        return False

    def initial_weather_check(self):
        self.start_weather_fetch()
        return False

    def periodic_weather_check(self):
        self.start_weather_fetch()
        return True

    def start_weather_fetch(self):
        threading.Thread(target=self._fetch_weather_bg, daemon=True).start()

    def _fetch_weather_bg(self):
        data = fetch_weather()
        GLib.idle_add(self._on_weather_fetched, data)

    def _on_weather_fetched(self, data):
        if data is not None:
            self.weather_data = data
        self.update_weather_label()
        self.update_icon()
        return False

    def format_weather_markup(self):
        d = self.weather_data
        if not d:
            return '<span size="large">Weather unavailable</span>'

        if self.weather_view == 'forecast':
            days = d.get('forecast_days', [])
            if not days:
                return '<span size="medium">Forecast unavailable</span>'
            parts = []
            for day in days:
                segment = ''
                if day.get('rain_prob') is not None and day['rain_prob'] > 0:
                    segment += '<span foreground="#2b2b2b">\u2614</span> '
                if day.get('max_temp') is not None and day.get('min_temp') is not None:
                    segment += GLib.markup_escape_text(
                        f"{day['max_temp']:.0f}/{day['min_temp']:.0f}\u00b0C"
                    )
                if segment:
                    parts.append(segment)
            text = " \u00b7 ".join(parts) if parts else "Forecast unavailable"
            return f'<span size="medium"><b>{text}</b></span>'

        parts = []
        glyph = weather_code_glyph(d.get('weather_code'))
        if d.get('temp') is not None:
            temp_text = GLib.markup_escape_text(f"{d['temp']:.0f}°C")
            if glyph:
                temp_text = f'<span foreground="#2b2b2b" rise="6000">{glyph}</span>' + temp_text
            parts.append(temp_text)
        if d.get('today_max_temp') is not None and d.get('today_min_temp') is not None:
            parts.append(GLib.markup_escape_text(
                f"{d['today_max_temp']:.0f}/{d['today_min_temp']:.0f}°C"
            ))
        if d.get('wind') is not None and d.get('today_max_wind') is not None:
            parts.append(GLib.markup_escape_text(f"{d['wind']:.0f}/{d['today_max_wind']:.0f} km/h"))
        elif d.get('wind') is not None:
            parts.append(GLib.markup_escape_text(f"{d['wind']:.0f} km/h"))
        if d.get('today_rain_prob') is not None and d.get('today_precip_sum') is not None:
            parts.append(GLib.markup_escape_text(
                f"{d['today_rain_prob']:.0f}%/{d['today_precip_sum']:.1f}mm"
            ))
        elif d.get('today_rain_prob') is not None:
            parts.append(GLib.markup_escape_text(f"{d['today_rain_prob']:.0f}%"))
        text = " · ".join(parts) if parts else "Weather unavailable"
        return f'<span size="large"><b>{text}</b></span>'

    def update_weather_label(self):
        if self.weather_label is None:
            return
        self.weather_label.set_markup(self.format_weather_markup())

    def rebuild_weather_bar(self):
        if self.weather_box is None:
            return
        for child in self.weather_box.get_children():
            self.weather_box.remove(child)

        label = Gtk.Label()
        label.set_xalign(0.5)
        label.set_hexpand(True)
        label.set_line_wrap(True)
        label.set_max_width_chars(48)
        label.set_justify(Gtk.Justification.CENTER)
        self.weather_label = label
        self.update_weather_label()

        if self.weather_view == 'forecast':
            back_btn = Gtk.Button(label='\u2039')
            back_btn.set_relief(Gtk.ReliefStyle.NONE)
            back_btn.set_tooltip_text('Back to today')
            back_btn.connect('clicked', self.on_weather_arrow_clicked, 'today')
            self.weather_box.pack_start(back_btn, False, False, 0)
            self.weather_box.pack_start(label, True, True, 0)
        else:
            self.weather_box.pack_start(label, True, True, 0)
            fwd_btn = Gtk.Button(label='\u203a')
            fwd_btn.set_relief(Gtk.ReliefStyle.NONE)
            fwd_btn.set_tooltip_text('Show 5-day forecast')
            fwd_btn.connect('clicked', self.on_weather_arrow_clicked, 'forecast')
            self.weather_box.pack_start(fwd_btn, False, False, 0)

        self.weather_box.show_all()

    def on_weather_arrow_clicked(self, _button, target_view):
        self.weather_view = target_view
        self.rebuild_weather_bar()

    def _begin_install(self):
        self.active_installs += 1
        self.update_icon()
        self.refresh_list()  # show per-row status under "Updates available"

    def _end_install(self):
        self.active_installs = max(0, self.active_installs - 1)
        self.update_icon()

    def on_timer_toggle_clicked(self, _button):
        self.timer_visible = not self.timer_visible
        if self.timer_box is not None:
            if self.timer_visible:
                self.timer_box.show_all()
            else:
                self.timer_box.hide()

    def _timer_tick(self):
        if self.timer_running and self.timer_remaining_seconds > 0:
            self.timer_remaining_seconds -= 1
            if self.timer_remaining_seconds <= 0:
                self.timer_remaining_seconds = 0
                self.timer_running = False
                self._fire_timer_done()
            self._update_timer_widgets()
        return True

    def _fire_timer_done(self):
        play_notification_sound()
        GLib.timeout_add(700, self._play_second_beep)

    def _play_second_beep(self):
        play_notification_sound()
        return False

    def _update_timer_widgets(self):
        if self.timer_scale is None or self.timer_label is None:
            return
        self._timer_updating_ui = True
        self.timer_scale.set_value(self.timer_remaining_seconds)
        self._timer_updating_ui = False
        self.timer_label.set_text(format_timer_duration(self.timer_remaining_seconds))

    def on_timer_slider_changed(self, scale):
        if self._timer_updating_ui:
            return
        value = int(scale.get_value())
        self.timer_remaining_seconds = value
        self.timer_running = value > 0
        if self.timer_label is not None:
            self.timer_label.set_text(format_timer_duration(value))

    def _should_show_weather_icon(self, count):
        return (
            count == 0 and not self.has_pkg_update()
            and self.weather_data and self.weather_data.get('temp') is not None
        )

    def format_weather_tooltip_text(self):
        """Plain-text version of the popup's 'today' weather line, for the
        tray icon's tooltip (which doesn't render Pango markup)."""
        d = self.weather_data
        if not d:
            return "Weather unavailable"
        parts = []
        if d.get('temp') is not None:
            parts.append(f"{d['temp']:.0f}°C")
        if d.get('today_max_temp') is not None and d.get('today_min_temp') is not None:
            parts.append(f"{d['today_max_temp']:.0f}/{d['today_min_temp']:.0f}°C")
        if d.get('wind') is not None and d.get('today_max_wind') is not None:
            parts.append(f"{d['wind']:.0f}/{d['today_max_wind']:.0f} km/h")
        elif d.get('wind') is not None:
            parts.append(f"{d['wind']:.0f} km/h")
        if d.get('today_rain_prob') is not None and d.get('today_precip_sum') is not None:
            parts.append(f"{d['today_rain_prob']:.0f}%/{d['today_precip_sum']:.1f}mm")
        elif d.get('today_rain_prob') is not None:
            parts.append(f"{d['today_rain_prob']:.0f}%")
        return " · ".join(parts) if parts else "Weather unavailable"

    def update_icon(self):
        count = self.total_badge_count()
        self.status_icon.set_from_pixbuf(self.render_icon(count))
        if self._should_show_weather_icon(count):
            tooltip = self.format_weather_tooltip_text()
        elif self.has_pkg_update():
            tooltip = f"{count} unread — package update available"
        elif count:
            tooltip = f"{count} unread"
        else:
            tooltip = "No unread items"
        self.status_icon.set_tooltip_text(tooltip)
        return False

    def total_badge_count(self):
        # Live channels intentionally excluded — they show in the popup list
        # and still trigger the sound/auto-popup notification, but shouldn't
        # affect the tray icon's badge number or color.
        with self.lock:
            return (
                len(self.state.get('unread', []))
                + len(self.state.get('available_updates', []))
            )

    def has_anything_to_show(self):
        with self.lock:
            return (
                bool(self.state.get('unread'))
                or bool(self.state.get('available_updates'))
                or bool(self.state.get('live_channels'))
            )

    def has_pkg_update(self):
        with self.lock:
            return bool(self.state.get('available_updates'))

    def render_icon(self, count):
        size = 24
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        ctx = cairo.Context(surface)

        show_weather = self._should_show_weather_icon(count)

        if show_weather:
            # Temperature only, no glyph — the icon is a fixed-size XEMBED
            # tray slot with no way to make it bigger overall, so dropping
            # the glyph here lets the digits alone claim the full icon
            # instead of splitting the space with it. The glyph still shows
            # in the popup's weather bar, where space isn't constrained.
            ctx.set_source_rgba(1, 1, 1, 1)
            temp_text = f"{self.weather_data['temp']:.0f}"
            padding = 2
            max_width = size - 2 * padding

            ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            temp_size = 20
            min_temp_size = 10
            while temp_size > min_temp_size:
                ctx.set_font_size(temp_size)
                if ctx.text_extents(temp_text)[4] <= max_width:
                    break
                temp_size -= 1

            xb, yb, tw, th, dx, dy = ctx.text_extents(temp_text)
            ctx.move_to((size - tw) / 2 - xb, size / 2 - th / 2 - yb)
            ctx.show_text(temp_text)
        else:
            if self.has_pkg_update():
                ctx.set_source_rgba(0.82, 0.18, 0.18, 1)   # red: update available
            elif count > 0:
                ctx.set_source_rgba(0.92, 0.55, 0.10, 1)   # orange: unread news / live channel
            else:
                ctx.set_source_rgba(0.20, 0.65, 0.30, 1)   # green: nothing unread, weather not loaded yet
            ctx.arc(size / 2, size / 2, size / 2 - 1, 0, 2 * 3.14159265)
            ctx.fill()

            ctx.set_source_rgba(1, 1, 1, 1)
            text = str(count) if count < 100 else '99+'
            ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(12 if len(text) <= 2 else 8)
            xb, yb, w, h, dx, dy = ctx.text_extents(text)
            ctx.move_to(size / 2 - w / 2 - xb, size / 2 - h / 2 - yb)
            ctx.show_text(text)

        surface.flush()
        return Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)

    def feed_name_for(self, url, feeds_map=None):
        if feeds_map is not None:
            return feeds_map.get(url, url)
        for feed_url, custom_name, _interval in load_feeds():
            if feed_url == url:
                return custom_name or feed_url
        return url

    # --- popup window ---

    def _apply_compact_css(self):
        css = b"""
        list row { padding: 1px 3px; min-height: 0px; }
        button { padding: 1px; }
        .weather-bar { background-color: #e8eef5; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _update_scroller_max_height(self):
        try:
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            screen_height = monitor.get_geometry().height
        except Exception:
            screen_height = 1080
        max_height = int(screen_height * 0.75)
        self.scroller.set_max_content_height(max_height)

    def build_popup_window(self):
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        win.set_keep_above(True)
        win.set_default_size(WINDOW_WIDTH, -1)
        win.connect('focus-out-event', lambda *_a: win.hide())
        win.connect('key-press-event', self.on_popup_key)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        weather_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        weather_box.get_style_context().add_class('weather-bar')
        weather_box.set_margin_start(6)
        weather_box.set_margin_end(6)
        weather_box.set_margin_top(4)
        weather_box.set_margin_bottom(4)
        self.weather_box = weather_box
        self.rebuild_weather_bar()
        outer.pack_start(weather_box, False, False, 0)
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        self.scroller = scroller
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect('row-activated', self.on_row_activated)
        self.listbox.connect('button-press-event', self.on_listbox_button_press)
        scroller.add(self.listbox)
        outer.pack_start(scroller, True, True, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        timer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        timer_box.set_margin_start(8)
        timer_box.set_margin_end(8)
        timer_box.set_margin_top(3)
        timer_box.set_margin_bottom(4)

        timer_label = Gtk.Label()
        timer_label.set_xalign(0.5)
        timer_label.set_text(format_timer_duration(self.timer_remaining_seconds))
        self.timer_label = timer_label
        timer_box.pack_start(timer_label, False, False, 0)

        timer_adjustment = Gtk.Adjustment(
            value=self.timer_remaining_seconds, lower=0, upper=7200,
            step_increment=60, page_increment=300, page_size=0
        )
        timer_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=timer_adjustment)
        timer_scale.set_draw_value(False)
        timer_scale.connect('value-changed', self.on_timer_slider_changed)
        self.timer_scale = timer_scale
        timer_box.pack_start(timer_scale, False, False, 0)

        timer_box.set_no_show_all(True)  # only shown/hidden via the bell toggle, not blanket show_all()
        self.timer_box = timer_box
        outer.pack_start(timer_box, False, False, 0)
        if self.timer_visible:
            timer_box.show_all()

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        footer.set_margin_start(6)
        footer.set_margin_end(6)
        footer.set_margin_top(4)
        footer.set_margin_bottom(4)
        timer_toggle_btn = Gtk.Button()
        timer_toggle_btn.set_relief(Gtk.ReliefStyle.NONE)
        bell_label = Gtk.Label()
        bell_label.set_markup('<span size="large">\U0001F514</span>')
        timer_toggle_btn.add(bell_label)
        timer_toggle_btn.set_tooltip_text('Show/hide countdown timer')
        timer_toggle_btn.connect('clicked', self.on_timer_toggle_clicked)
        footer.pack_start(timer_toggle_btn, False, False, 0)
        edit_btn = Gtk.Button(label='Edit config')
        edit_btn.connect('clicked', lambda *_a: edit_file_externally(CONFIG_FILE))
        footer.pack_start(edit_btn, True, True, 0)
        refresh_btn = Gtk.Button(label='Refresh')
        refresh_btn.connect('clicked', lambda *_a: (
            self.start_check_thread(force=True), self.start_twitch_check()
        ))
        footer.pack_start(refresh_btn, True, True, 0)
        mark_all_btn = Gtk.Button(label='Mark all read')
        mark_all_btn.connect('clicked', self.on_mark_all_read)
        footer.pack_start(mark_all_btn, True, True, 0)
        quit_btn = Gtk.Button(label='Quit')
        quit_btn.connect('clicked', lambda *_a: Gtk.main_quit())
        footer.pack_start(quit_btn, True, True, 0)
        outer.pack_start(footer, False, False, 0)

        win.add(outer)
        self.popup = win

    def on_popup_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            widget.hide()
        return False

    def toggle_popup(self, *_args):
        if self.popup and self.popup.get_visible():
            self.popup.hide()
            return
        self.show_popup()

    def show_popup(self, auto=False):
        if auto and is_fullscreen_active():
            return  # don't interrupt a fullscreen video/game/presentation
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None
            self.timer_scale = None
            self.timer_label = None
            self.timer_box = None
        self.weather_view = 'today'
        self.build_popup_window()
        self._update_scroller_max_height()
        self.refresh_list()
        self.position_popup()
        self.popup.show_all()
        self.popup.present()
        self.popup.grab_focus()

    def position_popup(self):
        x = y = None
        try:
            ok, screen, area, _orientation = self.status_icon.get_geometry()
        except Exception:
            ok = False
        if ok and area is not None:
            x = area.x
            y = area.y + area.height
            screen_width = screen.get_width() if screen else None
            if screen_width and x + WINDOW_WIDTH > screen_width:
                x = screen_width - WINDOW_WIDTH - 4
        if x is None:
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            geo = monitor.get_geometry()
            x = geo.x + geo.width - WINDOW_WIDTH - 10
            y = geo.y + 30
        self.popup.move(max(x, 0), max(y, 0))

    def refresh_list(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        with self.lock:
            unread = list(self.state.get('unread', []))
            available = list(self.state.get('available_updates', []))
            live_channels = list(self.state.get('live_channels', []))

        feeds_map = {url: (custom_name or url) for url, custom_name, _interval in load_feeds()}

        if not unread and not available and not live_channels:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            lbl = Gtk.Label(label='No unread items')
            lbl.set_margin_top(8)
            lbl.set_margin_bottom(8)
            row.add(lbl)
            self.listbox.add(row)
            if self.popup:
                self.popup.hide()
        else:
            is_first_section = True

            if live_channels:
                self.listbox.add(self.build_header_row(
                    '__live__', 'Live now', is_first=is_first_section, clickable=False
                ))
                is_first_section = False
                for entry in live_channels:
                    self.listbox.add(self.build_twitch_row(entry))

            if unread:
                shown = unread[:MAX_LIST_ITEMS]
                groups = {}
                order = []
                for entry in shown:
                    feed_url = entry.get('feed_url', '')
                    if feed_url not in groups:
                        groups[feed_url] = []
                        order.append(feed_url)
                    groups[feed_url].append(entry)

                for i, feed_url in enumerate(order):
                    feed_name = feeds_map.get(feed_url, feed_url)
                    self.listbox.add(self.build_header_row(
                        feed_url, feed_name, is_first=(is_first_section and i == 0)
                    ))
                    for entry in groups[feed_url]:
                        self.listbox.add(self.build_row(entry))
                is_first_section = False

                if len(unread) > MAX_LIST_ITEMS:
                    row = Gtk.ListBoxRow()
                    row.set_selectable(False)
                    row.set_activatable(False)
                    lbl = Gtk.Label(label=f"... and {len(unread) - MAX_LIST_ITEMS} more")
                    row.add(lbl)
                    self.listbox.add(row)

            if available:
                self.listbox.add(self.build_header_row(
                    '__updates__', 'Updates available',
                    is_first=is_first_section, clickable=False, install_all=True
                ))
                is_first_section = False
                for entry in available[:MAX_LIST_ITEMS]:
                    self.listbox.add(self.build_info_row(entry))
                if len(available) > MAX_LIST_ITEMS:
                    row = Gtk.ListBoxRow()
                    row.set_selectable(False)
                    row.set_activatable(False)
                    lbl = Gtk.Label(label=f"... and {len(available) - MAX_LIST_ITEMS} more")
                    row.add(lbl)
                    self.listbox.add(row)
        self.listbox.show_all()

    def build_header_row(self, feed_url, feed_name, is_first=False, clickable=True, install_all=False):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(clickable or install_all)
        if install_all:
            row.install_all_header = True
            row.set_tooltip_text("Install all available updates")
        elif clickable:
            row.header_feed_url = feed_url
            row.set_tooltip_text(f"Mark all '{feed_name}' items as read")

        row.set_margin_top(4 if is_first else 20)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_bottom(2)

        label = Gtk.Label()
        label.set_markup(f'<span size="larger" weight="bold">{GLib.markup_escape_text(feed_name)}</span>')
        label.set_xalign(0.5)
        label.set_halign(Gtk.Align.CENTER)
        box.pack_start(label, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_hexpand(True)
        box.pack_start(sep, False, False, 0)

        row.add(box)
        return row

    def build_twitch_row(self, entry):
        channel = entry['channel']
        title = entry.get('title') or ''

        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(True)
        row.twitch_channel = channel
        tooltip = f"Watch {channel}"
        if title:
            tooltip += f" — {title}"
        tooltip += f"\nstreamlink --player mpv twitch.tv/{channel} best"
        row.set_tooltip_text(tooltip)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_start(3)
        outer.set_margin_end(3)
        outer.set_margin_top(1)
        outer.set_margin_bottom(1)

        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        dot = Gtk.Label()
        dot.set_markup('<span foreground="#9146FF"><b>●</b></span>')
        top_row.pack_start(dot, False, False, 0)

        name_label = Gtk.Label()
        name_label.set_markup(f'<span foreground="#000000"><b>{GLib.markup_escape_text(channel)}</b></span>')
        name_label.set_xalign(0)
        name_label.set_hexpand(True)
        top_row.pack_start(name_label, True, True, 0)
        outer.pack_start(top_row, False, False, 0)

        if title:
            shown_title = title if len(title) <= MAX_TITLE_LEN else title[:MAX_TITLE_LEN - 1] + '…'
            title_label = Gtk.Label()
            title_label.set_markup(
                f'<span size="small" foreground="#555555">{GLib.markup_escape_text(shown_title)}</span>'
            )
            title_label.set_xalign(0)
            title_label.set_margin_start(14)
            outer.pack_start(title_label, False, False, 0)

        row.add(outer)
        return row

    def build_info_row(self, entry):
        """Purely informational row: no mark-as-read button, no click action.
        Used for 'Updates available' entries — install is only ever triggered
        via the section header (install all), never per-row."""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(0)
        box.set_margin_bottom(0)

        status = self.install_status.get(entry.get('pkgname'))
        if status:
            status_label = Gtk.Label()
            status_label.set_markup(
                f'<span foreground="#2b5fad"><i>{GLib.markup_escape_text(status)}</i></span>'
            )
            box.pack_start(status_label, False, False, 0)

        full_title = entry['title']
        truncated = len(full_title) > MAX_TITLE_LEN
        title = full_title[:MAX_TITLE_LEN - 1] + '\u2026' if truncated else full_title
        text = GLib.markup_escape_text(title)
        label = Gtk.Label()
        label.set_markup(f'<span foreground="#000000"><b>{text}</b></span>')
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        if truncated:
            label.set_tooltip_text(full_title)
            row.set_tooltip_text(full_title)

        box.pack_start(label, True, True, 0)
        row.add(box)
        return row

    def build_row(self, entry):
        row = Gtk.ListBoxRow()
        row.entry_id = entry['id']
        row.link = entry['link']

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(0)
        box.set_margin_bottom(0)

        mark_btn = Gtk.Button()
        mark_btn.set_relief(Gtk.ReliefStyle.NONE)
        mark_icon = Gtk.Image.new_from_icon_name('mail-mark-read-symbolic', Gtk.IconSize.MENU)
        mark_btn.add(mark_icon)
        mark_btn.set_tooltip_text('Mark as read')
        mark_btn.connect('clicked', self.on_mark_read_clicked, entry['id'])
        box.pack_start(mark_btn, False, False, 0)

        full_title = entry['title']
        truncated = len(full_title) > MAX_TITLE_LEN
        title = full_title[:MAX_TITLE_LEN - 1] + '\u2026' if truncated else full_title
        text = GLib.markup_escape_text(title)
        label = Gtk.Label()
        label.set_markup(f'<span foreground="#000000"><b>{text}</b></span>')
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        if truncated:
            row.set_tooltip_text(full_title)
            label.set_tooltip_text(full_title)

        box.pack_start(label, True, True, 0)
        row.add(box)
        return row

    def on_listbox_button_press(self, listbox, event):
        if event.button == 3:  # right-click: mark as read without opening
            row = listbox.get_row_at_y(int(event.y))
            if row is not None and hasattr(row, 'entry_id'):
                self.on_mark_read_clicked(None, row.entry_id)
                return True
        return False

    def _remove_unread(self, item_id):
        with self.lock:
            self.state['unread'] = [e for e in self.state.get('unread', []) if e['id'] != item_id]
            save_state(self.state)

    def _remove_available_update(self, item_id):
        with self.lock:
            self.state['available_updates'] = [
                e for e in self.state.get('available_updates', []) if e['id'] != item_id
            ]
            save_state(self.state)

    def install_all_updates(self):
        if self.active_installs > 0:
            return  # already running
        with self.lock:
            pkgnames = [e['pkgname'] for e in self.state.get('available_updates', [])]
        if not pkgnames:
            return
        self.install_status = {pkg: 'Waiting…' for pkg in pkgnames}
        self._begin_install()
        threading.Thread(target=self._run_update_all, args=(pkgnames,), daemon=True).start()

    def _set_status(self, pkgname, status):
        GLib.idle_add(self._apply_status, pkgname, status)

    def _apply_status(self, pkgname, status):
        self.install_status[pkgname] = status
        self.refresh_list()
        return False

    def _finalize_package_removal(self, pkgname):
        with self.lock:
            self.state['available_updates'] = [
                e for e in self.state.get('available_updates', []) if e['pkgname'] != pkgname
            ]
            save_state(self.state)
        self.install_status.pop(pkgname, None)
        self.update_icon()
        self.refresh_list()
        return False

    def _run_update_all(self, pkgnames):
        with self._xbps_lock:
            for pkgname in pkgnames:
                self._set_status(pkgname, 'Installing…')
                before = get_installed_version(pkgname)
                cmd = PRIVILEGE_CMD + ['xbps-install', '-Su', '-y', pkgname]
                returncode = -1
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1
                    )
                    # xbps-install prints section headers like "[*] Downloading
                    # packages", "[*] Collecting package files", "[*] Unpacking
                    # packages", "[*] Configuring unpacked packages" — the actual
                    # per-file lines under them don't contain words like
                    # "download" at all, so we key off these headers instead.
                    # Since we install one package at a time, every line in this
                    # stream belongs to the current package regardless of wording.
                    for line in proc.stdout:
                        stripped = line.strip()
                        if stripped.startswith('[*] Downloading'):
                            self._set_status(pkgname, 'Downloading…')
                        elif stripped.startswith('[*]'):
                            self._set_status(pkgname, 'Installing…')
                    proc.wait(timeout=UPDATE_TIMEOUT_SECONDS)
                    returncode = proc.returncode
                except Exception:
                    pass
                after = get_installed_version(pkgname)
                success = returncode == 0 and before != after
                if success:
                    self._set_status(pkgname, 'Done')
                    GLib.timeout_add(1200, self._finalize_package_removal, pkgname)
                else:
                    self._set_status(pkgname, 'Failed')
        GLib.idle_add(self._end_install)

    def on_row_activated(self, _listbox, row):
        if getattr(row, 'install_all_header', False):
            self.install_all_updates()
            return
        if hasattr(row, 'header_feed_url'):
            self.mark_feed_read(row.header_feed_url)
            return
        if hasattr(row, 'twitch_channel'):
            open_twitch_stream(row.twitch_channel)
            return
        if not hasattr(row, 'entry_id'):
            return
        item_id, link = row.entry_id, row.link
        self._remove_unread(item_id)
        if link:
            webbrowser.open(link)
        self.update_icon()
        self.refresh_list()

    def mark_feed_read(self, feed_url):
        with self.lock:
            self.state['unread'] = [
                e for e in self.state.get('unread', [])
                if e.get('feed_url', '') != feed_url
            ]
            save_state(self.state)
        self.update_icon()
        self.refresh_list()

    def on_mark_read_clicked(self, _button, item_id):
        self._remove_unread(item_id)
        self.update_icon()
        self.refresh_list()

    def on_mark_all_read(self, *_args):
        with self.lock:
            self.state['unread'] = []
            save_state(self.state)
        self.update_icon()
        self.refresh_list()


def main():
    RssTray()
    Gtk.main()


if __name__ == '__main__':
    main()
