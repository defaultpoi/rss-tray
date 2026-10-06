#!/usr/bin/env python3
"""Minimal tray RSS/Atom reader with system-wide Void package-update detection
and Twitch live-channel notifications."""
import warnings
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, Gio, Pango
try:
    gi.require_version('Wnck', '3.0')
    from gi.repository import Wnck
except Exception:
    Wnck = None  # fullscreen detection just no-ops if this isn't available
try:
    gi.require_version('GLibUnix', '2.0')
    from gi.repository import GLibUnix  # GLib >= 2.78: where the unix signal helpers moved
except Exception:
    GLibUnix = None
import cairo

# Gtk.StatusIcon is deprecated upstream but is what the XEMBED tray needs here;
# silence the per-call warnings it would otherwise print on every icon update.
warnings.filterwarnings('ignore', message=r'Gtk\.StatusIcon')
warnings.filterwarnings('ignore', message=r'GLib\.unix_signal_add')  # older-GLib fallback path
import feedparser
import json
import math
import os
import re
import shutil
import socket
import sys
import signal
import subprocess
import threading
import webbrowser
import hashlib
import atexit
import calendar
import time as time_module
import urllib.request
from datetime import datetime, timedelta, timezone

socket.setdefaulttimeout(15)  # avoid feed fetches hanging indefinitely on slow/broken servers

CONFIG_DIR = os.path.expanduser('~/.config/rss-tray')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.conf')
STATE_FILE = os.path.join(CONFIG_DIR, 'state.json')
CHECK_INTERVAL = 600  # default per-feed interval (seconds) when none is set in config.conf
SCHEDULER_TICK_SECONDS = 60  # how often we check whether any feed is due
PENDING_CHECK_INTERVAL_SECONDS = 12 * 3600  # default: twice a day, for system-wide package updates
TWITCH_CHECK_INTERVAL_SECONDS = 900  # how often to poll Twitch live status
NETWORK_RETRY_SECONDS = 10  # how often to recheck connectivity if offline at startup
OFFLINE_PROBE_SECONDS = 10  # how often the popup's offline indicator re-probes the network
OFFLINE_AFTER_FAILED_PROBES = 2  # consecutive failed probes before showing 'offline' (ignores blips)
FEED_RETRY_SECONDS = 120  # retry a feed this soon after a failed fetch (instead of a full interval)
MAX_LIST_ITEMS = 40
MAX_UNREAD_ITEMS = 500  # stored unread items; the oldest beyond this are dropped (they stay 'seen')
MAX_TITLE_LEN = 60
MAX_ITEM_AGE_SECONDS = 24 * 3600  # ignore entries older than this on first sight
SEEN_RETENTION_SECONDS = 30 * 24 * 3600  # prune seen-item records older than this
PRIVILEGE_CMD = ['sudo', '-n']  # -n: fail fast rather than hang if a password would be needed;
                          # change to ['doas'] if that's what you use; requires
                          # passwordless (NOPASSWD) rules for xbps-install, since
                          # updates run headlessly with no terminal/tty attached
WINDOW_WIDTH = 471  # 380 * 1.2, +15px total
UPDATE_TIMEOUT_SECONDS = 1800  # 30 minutes
NOTIFICATION_SOUND_FILE = os.path.join(CONFIG_DIR, 'notification.wav')  # no sound if absent

def build_weather_api_url(lat, lon):
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,wind_speed_10m,weather_code"
        "&hourly=wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum"
        "&forecast_days=6&timezone=auto"
    )
WEATHER_REFRESH_SECONDS = 1800  # default: 30 minutes (weather and alerts share this cycle)
METEOALARM_FEED_URL_TEMPLATE = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{country}"
# ISO 3166-1 alpha-2 codes of the countries MeteoAlarm publishes feeds for.
# Anywhere else (reverse-geocoded country_code not in here) alerts are
# switched off in config.conf, since there's no feed to poll.
METEOALARM_COUNTRY_CODES = frozenset({
    'AT', 'BA', 'BE', 'BG', 'CH', 'CY', 'CZ', 'DE', 'DK', 'EE', 'ES', 'FI',
    'FR', 'GB', 'GR', 'HR', 'HU', 'IE', 'IL', 'IS', 'IT', 'LT', 'LU', 'LV',
    'MD', 'ME', 'MK', 'MT', 'NL', 'NO', 'PL', 'PT', 'RO', 'RS', 'SE', 'SI',
    'SK', 'UA',
})
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
ALERT_PULSE_INTERVAL_MS = 600
WEATHER_BULLET_MARKUP = '<span size="large"><b>·</b></span>'  # between weather-bar values
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's own public web-client ID —
                                                          # used by twitch.tv itself for logged-out
                                                          # visitors. Unofficial/undocumented; no
                                                          # app registration or secret needed.


def ensure_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
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
            "# Twitch channel login names to watch for live status, one per line:\n"
            "#   channel|quality (optional)|schedule (optional)\n"
            "# Uses Twitch's own internal (unofficial) API — no account/app needed.\n"
            "# Clicking a live channel plays it (streamlink + mpv, one shared maximized window).\n"
            "# interval=<minutes> sets how often to check (default 15, minimum 1).\n"
            "# A schedule limits checking to when the channel is expected to stream\n"
            "# (system local time): [days] HH:MM[-HH:MM], several separated by ;\n"
            "# Days: mon..sun, ranges (tue-sun), lists (mon,wed), daily, weekdays,\n"
            "# weekends; no end time means until midnight. Examples:\n"
            "# examplechannel\n"
            "# weeklychannel||wed 18:00-23:00\n"
            "# dailychannel|720p60|tue-sun 13:00-; sat 10:00-12:00\n"
            "\n"
            "[youtube]\n"
            "# Same format as [twitch]; identifiers are @handle or channel/UC... and\n"
            "# case-sensitive. interval=<minutes>: default 15, minimum 5.\n"
            "\n"
            "[updates]\n"
            "# interval=<hours> between package update scans (default 12, minimum 1).\n"
            "\n"
            "[actions]\n"
            "# Commands the timer's Schedule mode can run, one per line: Label|command\n"
            "# (built-in defaults if this section is empty: Suspend and Power off).\n"
            "Suspend|loginctl suspend\n"
            "Power off|loginctl poweroff\n"
            "\n"
            "[schedule]\n"
            "# Recurring actions, managed from the popup's Timer > Schedule (or here),\n"
            "# one per line: on|off | HH:MM | days | action label from [actions]\n"
            "# days: daily, weekdays, weekends or e.g. mon,wed,fri. Examples:\n"
            "# on|00:30|daily|Suspend\n"
            "# off|07:00|weekdays|Lock screen\n"
            "# Shared settings: warn=<seconds of warning, 0 = none>, snooze=<minutes the\n"
            "# Snooze button postpones>, grace=<seconds: a run missed by more than this,\n"
            "# e.g. while the machine was asleep, is skipped instead of run late>.\n"
            "warn=60\n"
            "snooze=30\n"
            "grace=120\n"
            "\n"
            "[launcher]\n"
            "# Commands for the popup's Launch button, one per line: Label|command\n"
            "# (run through the shell, so ~, quotes and && work). Examples:\n"
            "# Files|thunar ~\n"
            "# Update system|xfce4-terminal -e \"sudo xbps-install -Su\"\n"
            "\n"
            "[weather]\n"
            "# Coordinates for the weather bar: lat|lon (one line, decimal degrees).\n"
            "# Uses Open-Meteo, no account/key needed. The weather bar stays hidden\n"
            "# until this line is set, e.g.:\n"
            "# <latitude>|<longitude>\n"
            "# interval=<minutes> between weather (and alert) refreshes (default 30, minimum 5).\n"
            "#\n"
            "# alerts=true enables MeteoAlarm severe-weather alerts (European\n"
            "# countries covered by MeteoAlarm) for the country/region matching the\n"
            "# coordinates above (reverse-geocoded automatically via OpenStreetMap).\n"
            "# When an alert is active, the matching weather value and the tray badge\n"
            "# pulse in the alert's color.\n"
            "# The app writes country=/region= back into this section once resolved\n"
            "# and never looks them up again; delete both lines to resolve afresh\n"
            "# (e.g. after changing the coordinates).\n"
            "alerts=false\n"
        )


def _read_config_sections():
    """Parses config.conf into {section: [lines]} for the known sections,
    each a list of raw non-comment, non-empty lines under that [section]."""
    sections = {'feeds': [], 'mute': [], 'twitch': [], 'weather': [], 'timer': [], 'youtube': [],
                'updates': [], 'launcher': [], 'actions': [], 'schedule': []}
    current = None
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8', errors='replace') as f:
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


def _load_live_entries(section, fold_case):
    """Parses the channel lines of [twitch] / [youtube]:
    'channel|quality|schedule', the last two optional. Returns
    [(channel, quality_or_None, schedule_text_or_None)], de-duplicated
    case-insensitively (first line wins) in config order. Twitch logins are
    lowercased (fold_case); YouTube identifiers keep the casing as typed."""
    entries, seen = [], set()
    for line in _read_config_sections()[section]:
        if '=' in line:
            continue  # a setting (e.g. interval=10), not a channel
        parts = [p.strip() for p in line.split('|', 2)]
        channel = parts[0].lower() if fold_case else parts[0]
        if not channel or channel.lower() in seen:
            continue
        seen.add(channel.lower())
        quality = parts[1] if len(parts) > 1 and parts[1] else None
        schedule = parts[2] if len(parts) > 2 and parts[2] else None
        entries.append((channel, quality, schedule))
    return entries


def _entry_schedules(entries):
    """{channel: windows} for the entries that have a valid schedule; an
    invalid one is reported (RSS_TRAY_DEBUG) and ignored, i.e. that channel is
    then checked all the time rather than never."""
    schedules = {}
    for channel, _quality, text in entries:
        if not text:
            continue
        try:
            schedules[channel] = parse_schedule(text)
        except ValueError as e:
            _log(f'ignoring schedule for {channel!r}: {e}')
    return schedules


def load_twitch_channels():
    """Lowercase Twitch login names to watch, in config order."""
    return [e[0] for e in _load_live_entries('twitch', True)]


def load_twitch_qualities():
    """{channel: quality} for lines with a quality; callers default the rest
    to 'best'."""
    return {c: q for c, q, _s in _load_live_entries('twitch', True) if q}


def load_twitch_schedules():
    return _entry_schedules(_load_live_entries('twitch', True))


def load_youtube_channels():
    """YouTube channel identifiers to watch, as typed (case-sensitive), e.g.
    '@somehandle' or 'channel/UCxxxxxxxxxxxxxxxxxxxxxx'."""
    return [e[0] for e in _load_live_entries('youtube', False)]


def load_youtube_qualities():
    return {c: q for c, q, _s in _load_live_entries('youtube', False) if q}


def load_youtube_schedules():
    return _entry_schedules(_load_live_entries('youtube', False))


# ---- per-channel check schedules -------------------------------------------
# 'wed 18:00-23:00', 'tue-sun 13:00-', 'mon,wed,fri 20:00-02:00; sat 10:00-12:00'
_DAY_NAMES = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
_ALL_DAYS = frozenset(range(7))
_CLOCK_RE = re.compile(r'^(\d{1,2})[:.](\d{2})$')


def _day_index(token):
    """Index (Monday=0) for 'mon'..'sun' or a longer prefix of the full name."""
    if len(token) >= 3:
        for index, name in enumerate(_DAY_NAMES):
            if name.startswith(token):
                return index
    raise ValueError(f'unknown day {token!r}')


def _parse_days(spec):
    spec = spec.lower().replace(' ', '')
    if spec in ('', 'daily', 'everyday', 'every', '*'):
        return _ALL_DAYS
    if spec == 'weekdays':
        return frozenset(range(5))
    if spec in ('weekend', 'weekends'):
        return frozenset((5, 6))
    days = set()
    for part in spec.split(','):
        if not part:
            raise ValueError(f'bad day list {spec!r}')
        first, dash, last = part.partition('-')
        start = _day_index(first)
        end = _day_index(last) if dash else start
        day = start
        while True:  # ranges may wrap, e.g. fri-mon
            days.add(day)
            if day == end:
                break
            day = (day + 1) % 7
    return frozenset(days)


def _parse_clock(text, allow_24=False):
    m = _CLOCK_RE.match(text.strip())
    if not m:
        raise ValueError(f'bad time {text!r}')
    hours, minutes = int(m.group(1)), int(m.group(2))
    if minutes > 59 or hours > 24 or (hours == 24 and (minutes or not allow_24)):
        raise ValueError(f'bad time {text!r}')
    return hours * 60 + minutes


def _parse_window(text):
    tokens = text.split()
    if not tokens:
        raise ValueError('empty window')
    last = tokens[-1]
    if re.match(r'^\d', last):
        days_spec = ''.join(tokens[:-1])
        start_text, dash, end_text = last.partition('-')
        start = _parse_clock(start_text)
        end = _parse_clock(end_text, allow_24=True) if end_text.strip() else 24 * 60
    else:  # days only: the whole day
        days_spec, start, end = ''.join(tokens), 0, 24 * 60
    return _parse_days(days_spec), start, end


def parse_schedule(text):
    """Parses 'WINDOW; WINDOW; ...' into [(days, start_minute, end_minute)];
    each WINDOW is '[days] HH:MM[-HH:MM]' (':' or '.' as the separator). Days:
    mon..sun (or full names), ranges like tue-sun, lists like mon,wed, daily,
    weekdays, weekends -- omitted means every day. No end time = until the end
    of that day; an end earlier than the start runs past midnight into the
    next day. Raises ValueError on anything it can't read."""
    windows = [_parse_window(part) for part in text.split(';') if part.strip()]
    if not windows:
        raise ValueError('empty schedule')
    return windows


def schedule_active(windows, now):
    """True if the datetime `now` (local time) falls inside any window."""
    minute = now.hour * 60 + now.minute
    weekday = now.weekday()
    for days, start, end in windows:
        if end > start:
            if weekday in days and start <= minute < end:
                return True
        elif (weekday in days and minute >= start) or ((weekday - 1) % 7 in days and minute < end):
            return True  # window runs past midnight
    return False


def channels_due(channels, schedules, live_channels, now):
    """The channels worth checking right now: those without a schedule, those
    inside one of their windows, and any that are currently live (so the end
    of a stream that outlasts its window is still noticed)."""
    return [c for c in channels
            if c not in schedules or c in live_channels or schedule_active(schedules[c], now)]


def scheduled_active_channels(schedules, now):
    """The channels that have a schedule and are inside a window at `now`."""
    return {c for c, windows in schedules.items() if schedule_active(windows, now)}


MIN_TWITCH_CHECK_MINUTES = 1
MIN_YOUTUBE_CHECK_MINUTES = 5  # each channel costs a full streamlink run
MIN_UPDATES_CHECK_HOURS = 1
MIN_WEATHER_REFRESH_MINUTES = 5


def load_launcher_entries():
    """[(label, command)] from the [launcher] section, config order. A line is
    'Label|command' (the command may itself contain '|'); a line without '|'
    is used as both label and command."""
    entries = []
    for line in _read_config_sections()['launcher']:
        label, sep, command = line.partition('|')
        label, command = label.strip(), command.strip()
        if not sep:
            command = label
        if command:
            entries.append((label or command, command))
    return entries


def run_launcher_command(command):
    """Runs `command` through the shell, fully detached from the tray app
    (own session, no stdio), from the home directory. True if it started."""
    try:
        proc = subprocess.Popen(
            ['sh', '-c', command], cwd=os.path.expanduser('~'), start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        _log(f'could not run launcher command {command!r}: {e}')
        return False
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it, no zombie
    return True


def _load_interval(section, default_seconds, minimum, unit_seconds):
    """Seconds from an 'interval=<n>' line in the given section, n counted in
    units of `unit_seconds`. Missing or unparsable -> default; values below
    `minimum` are raised to it (each check hits the network)."""
    for line in _read_config_sections()[section]:
        key, sep, val = line.partition('=')
        if sep and key.strip().lower() == 'interval':
            try:
                return max(minimum, int(val.split('#', 1)[0].strip())) * unit_seconds
            except ValueError:
                return default_seconds
    return default_seconds


def load_twitch_check_interval():
    return _load_interval('twitch', TWITCH_CHECK_INTERVAL_SECONDS, MIN_TWITCH_CHECK_MINUTES, 60)


def load_youtube_check_interval():
    return _load_interval('youtube', YOUTUBE_CHECK_INTERVAL_SECONDS, MIN_YOUTUBE_CHECK_MINUTES, 60)


def load_weather_refresh_interval():
    """Seconds between weather (and alert) refreshes: 'interval=<minutes>' in [weather]."""
    return _load_interval('weather', WEATHER_REFRESH_SECONDS, MIN_WEATHER_REFRESH_MINUTES, 60)


def load_updates_check_interval():
    """Seconds between package update scans: 'interval=<hours>' in [updates]."""
    return _load_interval('updates', PENDING_CHECK_INTERVAL_SECONDS, MIN_UPDATES_CHECK_HOURS, 3600)


DEFAULT_TIMER_MAX_MINUTES = 120
DEFAULT_TIMER_STEP_SECONDS = 60


# ---- timer "Schedule" mode: a recurring action (suspend, power off, ...) --------
DEFAULT_ACTIONS = [('Suspend', 'loginctl suspend'), ('Power off', 'loginctl poweroff')]
SCHEDULE_GLOBAL_DEFAULTS = {'warn': 60, 'snooze': 30, 'grace': 120}
NEW_SCHEDULE = {'enabled': False, 'minute': 30, 'days': _ALL_DAYS, 'action': 'Suspend'}
SCHEDULE_RESUME_GAP_SECONDS = 10  # a bigger wall-clock jump between ticks means the machine slept
SCHEDULE_MISSING_ACTION_SUFFIX = ' (not in [actions])'


def load_actions():
    """[(label, command)] from [actions] (same 'Label|command' lines as the
    launcher), or the built-in Suspend / Power off if the section is empty."""
    entries = []
    for line in _read_config_sections()['actions']:
        label, sep, command = line.partition('|')
        label, command = label.strip(), command.strip()
        if not sep:
            command = label
        if command:
            entries.append((label or command, command))
    return entries or list(DEFAULT_ACTIONS)


def format_days(days):
    """Inverse of _parse_days for the config file: daily, weekdays, weekends
    or a 'mon,wed,fri' list."""
    days = frozenset(days)
    if days == _ALL_DAYS:
        return 'daily'
    if days == frozenset(range(5)):
        return 'weekdays'
    if days == frozenset((5, 6)):
        return 'weekends'
    return ','.join(_DAY_NAMES[d][:3] for d in sorted(days))


def schedule_key(entry):
    """Stable identity of a schedule (its state -- last handled run, snooze --
    is filed under it); editing a schedule therefore starts it afresh."""
    return f"{entry['minute']:04d}|{format_days(entry['days'])}|{entry['action']}"


def format_schedule_line(entry):
    return (f"{'on' if entry['enabled'] else 'off'}|{entry['minute'] // 60:02d}:{entry['minute'] % 60:02d}"
            f"|{format_days(entry['days'])}|{entry['action']}\n")


def _parse_schedule_line(line):
    """'on|00:30|weekdays|Suspend' -> entry dict, or None if unreadable."""
    fields = [f.strip() for f in line.split('|', 3)]
    if len(fields) < 2:
        return None
    try:
        minute = _parse_clock(fields[1])
        days = _parse_days(fields[2]) if len(fields) > 2 and fields[2] else _ALL_DAYS
    except ValueError:
        return None
    if minute >= 24 * 60:
        return None
    return {
        'enabled': fields[0].lower() in ('on', 'true', 'yes', '1'),
        'minute': minute,
        'days': days,
        'action': fields[3] if len(fields) > 3 and fields[3] else NEW_SCHEDULE['action'],
    }


def _is_schedule_entry_line(line):
    return '|' in line and '=' not in line.split('|', 1)[0]


def load_schedule_settings():
    """{'warn' (s), 'snooze' (min), 'grace' (s), 'schedules': [entry, ...]}
    from [schedule]. Entries are 'on|HH:MM|days|action' lines, key=value lines
    hold the shared warn/snooze/grace. A section that still uses the original
    single-schedule keys (enabled=/time=/days=/action=) is read as one entry."""
    settings = dict(SCHEDULE_GLOBAL_DEFAULTS)
    entries, legacy, legacy_seen = [], dict(NEW_SCHEDULE), False
    for line in _read_config_sections()['schedule']:
        if _is_schedule_entry_line(line):
            entry = _parse_schedule_line(line)
            if entry is not None:
                entries.append(entry)
            continue
        key, sep, val = line.partition('=')
        if not sep:
            continue
        key, val = key.strip().lower(), val.split('#', 1)[0].strip()
        try:
            if key in ('warn', 'grace'):
                settings[key] = max(0, int(val))
            elif key == 'snooze':
                settings['snooze'] = max(1, int(val))
            elif key == 'enabled':
                legacy['enabled'], legacy_seen = val.lower() in ('1', 'true', 'yes', 'on'), True
            elif key == 'time':
                minute = _parse_clock(val)
                if minute < 24 * 60:
                    legacy['minute'] = minute
                legacy_seen = True
            elif key == 'days':
                legacy['days'], legacy_seen = _parse_days(val), True
            elif key == 'action' and val:
                legacy['action'], legacy_seen = val, True
        except ValueError:
            pass
    if not entries and legacy_seen:
        entries.append(legacy)
    seen, unique = set(), []
    for entry in entries:  # identical lines would share one identity: keep the first
        if schedule_key(entry) not in seen:
            seen.add(schedule_key(entry))
            unique.append(entry)
    settings['schedules'] = unique
    return settings


def save_schedules(entries):
    """Rewrites the schedule entry lines of [schedule] (and drops the
    original single-schedule keys); comments and the warn/snooze/grace
    settings are kept."""
    def transform(section):
        kept = [l for l in section
                if not _is_schedule_entry_line(l.strip())
                and l.partition('=')[0].strip().lower() not in ('enabled', 'time', 'days', 'action')]
        return kept + [format_schedule_line(e) for e in entries]
    _edit_config_section('schedule', transform)


def schedule_settings_for(settings, entry):
    """One schedule's entry merged with the shared warn/snooze/grace, the
    shape the scheduling functions below take."""
    return dict(entry, warn=settings['warn'], snooze=settings['snooze'], grace=settings['grace'])


def _schedule_occurrences(now_ts, days, minute):
    """(latest occurrence <= now, first occurrence > now) of 'HH:MM on these
    weekdays', as timestamps in local time (DST handled by the C library)."""
    base = datetime.fromtimestamp(now_ts).replace(hour=0, minute=0, second=0, microsecond=0)
    previous = upcoming = None
    for offset in range(-8, 9):
        day = base + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        occurrence = day.replace(hour=minute // 60, minute=minute % 60).timestamp()
        if occurrence <= now_ts:
            previous = occurrence
        elif upcoming is None:
            upcoming = occurrence
    return previous, upcoming


def schedule_pending(now_ts, settings, fired_ts, override):
    """[(occurrence, fire_time)] for the occurrences not handled yet (the
    latest past one -- it may be snoozed -- and the next one). `override` is
    {'occ', 'fire'}: a snooze that moved one occurrence's fire time."""
    pending = []
    for occurrence in _schedule_occurrences(now_ts, settings['days'], settings['minute']):
        if occurrence is None or occurrence <= fired_ts:
            continue
        fire = override['fire'] if override and override.get('occ') == occurrence else occurrence
        pending.append((occurrence, fire))
    return pending


def schedule_status(now_ts, settings, fired_ts, override):
    """('idle'|'warn'|'fire'|'skip', occurrence, seconds_left): what the
    scheduler should do right now. 'skip' = the fire time passed by more than
    `grace` (e.g. the machine was asleep): drop it, don't run it late."""
    for occurrence, fire in schedule_pending(now_ts, settings, fired_ts, override):
        if fire <= now_ts:
            if now_ts - fire <= settings['grace']:
                return 'fire', occurrence, 0
            return 'skip', occurrence, 0
        if settings['warn'] > 0 and fire - now_ts <= settings['warn']:
            return 'warn', occurrence, fire - now_ts
    return 'idle', None, 0


def schedule_snooze_override(now_ts, settings, fired_ts, override):
    """The override that postpones the pending occurrence by `snooze` minutes
    (counted from its current fire time, or from now if that has passed);
    None if nothing is pending."""
    # a stale past occurrence the tick hasn't dropped yet is not worth snoozing
    pending = [p for p in schedule_pending(now_ts, settings, fired_ts, override)
               if p[1] >= now_ts - settings['grace']]
    if not pending:
        return None
    occurrence, fire = pending[0]
    return {'occ': occurrence, 'fire': max(fire, now_ts) + settings['snooze'] * 60}


def load_timer_settings():
    """Returns {'max_seconds', 'step_seconds'} from config.conf's [timer]
    section: 'max=<minutes>' and 'step=<seconds>' key=value lines, in any
    order/combination. Missing or unparsable values fall back to the
    built-in defaults (120 minutes, 60 second steps)."""
    max_minutes = DEFAULT_TIMER_MAX_MINUTES
    step_seconds = DEFAULT_TIMER_STEP_SECONDS
    for line in _read_config_sections()['timer']:
        if '=' not in line:
            continue
        key, _, val = line.partition('=')
        key = key.strip().lower()
        val = val.split('#', 1)[0].strip()  # allow a trailing '#comment'
        if key == 'max':
            try:
                parsed = int(val)
                if parsed > 0:
                    max_minutes = parsed
            except ValueError:
                pass
        elif key == 'step':
            try:
                parsed = int(val)
                if parsed > 0:
                    step_seconds = parsed
            except ValueError:
                pass
    return {'max_seconds': max_minutes * 60, 'step_seconds': step_seconds}


def load_weather_settings():
    """Returns {'lat', 'lon', 'alerts_enabled', 'country', 'region'} from
    config.conf's [weather] section. lat/lon are None until a coordinates line
    is configured. The coordinates line has no '=' ('lat|lon'); everything else
    is a 'key=value' line. 'country' and 'region' are written back by the app
    itself (see _update_weather_region_cache) the first time alerts are
    enabled and the location has been resolved from the coordinates; they are
    never looked up again until the user deletes them."""
    lat = lon = None
    alerts_enabled = False
    country = None
    region = None
    for line in _read_config_sections()['weather']:
        if '=' in line:
            key, _, val = line.partition('=')
            key = key.strip().lower()
            val = val.strip()
            if key == 'alerts':
                alerts_enabled = val.lower() in ('1', 'true', 'yes', 'on')
            elif key == 'country':
                country = val or None
            elif key == 'region':
                region = val or None
        else:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                try:
                    lat, lon = float(parts[0]), float(parts[1])
                except ValueError:
                    pass
    return {
        'lat': lat, 'lon': lon, 'alerts_enabled': alerts_enabled,
        'country': country, 'region': region,
    }


def _edit_config_section(name, transform):
    """Rewrites config.conf in place: `transform` receives the lines of the
    [name] section (without the header; the section is created at the end of
    the file if it's missing) and returns the replacement lines. Every other
    line -- including comments and other sections -- is left untouched.
    Best-effort: failures are swallowed (callers just redo the work later)."""
    try:
        with open(CONFIG_FILE, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except OSError:
        lines = []

    def is_section(line, wanted=None):
        stripped = line.strip()
        if not (stripped.startswith('[') and stripped.endswith(']')):
            return False
        return wanted is None or stripped[1:-1].strip().lower() == wanted

    start = next((i for i, l in enumerate(lines) if is_section(l, name)), None)
    if start is None:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append(f'[{name}]\n')
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if is_section(lines[i])), len(lines))
    lines[start + 1:end] = transform(lines[start + 1:end])

    try:
        tmp = CONFIG_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        pass


def _edit_weather_section(transform):
    _edit_config_section('weather', transform)


_RESOLVED_LOCATION_KEYS = ('country=', 'region=')


def _update_weather_region_cache(region, country):
    """Stores the reverse-geocoded country=/region= in [weather]; they are
    only ever looked up again after the user deletes them."""
    def transform(section):
        kept = [l for l in section if not l.strip().lower().startswith(_RESOLVED_LOCATION_KEYS)]
        return kept + [f'country={country}\n', f'region={region}\n']
    _edit_weather_section(transform)


def _disable_weather_alerts():
    """Writes alerts=false into [weather] (and drops the cached location),
    used when the coordinates resolve to a country MeteoAlarm doesn't cover."""
    def transform(section):
        kept = [l for l in section
                if not l.strip().lower().startswith(_RESOLVED_LOCATION_KEYS)
                and not l.strip().lower().startswith('alerts=')]
        return kept + ['alerts=false\n']
    _edit_weather_section(transform)


def reverse_geocode_region(lat, lon):
    """Best-effort reverse geocode via Nominatim (English names); returns
    (country, region, country_code) -- region being the county, falling back
    to the state, and None if neither exists; country_code is the lowercase
    ISO code Nominatim reports. Returns None on any failure or if no country
    was found. The caller only asks when country/region aren't in the config."""
    url = (
        f"{NOMINATIM_REVERSE_URL}?lat={lat}&lon={lon}"
        "&format=jsonv2&zoom=8&accept-language=en"
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'rss-tray/1.0 (personal use)'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        address = data.get('address') or {}
        country = address.get('country')
        if not country:
            return None
        region = address.get('county') or address.get('state')
        return country, region, (address.get('country_code') or '').upper() or None
    except Exception:
        return None


def meteoalarm_feed_url(country):
    """MeteoAlarm's per-country legacy Atom feed, e.g. 'United Kingdom' ->
    .../meteoalarm-legacy-atom-united-kingdom."""
    slug = re.sub(r'[^a-z0-9]+', '-', (country or '').lower()).strip('-')
    return METEOALARM_FEED_URL_TEMPLATE.format(country=slug)


def is_muted(title, mute_phrases):
    title_lower = title.lower()
    return any(phrase in title_lower for phrase in mute_phrases)


ONLINE_PROBE_TARGETS = [
    ("1.1.1.1", 443),  # Cloudflare
    ("8.8.8.8", 443),  # Google
    ("9.9.9.9", 443),  # Quad9
]


def is_online():
    """Quick check for basic network connectivity: True if ANY of several
    independent, well-known public endpoints accepts a TCP connection on the
    HTTPS port. Raw IPs (no DNS lookup involved), port 443 rather than 53
    (some networks block outbound DNS to arbitrary servers but allow HTTPS),
    and deliberately NOT tied to any of this app's own feature dependencies
    (weather, Twitch, feed hosts) — an outage of one of those shouldn't get
    misread as 'the network isn't up' and stall unrelated startup polling."""
    for host, port in ONLINE_PROBE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            continue
    return False


def run_when_online(fn):
    """Calls fn() on a background thread as soon as the network is up (polling
    every NETWORK_RETRY_SECONDS). is_online() blocks for up to a second per
    probe target, so it must never run on the GTK main loop: while offline
    that froze the tray for seconds at every retry."""
    def work():
        while not is_online():
            time_module.sleep(NETWORK_RETRY_SECONDS)
        fn()
    threading.Thread(target=work, daemon=True).start()


def next_connectivity(online, failures, probe_ok):
    """(online, failures) after one probe: a success means online at once, but
    it takes OFFLINE_AFTER_FAILED_PROBES failures in a row to go offline, so
    one dropped probe doesn't flash the indicator."""
    if probe_ok:
        return True, 0
    failures += 1
    return (online and failures < OFFLINE_AFTER_FAILED_PROBES), failures


def load_state():
    state = {"seen": {}, "unread": []}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state = loaded
        except (json.JSONDecodeError, OSError):
            pass

    # Validate shape — a corrupted/hand-edited state.json shouldn't crash
    # the app elsewhere; reset just the offending key to a sane default.
    if not isinstance(state.get('seen'), dict):
        state['seen'] = {}
    if not isinstance(state.get('unread'), list):
        state['unread'] = []
    if not isinstance(state.get('available_updates'), list):
        state['available_updates'] = []
    if not isinstance(state.get('live_channels'), list):
        state['live_channels'] = []
    if not isinstance(state.get('live_youtube_channels'), list):
        state['live_youtube_channels'] = []
    if not isinstance(state.get('last_checked'), dict):
        state['last_checked'] = {}

    return state


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def entry_id(entry, feed_url):
    """A stable id for entries with a real id/link. Entries lacking both
    (the fallback: title+published) are hashed together with feed_url, so the
    same title+date on two different feeds doesn't collide."""
    raw = entry.get('id') or entry.get('link')
    if raw:
        return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()
    fallback = feed_url + '\x00' + entry.get('title', '') + entry.get('published', '')
    return hashlib.sha1(fallback.encode('utf-8', 'ignore')).hexdigest()


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


def _kill_process_group(proc, grace=5):
    """SIGTERM the whole process group (sudo relays it to the root-owned
    child), then SIGKILL after a grace period. The child may be root-owned,
    so EPERM from killpg is expected and ignored."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return


def parse_xbps_updates(output):
    """Pure parser for `xbps-install -Mn -u` output: returns the pkgnames whose
    action is 'update'. Lines look like
        cryptsetup-2.8.8_1 update x86_64 https://... 3203607 568523"""
    updates = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != 'update':
            continue
        match = re.match(r'^(.+)-[0-9][^-]*$', parts[0])
        if match:
            updates.append(match.group(1))
    return updates


def list_all_updates():
    """Read-only, in-memory, system-wide dry run — no root needed, nothing written
    to disk. Returns a list of pkgnames that have a real newer build published.

    Real xbps-install -Mn -u output (per package) looks like:
        cryptsetup-2.8.8_1 update x86_64 https://repo-default.voidlinux.org/current 3203607 568523
    i.e. "<pkgver> <action> <arch> <repo> <dlsize> <instsize>" — no '->' arrow."""
    try:
        proc = subprocess.Popen(
            ['xbps-install', '-Mn', '-u'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True
        )
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            return None  # None = scan failed (distinct from [] = no updates)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    output = (out or '') + (err or '')
    return parse_xbps_updates(output)


TWITCH_BATCH_SIZE = 20  # operations per GQL request


def _check_twitch_batch(channels):
    """One batched GQL POST for `channels`; {channel: {'title', 'category'}}
    for the live ones, or None if the request failed."""
    payload = json.dumps([
        {
            "operationName": "StreamMetadata",
            "query": "query StreamMetadata($channelLogin: String!) { "
                     "user(login: $channelLogin) { stream { type title game { name } } } }",
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
        return None  # None = request failed (distinct from {} = nobody live)
    if not isinstance(results, list):
        return None
    live = {}
    for channel, result in zip(channels, results):
        user = (result.get('data') or {}).get('user')
        if not user:
            continue
        stream = user.get('stream')
        if stream and stream.get('type') == 'live':
            live[channel] = {
                'title': stream.get('title') or '',
                'category': (stream.get('game') or {}).get('name') or '',
            }
    return live


def check_twitch_live_channels(channels):
    """Uses Twitch's internal (unofficial) GraphQL API — the same one twitch.tv
    itself uses for logged-out visitors — so no app registration/secret is
    needed. Undocumented; could break if Twitch changes their internal schema.
    Channels are batched into JSON-array POSTs of TWITCH_BATCH_SIZE (one
    request per channel would be wasteful, one giant request risks rejection).
    Returns {channel: {'title', 'category'}} for whichever channels are
    currently live; a failed/offline channel is simply absent from the
    result, not marked False. Returns None if any request failed, so the
    caller keeps its last known state rather than acting on a partial view."""
    live = {}
    for start in range(0, len(channels), TWITCH_BATCH_SIZE):
        batch = _check_twitch_batch(channels[start:start + TWITCH_BATCH_SIZE])
        if batch is None:
            return None
        live.update(batch)
    return live


# --- Live-stream playback -------------------------------------------------
# One persistent mpv window is shared by every live channel. mpv is started
# idle + maximized with an IPC socket; each click starts `streamlink
# --player-external-http` (serves the stream on a local port, no player of its
# own) and tells the running mpv to `loadfile` that URL, so a new channel
# replaces whatever is playing in the *same* window instead of opening another.
MPV_IPC_SOCKET = os.path.join(
    os.environ.get('XDG_RUNTIME_DIR') or '/tmp', f'rss-tray-mpv-{os.getuid()}.sock')
STREAMLINK_READY_TIMEOUT_SECONDS = 30
MPV_START_TIMEOUT_SECONDS = 10

_player_lock = threading.Lock()  # guards _streamlink_proc
_mpv_lock = threading.Lock()     # serialises "is mpv up? if not, start it"
_streamlink_proc = None
_mpv_proc = None
_playing = None  # (streamlink_proc, site, channel) of the stream mpv is showing


def is_stream_playing(site, channel):
    """True while `channel` on `site` ('Twitch'/'YouTube') is what the shared
    mpv is playing (its streamlink is still running)."""
    with _player_lock:
        playing = _playing
    return bool(playing and playing[1:] == (site, channel) and playing[0].poll() is None)


def live_marker_markup(color, playing):
    """The colored bullet in front of a live row; a play triangle (forced to
    its text form, not the emoji one) while that stream is playing."""
    symbol = '\u25b6\ufe0e' if playing else '\u25cf'
    return f'<span foreground="{color}"><b>{symbol}</b></span>'


def stream_window_title(site, channel, title=None):
    """mpv window title: site, user and stream title separated by bullets."""
    return ' \u00b7 '.join(p for p in (site, channel, title) if p)


def _log(message):
    """Diagnostics to stderr, silent unless RSS_TRAY_DEBUG is set (e.g.
    `RSS_TRAY_DEBUG=1 python3 ~/.local/bin/rss-tray.py`)."""
    if os.environ.get('RSS_TRAY_DEBUG'):
        print(f'rss-tray: {message}', file=sys.stderr, flush=True)


def _mpv_ipc(commands, timeout=2.0):
    """Sends mpv JSON-IPC commands (each a list, e.g. ['loadfile', url,
    'replace']) and waits for one reply per command. True only if mpv is
    reachable and every command answered 'success'."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(MPV_IPC_SOCKET)
        for command in commands:
            sock.sendall((json.dumps({'command': command}) + '\n').encode())
        pending = len(commands)
        ok = True
        buf = b''
        while pending > 0:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            buf += chunk
            *lines, buf = buf.split(b'\n')
            for line in lines:
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if 'error' in reply:  # events have no 'error' key
                    pending -= 1
                    if reply['error'] != 'success':
                        ok = False
        return ok
    except OSError:
        return False
    finally:
        sock.close()


def _ensure_mpv():
    """True if an mpv with our IPC socket is running, starting one (idle,
    maximized) if not. --idle=once: it sits empty until the first stream,
    and quits when that playback ends, so the next click starts a fresh one."""
    global _mpv_proc
    with _mpv_lock:
        if _mpv_ipc([['get_property', 'pid']]):
            return True
        try:
            os.unlink(MPV_IPC_SOCKET)  # stale socket from a dead mpv
        except OSError:
            pass
        try:
            _mpv_proc = subprocess.Popen(
                ['mpv', '--idle=once', '--force-window=yes',
                 '--window-maximized=yes', f'--input-ipc-server={MPV_IPC_SOCKET}'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return False
        deadline = time_module.monotonic() + MPV_START_TIMEOUT_SECONDS
        while time_module.monotonic() < deadline:
            if _mpv_proc.poll() is not None:
                return False
            if _mpv_ipc([['get_property', 'pid']], timeout=0.5):
                return True
            time_module.sleep(0.2)
        return False


def _load_in_mpv(stream_url, title):
    """Plays stream_url in the shared mpv window (replacing what's there)."""
    for _attempt in range(2):  # 2nd try covers mpv quitting right after the probe
        if not _ensure_mpv():
            return False
        if _mpv_ipc([['set_property', 'force-media-title', title],
                     ['loadfile', stream_url, 'replace']]):
            return True
        time_module.sleep(0.5)
    return False


def _stop_streamlink():
    global _streamlink_proc
    with _player_lock:
        proc, _streamlink_proc = _streamlink_proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


atexit.register(_stop_streamlink)


def _is_current_streamlink(proc):
    with _player_lock:
        return _streamlink_proc is proc


def _free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _apply_stream_title(proc, url, site, channel):
    """Refines the window title with the author's display name and stream title
    from `streamlink --json` (external-http mode has no {author}/{title}
    substitution of its own): '<site> \u00b7 <author> \u00b7 <title>'."""
    try:
        result = subprocess.run(['streamlink', '--json', url],
                                capture_output=True, text=True,
                                timeout=YOUTUBE_CHECK_TIMEOUT_SECONDS)
        meta = json.loads(result.stdout).get('metadata') or {}
    except Exception:
        return
    title = stream_window_title(site, meta.get('author') or channel, meta.get('title'))
    if meta and _is_current_streamlink(proc):
        _mpv_ipc([['set_property', 'force-media-title', title]])


def _stop_streamlink_when_mpv_exits(proc):
    """The external-http server would otherwise serve forever after the mpv
    window is closed; stop it once mpv has been gone for two checks."""
    misses = 0
    while proc.poll() is None:
        time_module.sleep(3)
        if _mpv_ipc([['get_property', 'pid']], timeout=1):
            misses = 0
            continue
        misses += 1
        if misses >= 2:
            try:
                proc.terminate()
            except OSError:
                pass
            return


def _play_in_shared_mpv(url, quality, site, channel, fallback_url, title=None):
    """Worker thread: serve the stream via streamlink, load it into the
    shared mpv. Falls back to the browser if anything fails (unless a newer
    click has superseded this one).

    The previous stream's streamlink is deliberately left running until mpv
    has been told to load the new one: killing it first would end the file
    mpv is playing, and with --idle=once mpv quits when playback ends -- the
    window would close and reopen instead of being reused."""
    port = _free_local_port()
    try:
        proc = subprocess.Popen(
            ['streamlink', '--player-external-http',
             '--player-external-http-interface', '127.0.0.1',
             '--player-external-http-port', str(port), url, quality],
            # streamlink logs to stdout in some versions and stderr in others;
            # read both through one pipe
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='replace')
    except Exception as e:
        _log(f'could not start streamlink: {e}')
        webbrowser.open(fallback_url)
        return
    global _streamlink_proc
    with _player_lock:
        previous, _streamlink_proc = _streamlink_proc, proc
    # streamlink logs this line once its local server is listening; mpv can
    # connect from then on (it fetches the actual stream on first request).
    watchdog = threading.Timer(STREAMLINK_READY_TIMEOUT_SECONDS, proc.kill)
    watchdog.start()
    ready = False
    recent = []  # last few log lines, for the failure message
    try:
        for line in proc.stdout:
            recent = (recent + [line.strip()])[-4:]
            if 'access with one of' in line:
                ready = True
                break
    finally:
        watchdog.cancel()
    loaded = ready and _load_in_mpv(f'http://127.0.0.1:{port}/',
                                    stream_window_title(site, channel, title))
    if not loaded:
        superseded = not _is_current_streamlink(proc)
        if not superseded:
            with _player_lock:  # the previous stream (if still alive) stays current
                _streamlink_proc = previous if previous is not None and previous.poll() is None else None
            if not ready:
                _log('streamlink never reported its local server; last output: ' + ' | '.join(recent))
            else:
                _log('streamlink is serving, but mpv could not be started or controlled')
        try:
            proc.terminate()
        except OSError:
            pass
        if not superseded:
            webbrowser.open(fallback_url)
        return
    global _playing
    with _player_lock:
        _playing = (proc, site, channel)
    if previous is not None and previous.poll() is None:
        try:
            previous.terminate()  # mpv has already switched over to the new stream
        except OSError:
            pass
    threading.Thread(target=_apply_stream_title, args=(proc, url, site, channel), daemon=True).start()
    threading.Thread(target=_stop_streamlink_when_mpv_exits, args=(proc,), daemon=True).start()
    for _line in proc.stdout:  # drain so streamlink never blocks on a full pipe
        pass


def _open_live_stream(url, quality, site, channel, fallback_url, title=None):
    if shutil.which('streamlink') and shutil.which('mpv'):
        try:
            threading.Thread(
                target=_play_in_shared_mpv,
                args=(url, quality, site, channel, fallback_url, title),
                daemon=True).start()
            return
        except Exception:
            pass
    webbrowser.open(fallback_url)


def open_twitch_stream(channel, quality='best', site='Twitch', title=None):
    """Plays the stream in the shared, maximized mpv window (see above) if
    streamlink and mpv are installed; falls back to opening the channel in
    the browser otherwise, or if starting playback fails."""
    _open_live_stream(f'twitch.tv/{channel}', quality, site, channel,
                      f'https://twitch.tv/{channel}', title)


YOUTUBE_CHECK_INTERVAL_SECONDS = 900  # how often to poll YouTube live status
YOUTUBE_CHECK_TIMEOUT_SECONDS = 20  # per-channel; this check shells out to
                                     # streamlink itself (no lightweight
                                     # keyless batch API exists for YouTube),
                                     # so it's checked sequentially, one
                                     # subprocess per channel


def _parse_streamlink_json(output):
    """json.loads() on `streamlink --json` output, tolerating log lines before
    the object (some streamlink versions print their log to stdout)."""
    start = output.find('{')
    if start < 0:
        raise ValueError('no JSON object in output')
    return json.loads(output[start:])


def check_youtube_live_channels(channels):
    """Uses `streamlink --json <channel>/live` per channel -- the same
    extraction path used for actual playback, so there's no risk of this
    check disagreeing with what clicking the row would actually do. No
    keyless YouTube API exists for batch-checking many channels in one
    request, so this is sequential, one streamlink invocation per channel.
    A channel is live if streamlink found playable streams for it (title and
    category are optional and may be empty).
    Returns {channel: {'title', 'category'}} for whichever channels are
    currently live (a not-live channel is simply absent, same convention as
    Twitch's check);
    returns None only if every single channel's check failed (e.g.
    streamlink isn't installed) -- an individual channel's request failing
    is treated the same as that channel not being live, and is simply
    retried next poll. Failures are logged to stderr for diagnosis."""
    if not channels:
        return {}
    live = {}
    any_success = False
    for channel in channels:
        url = f'https://www.youtube.com/{channel}/live'
        try:
            result = subprocess.run(
                ['streamlink', '--json', url],
                capture_output=True, text=True, timeout=YOUTUBE_CHECK_TIMEOUT_SECONDS
            )
            parsed = _parse_streamlink_json(result.stdout)
        except Exception as e:
            _log(f'YouTube check for {channel} failed: {type(e).__name__}: {e}')
            continue  # this channel's check failed; left absent, retried next poll
        any_success = True
        metadata = parsed.get('metadata') or {}
        if parsed.get('error'):
            _log(f"YouTube {channel}: not live ({str(parsed['error'])[:160]})")
        elif parsed.get('streams') or metadata.get('title'):
            live[channel] = {
                'title': metadata.get('title') or '',
                'category': metadata.get('category') or '',
            }
    if not any_success:
        return None  # every channel's check failed (e.g. streamlink missing)
    return live


def open_youtube_stream(channel, quality='best', title=None):
    """Same as open_twitch_stream (shared mpv window); falls back to the
    channel's page in the browser."""
    _open_live_stream(f'https://www.youtube.com/{channel}/live', quality,
                      'YouTube', channel, f'https://www.youtube.com/{channel}', title)


def play_notification_sound():
    """Best-effort: plays notification.wav from the config directory with
    whichever player is available. Silently does nothing if the file or every
    player is missing."""
    if not os.path.exists(NOTIFICATION_SOUND_FILE):
        return
    for player in (['paplay'], ['aplay', '-q'],
                   ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet']):
        if shutil.which(player[0]):
            try:
                subprocess.Popen(player + [NOTIFICATION_SOUND_FILE],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    """Opens `path` in the user's default application for its type -- the one
    set in the desktop's settings (e.g. XFCE's Default Applications). Tried in
    order: GIO (in-process, needs nothing beyond GTK itself), xdg-open, and
    finally $EDITOR in a terminal."""
    try:
        if Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None):
            return
    except GLib.Error as e:
        _log(f'GIO could not open {path}: {e.message}')
    if shutil.which('xdg-open'):
        try:
            subprocess.Popen(['xdg-open', path])
            return
        except OSError as e:
            _log(f'xdg-open failed: {e}')
    editor = os.environ.get('EDITOR', 'vi')
    if shutil.which('xfce4-terminal'):
        subprocess.Popen(['xfce4-terminal', '-e', f'{editor} "{path}"'])
    else:
        try:
            subprocess.Popen([editor, path])
        except Exception:
            print(f"Couldn't open an editor — edit manually: {path}")


ALERT_HAZARD_SEGMENT_MAP = (
    # (substring to look for in the hazard text, lowercased) -> segment key.
    # Checked in order; first match wins. 'glyph' recolors the weather-icon
    # glyph in the temp segment; the others recolor that value's own label.
    ('wind', 'wind'),
    ('temperature', 'hilo'),
    ('snow', 'glyph'),
    ('ice', 'glyph'),
    ('rain', 'rain'),
    ('thunderstorm', 'rain'),
    ('flood', 'rain'),
    ('coastal', 'rain'),
)


def _hazard_to_segment(hazard_text):
    h = hazard_text.lower()
    for needle, segment in ALERT_HAZARD_SEGMENT_MAP:
        if needle in h:
            return segment
    return None  # e.g. Fog, Forest fire, Avalanches -- badge-only, no matching value


_METEOALARM_TITLE_RE = re.compile(
    r'^\s*\S+\s+(.+?)\s+Warning\s+issued\s+for\s+.+?\s+-\s+(.+?)\s*$', re.IGNORECASE
)


def parse_meteoalarm_title(title):
    """Splits a MeteoAlarm entry title like 'Yellow Wind Warning issued for
    <country> - <region>' into (hazard, region). Returns (None, None) if the
    title doesn't match the expected shape."""
    m = _METEOALARM_TITLE_RE.match(title or '')
    if not m:
        return None, None
    return m.group(1), m.group(2)


ALERT_SEVERITY_RANK = {'yellow': 1, 'orange': 2, 'red': 3}

# (bright, dim) hex pairs for weather-bar text/glyph coloring, and (bright, dim)
# RGBA tuples for the tray badge, per awareness color.
ALERT_TEXT_COLORS = {
    'yellow': ('#c9a600', '#6b5800'),
    'orange': ('#d97300', '#6b3900'),
    'red': ('#cc0000', '#7a1414'),
}
ALERT_BADGE_COLORS = {
    'yellow': ((0.90, 0.75, 0.0, 1), (0.55, 0.46, 0.0, 1)),
    'orange': ((0.90, 0.45, 0.0, 1), (0.55, 0.28, 0.0, 1)),
    'red': ((0.90, 0.05, 0.05, 1), (0.50, 0.05, 0.05, 1)),
}


def _severity_to_color(cap_severity):
    """Maps a CAP severity string to MeteoAlarm's own awareness-level color:
    Moderate=yellow, Severe=orange, Extreme=red. Minor or an unrecognized
    value defaults to yellow (the least visually urgent), since overstating
    urgency for an unrecognized value is worse than understating it."""
    s = (cap_severity or '').strip().lower()
    if s == 'extreme':
        return 'red'
    if s == 'severe':
        return 'orange'
    return 'yellow'


def _parse_meteoalarm_time(value):
    """Parses a MeteoAlarm CAP timestamp like '2026-09-28T17:00:00+00:00'.
    Returns None if missing or unparsable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fetch_meteoalarm_alerts(region, country, now=None):
    """Fetches `country`'s MeteoAlarm feed and returns the entries whose area
    matches `region` (case-insensitive) AND whose time window currently
    covers `now` (defaults to the real current time), as a list of
    {'hazard', 'segment', 'title', 'expires', 'severity_color'} dicts.
    Returns None on a feed fetch failure (distinct from [] = feed fetched
    fine, nothing active for this region right now).

    The feed can retain entries past their own expiry (observed in
    practice), so onset/expires are checked here rather than trusting
    presence in the feed alone. Missing or unparsable timestamps err on the
    side of showing the alert rather than hiding a possibly-real one."""
    try:
        parsed = feedparser.parse(meteoalarm_feed_url(country))
    except Exception:
        return None
    if getattr(parsed, 'bozo', False) and not parsed.entries:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    region_lower = (region or '').strip().lower()
    alerts = []
    for entry in parsed.entries:
        if entry.get('cap_areadesc', '').strip().lower() != region_lower:
            continue
        hazard, _area = parse_meteoalarm_title(entry.get('title', ''))
        if not hazard:
            continue
        onset = (
            _parse_meteoalarm_time(entry.get('cap_onset'))
            or _parse_meteoalarm_time(entry.get('cap_effective'))
        )
        if onset is not None and now < onset:
            continue  # not started yet
        expires = _parse_meteoalarm_time(entry.get('cap_expires'))
        if expires is not None and now > expires:
            continue  # expired
        alerts.append({
            'hazard': hazard,
            'segment': _hazard_to_segment(hazard),
            'title': entry.get('title', ''),
            'expires': entry.get('cap_expires', ''),
            'severity_color': _severity_to_color(entry.get('cap_severity')),
        })
    return alerts


def fetch_weather():
    """Best-effort fetch from Open-Meteo. Returns a dict or None on any failure."""
    settings = load_weather_settings()
    lat, lon = settings['lat'], settings['lon']
    if lat is None or lon is None:
        return None  # no coordinates configured: weather bar stays hidden
    url = build_weather_api_url(lat, lon)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
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
    """Maps a WMO weather_code to a plain Unicode glyph (rendered via
    mono_glyph_markup, never as a color emoji).
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


WEATHER_GLYPH_COLOR = '#2b2b2b'


# DejaVu Sans' cloud is small and sits low (its top is ~0.36em above the
# baseline, vs ~0.74em for the digits and the sun/snow/bolt glyphs), so it's
# lifted by this many em to line up with the others (value tuned against a
# real screenshot of the bar).
GLYPH_RISE_EM = {'\u2601': 0.52}


def glyph_rise_units(glyph, widget=None):
    """Pango `rise` (1/1024 pt) that top-aligns `glyph`, or 0 if it needs none.
    The glyph's span is size="large" (1.2x), so the base font size of
    `widget` (10pt if unknown) is scaled accordingly."""
    em = GLYPH_RISE_EM.get(glyph)
    if not em:
        return 0
    size = 10 * 1024
    try:
        size = widget.get_pango_context().get_font_description().get_size() or size
    except Exception:
        pass
    return int(em * 1.2 * size)


def rise_spacer_markup(rise):
    """Zero-width run with the same font and rise as a raised glyph. Appended
    to the other values so every label gets the exact same line height (and so
    the same text baseline) as the one holding the raised glyph."""
    if not rise:
        return ''
    return f'<span font_family="DejaVu Sans" rise="{rise}">\u200b</span>'


def weather_row_pad(widget=None):
    """Zero-width raised run appended to every value on both weather slides,
    so all labels (and both slides) get identical line height and therefore
    identical text position, whether or not a raised glyph is on screen."""
    return rise_spacer_markup(glyph_rise_units('\u2601', widget))


def mono_glyph_markup(glyph, color=WEATHER_GLYPH_COLOR, rise=0):
    """Weather glyph forced to the monochrome text form (U+FE0E variation
    selector + a text font), so it's drawn in `color` and stays readable on
    the light weather bar instead of becoming a pale color emoji."""
    rise_attr = f' rise="{rise}"' if rise else ''
    return (f'<span foreground="{color}" font_family="DejaVu Sans"{rise_attr}>'
            f'{glyph}\ufe0e</span>')


# The tray icon draws the degree sign as a small stroked ring (a text glyph
# would be too tiny/blurry at 24px and would eat into the digits' width).
TEMP_RING_RADIUS = 1.0
TEMP_RING_LINE_WIDTH = 1.0
TEMP_RING_GAP = 0.4
TEMP_RING_RESERVE = TEMP_RING_GAP + 2 * TEMP_RING_RADIUS + TEMP_RING_LINE_WIDTH


def temp_icon_layout(size, text_width, text_height):
    """Horizontal start of the digits and centre of the degree ring so that
    digits + ring are centred together in a `size`-px icon, ring top level
    with the digits' top. Returns (digits_left, ring_cx, ring_cy)."""
    total = text_width + TEMP_RING_RESERVE
    digits_left = (size - total) / 2
    ring_outer = 2 * TEMP_RING_RADIUS + TEMP_RING_LINE_WIDTH
    ring_cx = digits_left + text_width + TEMP_RING_GAP + ring_outer / 2
    ring_cy = max(size / 2 - text_height / 2 + ring_outer / 2, ring_outer / 2)
    return digits_left, ring_cx, ring_cy


# `background:` (the shorthand), not `background-color:`, on everything that
# makes up the list area: many themes paint a background-image (gradient or
# solid) on lists/viewports, and that image is drawn over a plain
# background-color -- the whole news area then turns black/dark with the rows
# sitting on top of it. The shorthand also resets the image. Text colour is
# forced too, since a dark theme's light foreground would be invisible on white.
OFFLINE_BANNER_TEXT = "Offline"
LAUNCHER_MAX_HEIGHT_PX = 300  # the Launch list scrolls beyond this
LIST_BOTTOM_SPACE_PX = 85  # reserved under the last row for the timer slide (countdown mode)
TIMER_HEIGHT_COUNTDOWN_PX = 60
TIMER_HEIGHT_SCHEDULE_PX = 134
TIMER_SPACER_EXTRA_PX = 25  # gap kept above the slide

POPUP_CSS = """
list row { padding: 1px 3px; min-height: 0px; }
button { padding: 1px; }
list, viewport, scrolledwindow, overlay, .popup-content { background: #ffffff; }
list, list label { color: #000000; }
.weather-bar { background: #e8eef5; }
.offline-banner { background: #b3261e; color: #ffffff; padding: 3px 6px; font-weight: bold; }
.schedule-warning { background: #b3261e; padding: 3px 6px; }
.schedule-warning label { color: #ffffff; font-weight: bold; }
.schedule-warning button label { color: #000000; font-weight: normal; }
.weather-bar label, .timer-bar label { color: #000000; }
.timer-bar { background: #ffffff; }
"""


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
        self._youtube_lock = threading.Lock()  # guards overlapping YouTube-check cycles
        self.popup = None
        self.listbox = None
        self.scroller = None
        self._launcher_open = False
        self.online = True            # last known connectivity (see probe_connectivity)
        self._probe_failures = 0
        self._probing = False
        self.offline_banner = None
        self.weather_data = None
        self.weather_box = None
        self.weather_view = 'today'
        self.active_alerts = []
        self._alert_pulse_counter = 0
        self._last_pulse_signature = None
        self.active_installs = 0
        self.install_status = {}  # pkgname -> 'Waiting…'/'Downloading…'/'Installing…'/'Done'/'Failed'
        self.timer_remaining_seconds = 0
        self.timer_running = False
        self.timer_deadline = None  # time_module.monotonic() value at which the timer fires
        self._timer_updating_ui = False
        self.timer_scale = None
        self.timer_label = None
        self.timer_box = None
        self.timer_visible = False
        self.timer_mode = 'countdown'     # or 'schedule' (the slide's two modes)
        self._mode_updating = False
        self.mode_buttons = {}
        self.countdown_box = None
        self.schedule_box = None
        self.sched = None                 # widgets of the Schedule controls (per popup)
        self.sched_index = 0              # which schedule the controls show
        self._sched_updating = False
        self.list_spacer = None
        self.schedule_banner = None
        self.schedule_banner_label = None
        self.schedule_warning = None      # {'occ', 'left', 'action'} while the pre-action warning shows
        self._schedule_flash_on = False
        self._last_schedule_tick = None
        self._schedule_cache = (None, None)
        self._schedule_resync = True

        self._apply_compact_css()

        self.status_icon = Gtk.StatusIcon()
        self.status_icon.connect('activate', self.toggle_popup)
        self.status_icon.connect('popup-menu', self.toggle_popup)
        self.update_icon()

        GLib.timeout_add_seconds(1, self.initial_check)
        GLib.timeout_add_seconds(SCHEDULER_TICK_SECONDS, self.periodic_check)
        GLib.timeout_add(800, self.maybe_auto_show_startup)
        GLib.timeout_add_seconds(2, self.initial_weather_check)
        GLib.timeout_add_seconds(load_weather_refresh_interval(), self.periodic_weather_check)
        GLib.timeout_add(ALERT_PULSE_INTERVAL_MS, self._alert_pulse_tick)
        GLib.timeout_add_seconds(3, self.initial_twitch_check)
        GLib.timeout_add_seconds(load_twitch_check_interval(), self.periodic_twitch_check)
        GLib.timeout_add_seconds(4, self.initial_youtube_check)
        GLib.timeout_add_seconds(load_youtube_check_interval(), self.periodic_youtube_check)
        GLib.timeout_add_seconds(1, self._timer_tick)
        GLib.timeout_add_seconds(1, lambda: self.probe_connectivity() and False)
        GLib.timeout_add_seconds(OFFLINE_PROBE_SECONDS, self.probe_connectivity)
        self._scheduled_active = self._current_scheduled_active()
        GLib.timeout_add_seconds(60, self._live_schedule_tick)

    @staticmethod
    def _current_scheduled_active(now=None):
        now = now or datetime.now()
        return {
            'twitch': scheduled_active_channels(load_twitch_schedules(), now),
            'youtube': scheduled_active_channels(load_youtube_schedules(), now),
        }

    def _live_schedule_tick(self):
        """Once a minute (no network): when a scheduled channel's window has
        just opened, check right away instead of waiting for the next
        interval, so a stream starting on time shows up on time."""
        current = self._current_scheduled_active()
        opened = {platform: current[platform] - self._scheduled_active[platform] for platform in current}
        self._scheduled_active = current
        if opened['twitch']:
            self.start_twitch_check()
        if opened['youtube']:
            self.start_youtube_check()
        return True

    def maybe_auto_show_startup(self):
        if self.has_anything_to_show():
            self.show_popup(auto=True)
        return False

    def initial_check(self):
        run_when_online(lambda: self.start_check_thread(force=True))
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
        if not self.online and not force:
            return  # offline: nothing could be fetched, and a failed fetch must not count as a check
        feeds = load_feeds()
        mute_phrases = load_mute_filters()
        new_items = []
        now = time_module.time()
        with self.lock:
            seen_ids = set(self.state.get('seen', {}).keys())
            last_checked = dict(self.state.get('last_checked', {}))
        newly_seen_ids = set()
        due_urls = []
        intervals = {}
        for url, _custom_name, interval_seconds in feeds:
            intervals[url] = interval_seconds
            last = last_checked.get(url, 0)
            if force or (now - last) >= interval_seconds:
                due_urls.append(url)
        for url in due_urls:
            retry_soon = now - intervals[url] + min(intervals[url], FEED_RETRY_SECONDS)
            try:
                parsed = feedparser.parse(url)
            except Exception:
                last_checked[url] = retry_soon
                continue
            if getattr(parsed, 'bozo', False) and not parsed.entries:
                last_checked[url] = retry_soon  # fetch/parse failed
                continue
            last_checked[url] = now
            for entry in parsed.entries:
                eid = entry_id(entry, url)
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
                    self.state['unread'] = (new_items + self.state.get('unread', []))[:MAX_UNREAD_ITEMS]
            save_state(self.state)
        if new_items:
            GLib.idle_add(self.on_new_items)

    def check_updates_if_due(self, force=False):
        now = time_module.time()
        with self.lock:
            last = self.state.get('updates_last_checked', 0)
            last_attempt = self.state.get('updates_last_attempt', 0)
            fails = self.state.get('updates_fail_count', 0)
        if not force:
            interval = load_updates_check_interval()
            if (now - last) < interval:
                return
            if fails and (now - last_attempt) < min(300 * 2 ** (fails - 1), interval):
                return  # backing off after failed scans
        if not self._xbps_lock.acquire(blocking=False):
            return  # an install is running: scanning mid-transaction would give a wrong list
        try:
            pkgnames = list_all_updates()
        finally:
            self._xbps_lock.release()
        if pkgnames is None:
            with self.lock:
                self.state['updates_last_attempt'] = now
                self.state['updates_fail_count'] = min(fails + 1, 10)
                save_state(self.state)
            return  # keep the previous available_updates untouched
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
            self.state['updates_fail_count'] = 0
            save_state(self.state)
        if promoted_new:
            GLib.idle_add(self.on_new_items)  # reuse: sound + auto-popup + icon refresh

    def on_new_items(self):
        self.update_icon()
        play_notification_sound()
        self.show_popup(auto=True)  # auto-open whenever new unread items, updates, or live channels arrive
        return False

    def initial_twitch_check(self):
        run_when_online(self.start_twitch_check)
        return False

    def periodic_twitch_check(self):
        self.start_twitch_check()
        # re-arm with the interval currently in config.conf, so edits apply
        # after the next check without restarting the app
        GLib.timeout_add_seconds(load_twitch_check_interval(), self.periodic_twitch_check)
        return False

    def start_twitch_check(self):
        if not self._twitch_lock.acquire(blocking=False):
            return  # a check is already in flight — skip this tick
        threading.Thread(target=self._check_twitch_guarded, daemon=True).start()

    def _check_twitch_guarded(self):
        try:
            channels = load_twitch_channels()
            if channels:
                with self.lock:
                    live = {e['channel'] for e in self.state.get('live_channels', [])}
                due = channels_due(channels, load_twitch_schedules(), live, datetime.now())
                live_now = check_twitch_live_channels(due)
                if live_now is not None:  # failed check: keep last known live state
                    GLib.idle_add(self._on_twitch_checked, live_now)
        finally:
            self._twitch_lock.release()

    def _on_twitch_checked(self, live_now):
        with self.lock:
            was_live = {e['channel'] for e in self.state.get('live_channels', [])}
            self.state['live_channels'] = [
                {'channel': ch, **live_now[ch]} for ch in sorted(live_now)
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

    def initial_youtube_check(self):
        run_when_online(self.start_youtube_check)
        return False

    def periodic_youtube_check(self):
        self.start_youtube_check()
        GLib.timeout_add_seconds(load_youtube_check_interval(), self.periodic_youtube_check)
        return False

    def start_youtube_check(self):
        if not self._youtube_lock.acquire(blocking=False):
            return  # a check is already in flight — skip this tick
        threading.Thread(target=self._check_youtube_guarded, daemon=True).start()

    def _check_youtube_guarded(self):
        try:
            channels = load_youtube_channels()
            if channels:
                with self.lock:
                    live = {e['channel'] for e in self.state.get('live_youtube_channels', [])}
                due = channels_due(channels, load_youtube_schedules(), live, datetime.now())
                live_now = check_youtube_live_channels(due)
                if live_now is not None:  # failed check: keep last known live state
                    GLib.idle_add(self._on_youtube_checked, live_now)
        finally:
            self._youtube_lock.release()

    def _on_youtube_checked(self, live_now):
        with self.lock:
            was_live = {e['channel'] for e in self.state.get('live_youtube_channels', [])}
            self.state['live_youtube_channels'] = [
                {'channel': ch, **live_now[ch]} for ch in sorted(live_now)
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
        run_when_online(self.start_weather_fetch)
        return False

    def periodic_weather_check(self):
        self.start_weather_fetch()
        # re-arm with the interval currently in config.conf (edits apply without a restart)
        GLib.timeout_add_seconds(load_weather_refresh_interval(), self.periodic_weather_check)
        return False

    def start_weather_fetch(self):
        threading.Thread(target=self._fetch_weather_bg, daemon=True).start()

    def _fetch_weather_bg(self):
        data = fetch_weather()
        alerts = self._fetch_alerts_bg()
        GLib.idle_add(self._on_weather_fetched, data, alerts)

    def _fetch_alerts_bg(self):
        """Fetches active MeteoAlarm alerts for the country/region in
        config.conf, resolving them from the coordinates (once) if they are
        missing. Returns None on failure (caller keeps the previous alerts),
        [] if alerts are disabled or the region genuinely has nothing active."""
        settings = load_weather_settings()
        if not settings['alerts_enabled'] or settings['lat'] is None or settings['lon'] is None:
            return []
        country, region = settings['country'], settings['region']
        if not (country and region):
            resolved = reverse_geocode_region(settings['lat'], settings['lon'])
            if not resolved:
                return None  # lookup failed; retried on the next poll
            country, region, code = resolved
            if code and code not in METEOALARM_COUNTRY_CODES:
                _disable_weather_alerts()  # no MeteoAlarm feed for this country
                return []
            if not (country and region):
                return None
            _update_weather_region_cache(region, country)
        return fetch_meteoalarm_alerts(region, country)

    def _on_weather_fetched(self, data, alerts=None):
        if data is not None:
            self.weather_data = data
        if alerts is not None:
            self.active_alerts = alerts
        self.rebuild_weather_bar()
        self.update_icon()
        return False

    def has_active_alerts(self):
        return bool(self.active_alerts)

    def _segment_alert_colors(self):
        """{segment_key: color_name}, using the worst-severity alert
        targeting each segment when more than one does."""
        info = {}
        for a in self.active_alerts:
            seg = a.get('segment')
            if not seg:
                continue
            color = a.get('severity_color', 'yellow')
            if seg not in info or ALERT_SEVERITY_RANK[color] > ALERT_SEVERITY_RANK[info[seg]]:
                info[seg] = color
        return info

    def _worst_alert_color(self):
        colors = [a.get('severity_color', 'yellow') for a in self.active_alerts]
        if not colors:
            return None
        return max(colors, key=lambda c: ALERT_SEVERITY_RANK.get(c, 0))

    def _pulse_on_for(self, color_name):
        """Whether `color_name`'s pulse is in its bright phase right now.
        Extreme (red) flips every tick (the base/fastest rate); severe
        (orange) flips half as often; moderate (yellow) a quarter as often
        -- all derived from one shared counter/timer rather than separate
        timers per color."""
        c = self._alert_pulse_counter
        if color_name == 'red':
            return (c % 2) == 0
        if color_name == 'orange':
            return (c % 4) < 2
        return (c % 8) < 4  # yellow

    def _pulse_signature(self):
        """Bright/dim phase of every alert colour in play; changes only when
        something visible would actually change."""
        colors = {a.get('severity_color', 'yellow') for a in self.active_alerts}
        return tuple(self._pulse_on_for(c) for c in sorted(colors))

    def _alert_pulse_tick(self):
        self._alert_pulse_counter += 1
        if self.has_active_alerts():
            signature = self._pulse_signature()
            if signature != self._last_pulse_signature:  # yellow/orange flip far less often than red
                self._last_pulse_signature = signature
                self.update_icon()
                if self.popup and self.popup.get_visible():
                    self.rebuild_weather_bar()
        return True

    def build_forecast_weather_segments(self):
        """Returns one markup segment per forecast day (rain glyph + high/low),
        or [] if there's no forecast. Rendered by rebuild_weather_bar exactly
        like the 'today' segments: spread across the bar, bullet-separated."""
        d = self.weather_data
        if not d:
            return []
        segments = []
        pad = weather_row_pad(getattr(self, 'weather_box', None))
        for day in d.get('forecast_days', []):
            segment = ''
            if day.get('rain_prob') is not None and day['rain_prob'] > 0:
                segment += mono_glyph_markup('\u2614') + ' '
            if day.get('max_temp') is not None and day.get('min_temp') is not None:
                segment += GLib.markup_escape_text(
                    f"{day['max_temp']:.0f}/{day['min_temp']:.0f}°C"
                )
            if segment:
                segments.append(f'<span size="large"><b>{segment}{pad}</b></span>')
        return segments

    def build_today_weather_segments(self):
        """Returns a list of independently-markup'd segments (temp, high/low,
        wind, rain) for the 'today' view — rendered as separate widgets so
        they can be evenly spaced across the full row, bullet-separated."""
        d = self.weather_data
        if not d:
            return []
        seg_colors = self._segment_alert_colors()

        def colorize(text, segment):
            color_name = seg_colors.get(segment)
            if not color_name:
                return text
            bright, dim = ALERT_TEXT_COLORS[color_name]
            hexcolor = bright if self._pulse_on_for(color_name) else dim
            return f'<span foreground="{hexcolor}">{text}</span>'

        segments = []
        glyph = weather_code_glyph(d.get('weather_code'))
        widget = getattr(self, 'weather_box', None)
        rise = glyph_rise_units(glyph, widget) if glyph else 0
        pad = weather_row_pad(widget)
        if d.get('temp') is not None:
            temp_text = GLib.markup_escape_text(f"{d['temp']:.0f}°C")
            if glyph:
                glyph_color_name = seg_colors.get('glyph')
                if glyph_color_name:
                    bright, dim = ALERT_TEXT_COLORS[glyph_color_name]
                    glyph_color = bright if self._pulse_on_for(glyph_color_name) else dim
                else:
                    glyph_color = WEATHER_GLYPH_COLOR
                temp_text = mono_glyph_markup(glyph, glyph_color, rise) + ' ' + temp_text
            segments.append(f'<span size="large"><b>{temp_text}{pad}</b></span>')
        if d.get('today_max_temp') is not None and d.get('today_min_temp') is not None:
            hi_lo = colorize(GLib.markup_escape_text(
                f"{d['today_max_temp']:.0f}/{d['today_min_temp']:.0f}°C"
            ), 'hilo')
            segments.append(f'<span size="large"><b>{hi_lo}{pad}</b></span>')
        if d.get('wind') is not None and d.get('today_max_wind') is not None:
            wind_text = colorize(GLib.markup_escape_text(
                f"{d['wind']:.0f}/{d['today_max_wind']:.0f} km/h"
            ), 'wind')
            segments.append(f'<span size="large"><b>{wind_text}{pad}</b></span>')
        elif d.get('wind') is not None:
            wind_text = colorize(GLib.markup_escape_text(f"{d['wind']:.0f} km/h"), 'wind')
            segments.append(f'<span size="large"><b>{wind_text}{pad}</b></span>')
        prob, precip = d.get('today_rain_prob'), d.get('today_precip_sum')
        if prob is not None:
            if round(prob) == 0 and precip is not None:
                rain = f"{precip:.1f}mm"  # 0% chance: just the amount, no "0%/"
            elif precip is not None:
                rain = f"{prob:.0f}%/{precip:.1f}mm"
            else:
                rain = f"{prob:.0f}%"
            rain_text = colorize(GLib.markup_escape_text(rain), 'rain')
            segments.append(f'<span size="large"><b>{rain_text}{pad}</b></span>')
        return segments

    def _build_weather_row(self, segments, empty_markup):
        """Spreads `segments` evenly across the bar's full width with a bullet
        between each pair; shared by the 'today' and 'forecast' views so
        they look identical."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_hexpand(True)
        row.set_valign(Gtk.Align.CENTER)
        # Align the values on their text baselines (not by box edges): the raised
        # glyph makes the first label taller, which otherwise shifts its text.
        row.set_baseline_position(Gtk.BaselinePosition.CENTER)
        if not segments:
            segments, separate = [empty_markup], False
        else:
            separate = True
        for n, seg in enumerate(segments):
            if separate and n > 0:
                bullet = Gtk.Label()
                bullet.set_markup(WEATHER_BULLET_MARKUP)
                bullet.set_valign(Gtk.Align.BASELINE)
                row.pack_start(bullet, False, False, 0)
            lbl = Gtk.Label()
            lbl.set_markup(seg)
            lbl.set_hexpand(True)
            lbl.set_xalign(0.5)
            lbl.set_valign(Gtk.Align.BASELINE)
            row.pack_start(lbl, True, True, 0)
        return row

    def rebuild_weather_bar(self):
        if self.weather_box is None:
            return
        for child in self.weather_box.get_children():
            self.weather_box.remove(child)

        if self.weather_view == 'forecast':
            back_btn = Gtk.Button(label='‹')
            back_btn.set_tooltip_text('Back to today')
            back_btn.connect('clicked', self.on_weather_arrow_clicked, 'today')
            back_btn.set_valign(Gtk.Align.CENTER)
            self.weather_box.pack_start(back_btn, False, False, 0)

            pad = weather_row_pad(self.weather_box)
            empty = (f'<span size="large">Weather unavailable{pad}</span>' if not self.weather_data
                     else f'<span size="large">Forecast unavailable{pad}</span>')
            row = self._build_weather_row(self.build_forecast_weather_segments(), empty)
            self.weather_box.pack_start(row, True, True, 0)
        else:
            row = self._build_weather_row(
                self.build_today_weather_segments(),
                f'<span size="large">Weather unavailable{weather_row_pad(self.weather_box)}</span>')
            self.weather_box.pack_start(row, True, True, 0)

            fwd_btn = Gtk.Button(label='›')
            fwd_btn.set_tooltip_text('Show 5-day forecast')
            fwd_btn.connect('clicked', self.on_weather_arrow_clicked, 'forecast')
            fwd_btn.set_valign(Gtk.Align.CENTER)
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

    # ---- Schedule mode ------------------------------------------------------
    def _schedule_settings(self):
        """Settings, re-read only when config.conf changed (this runs every
        second). A change also re-syncs, so editing a time never runs an
        occurrence that is already in the past, and drops the state of
        schedules that no longer exist."""
        try:
            stamp = os.stat(CONFIG_FILE).st_mtime_ns
        except OSError:
            stamp = None
        if self._schedule_cache[1] is None or self._schedule_cache[0] != stamp:
            settings = load_schedule_settings()
            self._schedule_cache = (stamp, settings)
            self._schedule_resync = True
            self._prune_schedule_state({schedule_key(e) for e in settings['schedules']})
        return self._schedule_cache[1]

    def _prune_schedule_state(self, keys):
        with self.lock:
            changed = False
            for name in ('schedule_fired', 'schedule_override'):
                current = self.state.get(name)
                if not isinstance(current, dict):
                    if name in self.state:
                        del self.state[name]
                        changed = True
                    continue
                for key in [k for k in current if k not in keys]:
                    del current[key]
                    changed = True
            if changed:
                save_state(self.state)

    def _schedule_state(self):
        with self.lock:
            fired, override = self.state.get('schedule_fired'), self.state.get('schedule_override')
            return (dict(fired) if isinstance(fired, dict) else {},
                    dict(override) if isinstance(override, dict) else {})

    def _mark_schedule_handled(self, key, occurrence):
        with self.lock:
            fired = self.state.setdefault('schedule_fired', {})
            if not isinstance(fired, dict):
                fired = self.state['schedule_fired'] = {}
            fired[key] = max(fired.get(key, 0), occurrence)
            overrides = self.state.get('schedule_override')
            if isinstance(overrides, dict):
                overrides.pop(key, None)
            save_state(self.state)

    def _skip_missed_schedule(self, now_ts, key, settings):
        """App start, wake from sleep or a settings change: a time that has
        already passed is never acted on late (unless it was snoozed)."""
        previous = _schedule_occurrences(now_ts, settings['days'], settings['minute'])[0]
        fired, overrides = self._schedule_state()
        if previous is None or previous <= fired.get(key, 0):
            return
        override = overrides.get(key)
        if override and override.get('occ') == previous and override['fire'] > now_ts:
            return  # still snoozed
        self._mark_schedule_handled(key, previous)

    def _schedule_tick(self):
        now_ts = time_module.time()
        last, self._last_schedule_tick = self._last_schedule_tick, now_ts
        settings = self._schedule_settings()
        active = [(schedule_key(e), schedule_settings_for(settings, e))
                  for e in settings['schedules'] if e['enabled']]
        if not active:
            self._schedule_resync = True  # enabling later must not act on an older time
            self._set_schedule_warning(None)
            self._refresh_schedule_ui()
            return
        if self._schedule_resync or last is None or now_ts - last > SCHEDULE_RESUME_GAP_SECONDS:
            self._schedule_resync = False
            for key, entry in active:
                self._skip_missed_schedule(now_ts, key, entry)
        fired, overrides = self._schedule_state()
        warnings, to_fire = [], []
        for key, entry in active:
            kind, occurrence, left = schedule_status(now_ts, entry, fired.get(key, 0), overrides.get(key))
            if kind == 'warn':
                warnings.append({'key': key, 'occ': occurrence, 'left': math.ceil(left), 'action': entry['action']})
            elif kind == 'fire':
                to_fire.append((key, occurrence, entry))
            elif kind == 'skip':
                self._mark_schedule_handled(key, occurrence)
        for key, occurrence, entry in to_fire:
            self._fire_schedule(key, occurrence, entry)
        self._set_schedule_warning(min(warnings, key=lambda w: w['left']) if warnings else None)
        self._refresh_schedule_ui()

    def _fire_schedule(self, key, occurrence, settings):
        self._mark_schedule_handled(key, occurrence)
        actions = load_actions()
        command = next((c for label, c in actions if label == settings['action']), None) \
            or next((c for label, c in actions if label.lower() == settings['action'].lower()), None)
        if command:
            run_launcher_command(command)
        else:
            _log(f"schedule: no action named {settings['action']!r} in [actions]")

    def _set_schedule_warning(self, warning):
        previous = self.schedule_warning
        if warning is None and previous is None:
            return
        self.schedule_warning = warning
        if warning is not None:
            self._schedule_flash_on = not self._schedule_flash_on
            if previous is None:  # the warning just started: be hard to miss
                play_notification_sound()
                if not (self.popup and self.popup.get_visible()):
                    self.show_popup()
        else:
            self._schedule_flash_on = False
        self.update_icon()

    def on_schedule_cancel(self, _button=None):
        warning = self.schedule_warning
        if warning:
            self._mark_schedule_handled(warning['key'], warning['occ'])
            self._set_schedule_warning(None)
            self._refresh_schedule_ui()

    def _snooze_schedule(self, key):
        settings = self._schedule_settings()
        entry = next((schedule_settings_for(settings, e) for e in settings['schedules']
                      if schedule_key(e) == key), None)
        if entry is not None:
            fired, overrides = self._schedule_state()
            new = schedule_snooze_override(time_module.time(), entry, fired.get(key, 0), overrides.get(key))
            if new:
                with self.lock:
                    store = self.state.get('schedule_override')
                    if not isinstance(store, dict):
                        store = self.state['schedule_override'] = {}
                    store[key] = new
                    save_state(self.state)
        warning = self.schedule_warning
        if warning and warning['key'] == key:
            self._set_schedule_warning(None)
        self._refresh_schedule_ui()

    def on_schedule_snooze(self, _button=None):
        """Snooze button of the warning bar: postpones the schedule that is warning."""
        if self.schedule_warning:
            self._snooze_schedule(self.schedule_warning['key'])

    def on_schedule_snooze_selected(self, _button=None):
        """Snooze button of the slide: postpones the next run of the selected schedule."""
        entry = self._selected_schedule()
        if entry is not None:
            self._snooze_schedule(schedule_key(entry))

    def _selected_schedule(self):
        entries = self._schedule_settings()['schedules']
        if not entries:
            return None
        self.sched_index = min(max(self.sched_index, 0), len(entries) - 1)
        return entries[self.sched_index]

    def _schedule_summary(self, settings):
        warning = self.schedule_warning
        if warning:
            return f"{warning['action']} in {warning['left']} s"
        now_ts = time_module.time()
        fired, overrides = self._schedule_state()
        upcoming = []
        for entry in settings['schedules']:
            if not entry['enabled']:
                continue
            key = schedule_key(entry)
            for occurrence, fire in schedule_pending(now_ts, entry, fired.get(key, 0), overrides.get(key)):
                if fire > now_ts:
                    upcoming.append((fire, occurrence, entry['action']))
        if not upcoming:
            return 'Schedule on' if any(e['enabled'] for e in settings['schedules']) else 'Schedule off'
        fire, occurrence, action = min(upcoming)
        when = datetime.fromtimestamp(fire)
        text = f"Next: {_DAY_NAMES[when.weekday()][:3].title()} {when:%H:%M} {action}"
        return text + (' (snoozed)' if fire != occurrence else '')

    def _refresh_schedule_ui(self):
        warning = self.schedule_warning
        if self.schedule_banner is not None:
            self.schedule_banner.set_visible(warning is not None)
            if warning:
                self.schedule_banner_label.set_text(f"{warning['action']} in {warning['left']} s")
        if self.timer_label is not None and self.timer_mode == 'schedule':
            self.timer_label.set_text(self._schedule_summary(self._schedule_settings()))
        if self.sched is not None:
            entry = self._selected_schedule()
            self.sched['snooze_btn'].set_sensitive(entry is not None and entry['enabled'])

    def _apply_timer_mode(self):
        if self.timer_box is None:
            return
        schedule = self.timer_mode == 'schedule'
        self.countdown_box.set_visible(not schedule)
        self.schedule_box.set_visible(schedule)
        height = TIMER_HEIGHT_SCHEDULE_PX if schedule else TIMER_HEIGHT_COUNTDOWN_PX
        self.timer_box.set_size_request(-1, height)
        if self.list_spacer is not None:
            self.list_spacer.set_size_request(-1, height + TIMER_SPACER_EXTRA_PX)
        if schedule:
            self._refresh_schedule_ui()
        else:
            self.timer_label.set_text(format_timer_duration(self.timer_remaining_seconds))

    def on_timer_mode_toggled(self, button, mode):
        if self._mode_updating:
            return
        self._mode_updating = True
        if not button.get_active():
            button.set_active(True)  # clicking the active mode again keeps it selected
        else:
            self.timer_mode = mode
            for name, other in self.mode_buttons.items():
                other.set_active(name == mode)
        self._mode_updating = False
        self._apply_timer_mode()

    # -- the Schedule controls: a selector for the list, and the selected schedule's fields
    @staticmethod
    def _schedule_label(entry):
        text = f"{entry['minute'] // 60:02d}:{entry['minute'] % 60:02d} {format_days(entry['days'])} \u00b7 {entry['action']}"
        return text if entry['enabled'] else text + ' (off)'

    def _save_schedule_entries(self, entries):
        save_schedules(entries)
        self._schedule_cache = (None, None)  # re-read (and re-sync) on next use

    def _populate_schedule_selector(self):
        entries = self._schedule_settings()['schedules']
        select = self.sched['select']
        self._sched_updating = True
        select.remove_all()
        for entry in entries:
            select.append_text(self._schedule_label(entry))
        if entries:
            self.sched_index = min(max(self.sched_index, 0), len(entries) - 1)
            select.set_active(self.sched_index)
        self._sched_updating = False
        self._load_selected_schedule_into_controls()

    def _load_selected_schedule_into_controls(self):
        entry = self._selected_schedule()
        sched = self.sched
        sched['fields'].set_sensitive(entry is not None)
        sched['remove'].set_sensitive(entry is not None)
        self._sched_updating = True
        if entry is not None:
            sched['enabled'].set_active(entry['enabled'])
            sched['hour'].set_value(entry['minute'] // 60)
            sched['minute'].set_value(entry['minute'] % 60)
            for index, button in enumerate(sched['days']):
                button.set_active(index in entry['days'])
            action = sched['action']
            action.remove_all()
            labels = [label for label, _command in load_actions()]
            for label in labels:
                action.append_text(label)
            if entry['action'] in labels:
                action.set_active(labels.index(entry['action']))
            else:  # the action no longer exists: show it, flagged, so nothing silently changes
                action.append_text(entry['action'] + SCHEDULE_MISSING_ACTION_SUFFIX)
                action.set_active(len(labels))
        self._sched_updating = False
        self._refresh_schedule_ui()

    def _on_schedule_selected(self, select):
        if self._sched_updating:
            return
        self.sched_index = max(select.get_active(), 0)
        self._load_selected_schedule_into_controls()

    def _on_schedule_add(self, _button):
        entries = [dict(e) for e in self._schedule_settings()['schedules']]
        new = dict(NEW_SCHEDULE)
        while any(schedule_key(e) == schedule_key(new) for e in entries):
            new['minute'] = (new['minute'] + 30) % (24 * 60)  # identities must differ
        entries.append(new)
        self._save_schedule_entries(entries)
        self.sched_index = len(entries) - 1
        self._populate_schedule_selector()

    def _on_schedule_remove(self, _button):
        entries = [dict(e) for e in self._schedule_settings()['schedules']]
        if entries:
            del entries[min(self.sched_index, len(entries) - 1)]
            self._save_schedule_entries(entries)
            self.sched_index = max(0, min(self.sched_index, len(entries) - 1))
            self._populate_schedule_selector()

    def _on_schedule_control_changed(self, widget=None):
        if self._sched_updating or self.sched is None:
            return
        days = frozenset(i for i, b in enumerate(self.sched['days']) if b.get_active())
        if not days:  # at least one day must stay selected
            self._sched_updating = True
            widget.set_active(True)
            self._sched_updating = False
            return
        entries = [dict(e) for e in self._schedule_settings()['schedules']]
        if not entries:
            return
        index = min(self.sched_index, len(entries) - 1)
        entry = entries[index]
        entry['enabled'] = self.sched['enabled'].get_active()
        entry['minute'] = int(self.sched['hour'].get_value()) * 60 + int(self.sched['minute'].get_value())
        entry['days'] = days
        action = self.sched['action'].get_active_text() or ''
        if action and not action.endswith(SCHEDULE_MISSING_ACTION_SUFFIX):
            entry['action'] = action
        if any(i != index and schedule_key(e) == schedule_key(entry) for i, e in enumerate(entries)):
            return  # would duplicate another schedule; ignore this change
        self._save_schedule_entries(entries)
        select = self.sched['select']
        self._sched_updating = True
        select.remove(index)
        select.insert_text(index, self._schedule_label(entry))
        select.set_active(index)
        self._sched_updating = False
        self._refresh_schedule_ui()

    def _build_schedule_box(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)

        row0 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        select = Gtk.ComboBoxText()
        add_btn = Gtk.Button(label='+')
        add_btn.set_tooltip_text('Add a schedule')
        remove_btn = Gtk.Button(label='\u2212')
        remove_btn.set_tooltip_text('Remove the selected schedule')
        row0.pack_start(select, True, True, 0)
        row0.pack_start(add_btn, False, False, 0)
        row0.pack_start(remove_btn, False, False, 0)

        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        enabled = Gtk.CheckButton(label='On')
        hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        for spin in (hour, minute):
            spin.set_wrap(True)
            spin.set_numeric(True)
            spin.set_width_chars(2)
            spin.connect('output', lambda s: (s.set_text(f'{int(s.get_value()):02d}'), True)[1])
        action = Gtk.ComboBoxText()
        row1.pack_start(enabled, False, False, 0)
        row1.pack_start(Gtk.Label(label='at'), False, False, 0)
        row1.pack_start(hour, False, False, 0)
        row1.pack_start(Gtk.Label(label=':'), False, False, 0)
        row1.pack_start(minute, False, False, 0)
        row1.pack_start(action, True, True, 0)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        day_buttons = []
        for index, name in enumerate(_DAY_NAMES):
            button = Gtk.ToggleButton(label=name[:2].title())
            button.set_tooltip_text(name.title())
            day_buttons.append(button)
            row2.pack_start(button, False, False, 0)
        snooze_btn = Gtk.Button(label=f"Snooze {self._schedule_settings()['snooze']} min")
        snooze_btn.set_tooltip_text('Postpone the next run of this schedule')
        snooze_btn.connect('clicked', self.on_schedule_snooze_selected)
        row2.pack_end(snooze_btn, False, False, 0)

        fields.pack_start(row1, False, False, 0)
        fields.pack_start(row2, False, False, 0)
        box.pack_start(row0, False, False, 0)
        box.pack_start(fields, False, False, 0)

        self.sched = {'select': select, 'add': add_btn, 'remove': remove_btn, 'fields': fields,
                      'enabled': enabled, 'hour': hour, 'minute': minute, 'action': action,
                      'days': day_buttons, 'snooze_btn': snooze_btn}
        select.connect('changed', self._on_schedule_selected)
        add_btn.connect('clicked', self._on_schedule_add)
        remove_btn.connect('clicked', self._on_schedule_remove)
        enabled.connect('toggled', self._on_schedule_control_changed)
        hour.connect('value-changed', self._on_schedule_control_changed)
        minute.connect('value-changed', self._on_schedule_control_changed)
        action.connect('changed', self._on_schedule_control_changed)
        for button in day_buttons:
            button.connect('toggled', self._on_schedule_control_changed)
        self._populate_schedule_selector()
        return box

    def on_timer_toggle_clicked(self, _button):
        self.timer_visible = not self.timer_visible
        if self.timer_box is not None:
            self.timer_box.set_visible(self.timer_visible)

    def _timer_tick(self):
        if self.timer_running and self.timer_deadline is not None:
            left = self.timer_deadline - time_module.monotonic()
            self.timer_remaining_seconds = max(0, int(-(-left // 1)))  # ceil
            if self.timer_remaining_seconds <= 0:
                self.timer_running = False
                self.timer_deadline = None
                self._fire_timer_done()
            self._update_timer_widgets()
        self._schedule_tick()
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
        if self.timer_mode == 'countdown':  # in Schedule mode the label shows the schedule summary
            self.timer_label.set_text(format_timer_duration(self.timer_remaining_seconds))

    def on_timer_slider_changed(self, scale):
        if self._timer_updating_ui:
            return
        value = int(scale.get_value())
        self.timer_remaining_seconds = value
        self.timer_running = value > 0
        self.timer_deadline = (time_module.monotonic() + value) if value > 0 else None
        if self.timer_label is not None:
            self.timer_label.set_text(format_timer_duration(value))

    def _should_show_weather_icon(self, count):
        return (
            count == 0 and not self.has_pkg_update() and not self.has_active_alerts()
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
        if self.schedule_warning:
            tooltip = (f"{self.schedule_warning['action']} in {self.schedule_warning['left']} s "
                       "\u2014 open the popup to cancel or snooze")
        elif self.has_active_alerts():
            hazards = ', '.join(sorted({a['hazard'] for a in self.active_alerts}))
            tooltip = f"\u26a0 {hazards} warning"
        elif self._should_show_weather_icon(count):
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
                or bool(self.state.get('live_youtube_channels'))
            )

    def has_pkg_update(self):
        with self.lock:
            return bool(self.state.get('available_updates'))

    def render_icon(self, count):
        size = 24
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        ctx = cairo.Context(surface)

        if self.schedule_warning is not None and self._schedule_flash_on:
            # pending scheduled action: flash a red "!" every other second
            ctx.set_source_rgba(0.82, 0.12, 0.12, 1)
            ctx.arc(size / 2, size / 2, size / 2 - 1, 0, 2 * 3.14159265)
            ctx.fill()
            ctx.set_source_rgba(1, 1, 1, 1)
            ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ctx.set_font_size(18)
            xb, yb, w, h, _dx, _dy = ctx.text_extents('!')
            ctx.move_to(size / 2 - w / 2 - xb, size / 2 - h / 2 - yb)
            ctx.show_text('!')
            surface.flush()
            return Gdk.pixbuf_get_from_surface(surface, 0, 0, size, size)

        show_weather = self._should_show_weather_icon(count)

        if show_weather:
            # Temperature only, no glyph — the icon is a fixed-size XEMBED
            # tray slot with no way to make it bigger overall, so dropping
            # the glyph here lets the digits alone claim the full icon
            # instead of splitting the space with it. The glyph still shows
            # in the popup's weather bar, where space isn't constrained.
            ctx.set_source_rgba(1, 1, 1, 1)
            temp_text = f"{self.weather_data['temp']:.0f}"
            padding = 1
            max_width = size - 2 * padding - TEMP_RING_RESERVE  # room for the ring

            ctx.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            temp_size = 20
            min_temp_size = 10
            while temp_size > min_temp_size:
                ctx.set_font_size(temp_size)
                if ctx.text_extents(temp_text)[4] <= max_width:
                    break
                temp_size -= 1

            xb, yb, tw, th, _dx, _dy = ctx.text_extents(temp_text)
            digits_left, ring_cx, ring_cy = temp_icon_layout(size, tw, th)
            ctx.move_to(digits_left - xb, size / 2 - th / 2 - yb)
            ctx.show_text(temp_text)
            ctx.set_line_width(TEMP_RING_LINE_WIDTH)
            ctx.new_sub_path()
            # snap the centre to a pixel centre so the tiny ring is drawn
            # crisp (full-intensity pixels) instead of a blurry grey dot
            ctx.arc(math.floor(ring_cx) + 0.5, math.floor(ring_cy) + 0.5,
                    TEMP_RING_RADIUS, 0, 2 * 3.14159265)
            ctx.stroke()
        else:
            worst_alert_color = self._worst_alert_color()
            if worst_alert_color:
                bright, dim = ALERT_BADGE_COLORS[worst_alert_color]
                ctx.set_source_rgba(*(bright if self._pulse_on_for(worst_alert_color) else dim))
            elif self.has_pkg_update():
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

    def _apply_compact_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(POPUP_CSS.encode())
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

    def probe_connectivity(self):
        """Background network probe feeding the popup's offline indicator.
        Returns True so it can also be used directly as a repeating timer."""
        if not self._probing:
            self._probing = True

            def work():
                ok = is_online()
                GLib.idle_add(self._on_probe_result, ok)
            threading.Thread(target=work, daemon=True).start()
        return True

    def _on_probe_result(self, probe_ok):
        self._probing = False
        online, self._probe_failures = next_connectivity(self.online, self._probe_failures, probe_ok)
        if online != self.online:
            self._set_online(online)
        return False

    def _set_online(self, online):
        came_back = online and not self.online
        self.online = online
        if self.offline_banner is not None:
            self.offline_banner.set_visible(not online)
        if came_back:
            # don't wait out the remaining intervals after an outage
            self.start_check_thread()
            self.start_weather_fetch()
            self.start_twitch_check()
            self.start_youtube_check()

    def on_popup_focus_out(self, win, _event):
        if self._launcher_open:
            return False  # the Launch list took focus; don't close under it
        win.hide()
        return False

    def on_launcher_button_clicked(self, button):
        """Builds the Launch list fresh (so config edits show up immediately)
        and pops it up above the button."""
        entries = load_launcher_entries()
        popover = Gtk.Popover.new(button)
        popover.set_position(Gtk.PositionType.TOP)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        if not entries:
            hint = Gtk.Label(label='No commands yet — add them under [launcher]\nin the config (Label|command).')
            hint.set_margin_start(6)
            hint.set_margin_end(6)
            hint.set_margin_top(4)
            hint.set_margin_bottom(4)
            box.pack_start(hint, False, False, 0)
        for label, command in entries:
            item = Gtk.Button(label=label)
            item.set_relief(Gtk.ReliefStyle.NONE)
            item.get_child().set_xalign(0)
            item.set_tooltip_text(command)
            item.connect('clicked', self.on_launcher_item_clicked, command, popover)
            box.pack_start(item, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(LAUNCHER_MAX_HEIGHT_PX)
        scroller.add(box)
        popover.add(scroller)
        popover.connect('closed', self.on_launcher_closed)
        self._launcher_open = True
        scroller.show_all()
        popover.popup()

    def on_launcher_closed(self, popover):
        self._launcher_open = False
        GLib.idle_add(popover.destroy)

    def on_launcher_item_clicked(self, _item, command, popover):
        run_launcher_command(command)
        popover.popdown()
        if self.popup:
            self.popup.hide()

    def build_popup_window(self):
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_type_hint(Gdk.WindowTypeHint.POPUP_MENU)
        win.set_keep_above(True)
        win.set_default_size(WINDOW_WIDTH, -1)
        win.connect('focus-out-event', self.on_popup_focus_out)
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

        banner = Gtk.Label(label=OFFLINE_BANNER_TEXT)
        banner.get_style_context().add_class('offline-banner')
        banner.set_line_wrap(True)
        banner.set_justify(Gtk.Justification.CENTER)
        banner.set_no_show_all(True)  # shown/hidden by _set_online, not by show_all()
        banner.set_visible(not self.online)
        self.offline_banner = banner
        outer.pack_start(banner, False, False, 0)

        warn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        warn_box.get_style_context().add_class('schedule-warning')
        warn_label = Gtk.Label()
        warn_label.set_xalign(0)
        warn_label.set_hexpand(True)
        cancel_btn = Gtk.Button(label='Cancel')
        cancel_btn.connect('clicked', self.on_schedule_cancel)
        warn_snooze_btn = Gtk.Button(label=f"Snooze {self._schedule_settings()['snooze']} min")
        warn_snooze_btn.connect('clicked', self.on_schedule_snooze)
        for widget in (warn_label, cancel_btn, warn_snooze_btn):
            warn_box.pack_start(widget, widget is warn_label, widget is warn_label, 0)
        warn_box.set_no_show_all(True)  # shown by _refresh_schedule_ui; children shown explicitly
        for widget in (warn_label, cancel_btn, warn_snooze_btn):
            widget.show()
        if self.schedule_warning:
            warn_label.set_text(f"{self.schedule_warning['action']} in {self.schedule_warning['left']} s")
        warn_box.set_visible(self.schedule_warning is not None)
        self.schedule_banner = warn_box
        self.schedule_banner_label = warn_label
        outer.pack_start(warn_box, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        self.scroller = scroller
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect('row-activated', self.on_row_activated)
        self.listbox.connect('button-press-event', self.on_listbox_button_press)

        # The list sits in a box together with a real spacer that reserves the
        # strip the timer slides over (timer_box is 60px tall, valign END; the
        # extra 25px keeps a gap above it instead of its top edge sitting flush
        # against the last row). It must be actual content rather than a margin
        # on the list: a margin lies outside every widget's paint area, so once
        # the list was scrolled to its end that strip rendered as an unpainted
        # black rectangle. An EventBox wraps it all so a right-click on any
        # empty part -- including the strip behind a closed timer -- closes
        # the popup.
        spacer = Gtk.Box()
        spacer.set_size_request(-1, LIST_BOTTOM_SPACE_PX)
        self.list_spacer = spacer
        list_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        list_content.pack_start(self.listbox, False, False, 0)
        list_content.pack_start(spacer, False, False, 0)
        list_events = Gtk.EventBox()
        list_events.get_style_context().add_class('popup-content')
        list_events.connect('button-press-event', self.on_empty_area_button_press)
        list_events.add(list_content)
        scroller.add(list_events)

        content_overlay = Gtk.Overlay()
        content_overlay.add(scroller)

        timer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        timer_box.set_margin_start(8)
        timer_box.set_margin_end(8)
        timer_box.set_margin_top(3)
        timer_box.set_margin_bottom(4)
        timer_box.set_size_request(-1, TIMER_HEIGHT_COUNTDOWN_PX)
        timer_box.set_halign(Gtk.Align.FILL)
        timer_box.set_valign(Gtk.Align.END)
        timer_box.get_style_context().add_class('timer-bar')
        timer_box.set_no_show_all(True)
        timer_box.set_visible(self.timer_visible)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.mode_buttons = {}
        for mode, title in (('countdown', 'Countdown'), ('schedule', 'Schedule')):
            mode_button = Gtk.ToggleButton(label=title)
            mode_button.set_active(self.timer_mode == mode)
            mode_button.connect('toggled', self.on_timer_mode_toggled, mode)
            self.mode_buttons[mode] = mode_button
            mode_row.pack_start(mode_button, False, False, 0)
        timer_label = Gtk.Label()
        timer_label.set_xalign(1)
        timer_label.set_hexpand(True)
        timer_label.set_text(format_timer_duration(self.timer_remaining_seconds))
        self.timer_label = timer_label
        header.pack_start(mode_row, False, False, 0)
        header.pack_start(timer_label, True, True, 0)
        timer_box.pack_start(header, False, False, 0)

        timer_settings = load_timer_settings()
        max_seconds = timer_settings['max_seconds']
        step_seconds = timer_settings['step_seconds']
        if self.timer_remaining_seconds > max_seconds:
            self.timer_remaining_seconds = max_seconds  # clamp: config may have
                                                          # lowered max since a
                                                          # timer was last set
        timer_adjustment = Gtk.Adjustment(
            value=self.timer_remaining_seconds, lower=0, upper=max_seconds,
            step_increment=step_seconds, page_increment=step_seconds * 5, page_size=0
        )
        timer_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=timer_adjustment)
        timer_scale.set_draw_value(False)
        timer_scale.connect('value-changed', self.on_timer_slider_changed)
        self.timer_scale = timer_scale
        countdown_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        countdown_box.pack_start(timer_scale, False, False, 0)
        timer_box.pack_start(countdown_box, False, False, 0)
        self.sched = None
        schedule_box = self._build_schedule_box()
        timer_box.pack_start(schedule_box, False, False, 0)
        self.countdown_box, self.schedule_box = countdown_box, schedule_box

        self.timer_box = timer_box
        content_overlay.add_overlay(timer_box)
        # timer_box has no_show_all(True) so that the popup's own show_all()
        # doesn't reveal it prematurely — but that flag ALSO blocks show_all()
        # called on timer_box itself (no_show_all stops recursion at the
        # widget that has it set, however show_all() was invoked). So its
        # children must be shown individually with .show(), which no_show_all
        # does not affect, rather than via any show_all() call on the box.
        header.show_all()
        countdown_box.show_all()
        schedule_box.show_all()
        self._apply_timer_mode()

        outer.pack_start(content_overlay, True, True, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        footer.set_margin_start(6)
        footer.set_margin_end(6)
        footer.set_margin_top(4)
        footer.set_margin_bottom(4)
        launch_btn = Gtk.Button(label='Launch')
        launch_btn.set_tooltip_text('Run one of your [launcher] commands')
        launch_btn.connect('clicked', self.on_launcher_button_clicked)
        footer.pack_start(launch_btn, True, True, 0)
        timer_toggle_btn = Gtk.Button(label='Timer')
        timer_toggle_btn.set_tooltip_text('Show/hide countdown timer')
        timer_toggle_btn.connect('clicked', self.on_timer_toggle_clicked)
        footer.pack_start(timer_toggle_btn, True, True, 0)
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
        if auto and self.popup is not None and self.popup.get_visible():
            self.refresh_list()  # already open: update in place, keeping scroll position and any open Launch list
            return
        if self.popup is not None:
            self._launcher_open = False  # its Launch list (if open) dies with the window
            self.popup.destroy()
            self.popup = None
            self.timer_scale = None
            self.timer_label = None
            self.timer_box = None
            self.countdown_box = self.schedule_box = self.sched = None
            self.list_spacer = self.schedule_banner = self.schedule_banner_label = None
            self.mode_buttons = {}
        self.weather_view = 'today'
        self.probe_connectivity()  # fresh answer for the indicator while the popup is open
        self.build_popup_window()
        self._update_scroller_max_height()
        self.refresh_list()
        self.position_popup()
        self.popup.show_all()
        self.popup.present()
        self.popup.grab_focus()

    def position_popup(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()

        x = geo.x + geo.width - WINDOW_WIDTH  # flush against the right edge

        y = geo.y + 2  # flush against the top, as a fallback
        try:
            ok, _screen, area, _orientation = self.status_icon.get_geometry()
            if ok and area is not None:
                y = area.y + area.height + 4  # 4px below the panel/tray icon
        except Exception:
            pass

        self.popup.move(max(x, 0), max(y, 0))

    def refresh_list(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        with self.lock:
            unread = list(self.state.get('unread', []))
            available = list(self.state.get('available_updates', []))
            live_channels = list(self.state.get('live_channels', []))
            live_youtube_channels = list(self.state.get('live_youtube_channels', []))

        feeds_map = {url: (custom_name or url) for url, custom_name, _interval in load_feeds()}

        any_live = bool(live_channels or live_youtube_channels)
        truly_empty = not unread and not available and not any_live
        only_live_remains = (not unread and not available) and any_live
        popup_already_visible = bool(self.popup and self.popup.get_visible())
        should_hide = truly_empty or (only_live_remains and popup_already_visible)

        if should_hide:
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

            if any_live:
                self.listbox.add(self.build_header_row(
                    '__live__', 'Live now', is_first=is_first_section, clickable=False
                ))
                is_first_section = False
                for entry in live_channels:
                    self.listbox.add(self.build_twitch_row(entry))
                for entry in live_youtube_channels:
                    self.listbox.add(self.build_youtube_row(entry))

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

    def _build_live_row(self, entry, site, color):
        """Shared by the Twitch and YouTube rows: '<user> - <category>' with
        the stream title as a tooltip; the leading bullet becomes a play
        triangle while this channel is what mpv is playing."""
        channel = entry['channel']
        title = entry.get('title') or ''
        category = entry.get('category') or ''

        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(True)
        row.live_title = title

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(0)
        box.set_margin_bottom(0)

        marker = Gtk.Label()
        marker.set_markup(live_marker_markup(color, is_stream_playing(site, channel)))
        box.pack_start(marker, False, False, 0)

        markup = f'<b>{GLib.markup_escape_text(channel)}</b>'
        if category:
            markup += f' - {GLib.markup_escape_text(category)}'
        label = Gtk.Label()
        label.set_markup(f'<span foreground="#000000">{markup}</span>')
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        box.pack_start(label, True, True, 0)

        if title:
            row.set_tooltip_text(title)
            label.set_tooltip_text(title)

        row.add(box)
        return row

    def build_twitch_row(self, entry):
        row = self._build_live_row(entry, 'Twitch', '#9146FF')
        row.twitch_channel = entry['channel']
        row.twitch_quality = load_twitch_qualities().get(entry['channel'], 'best')
        return row

    def build_youtube_row(self, entry):
        row = self._build_live_row(entry, 'YouTube', '#FF0000')
        row.youtube_channel = entry['channel']
        row.youtube_quality = load_youtube_qualities().get(entry['channel'], 'best')
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

    def on_empty_area_button_press(self, _widget, event):
        if event.button == 3 and self.popup:  # right-click on empty space: close
            self.popup.hide()
            return True
        return False

    def on_listbox_button_press(self, listbox, event):
        if event.button == 3:  # right-click
            row = listbox.get_row_at_y(int(event.y))
            if row is not None and hasattr(row, 'entry_id'):
                self.on_mark_read_clicked(None, row.entry_id)  # mark as read without opening
                return True
            if row is None:
                if self.popup:
                    self.popup.hide()  # empty space below the last item — close the popup
                return True
        return False

    def _remove_unread(self, item_id):
        with self.lock:
            self.state['unread'] = [e for e in self.state.get('unread', []) if e['id'] != item_id]
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
            versions_at_start = {pkg: get_installed_version(pkg) for pkg in pkgnames}
            for pkgname in pkgnames:
                before = get_installed_version(pkgname)
                if before != versions_at_start[pkgname]:
                    # `xbps-install -u <pkg>` also updates <pkg>'s outdated
                    # dependencies in the same transaction, so a package listed
                    # later may already have been updated as someone's
                    # dependency; running xbps again would change nothing and
                    # wrongly look like a failure.
                    self._set_status(pkgname, 'Done')
                    GLib.timeout_add(1200, self._finalize_package_removal, pkgname)
                    continue
                self._set_status(pkgname, 'Installing…')
                cmd = PRIVILEGE_CMD + ['xbps-install', '-Su', '-y', pkgname]
                returncode = -1
                watchdog = None
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, start_new_session=True
                    )
                    # stdout iteration blocks until EOF, so a wait(timeout=)
                    # afterwards can never fire on a hang; the watchdog can.
                    watchdog = threading.Timer(
                        UPDATE_TIMEOUT_SECONDS, _kill_process_group, args=(proc,)
                    )
                    watchdog.daemon = True
                    watchdog.start()
                    # xbps-install prints section headers like "[*] Downloading
                    # packages", "[*] Collecting package files", "[*] Unpacking
                    # packages", "[*] Configuring unpacked packages" — the actual
                    # per-file lines under them don't contain words like
                    # "download" at all, so we key off these headers instead.
                    # Since we install one package at a time, every line in this
                    # stream belongs to the current package regardless of wording.
                    shown = 'Installing…'
                    for line in proc.stdout:
                        stripped = line.strip()
                        if not stripped.startswith('[*]'):
                            continue
                        status = 'Downloading…' if stripped.startswith('[*] Downloading') else 'Installing…'
                        if status != shown:  # each change rebuilds the list; skip repeats
                            shown = status
                            self._set_status(pkgname, status)
                    proc.wait()
                    returncode = proc.returncode
                except Exception:
                    pass
                finally:
                    if watchdog is not None:
                        watchdog.cancel()
                after = get_installed_version(pkgname)
                success = returncode == 0 and before != after
                if success:
                    self._set_status(pkgname, 'Done')
                    GLib.timeout_add(1200, self._finalize_package_removal, pkgname)
                else:
                    self._set_status(pkgname, 'Failed')
        GLib.idle_add(self._end_install)

    def on_row_activated(self, _listbox, row):
        if not self.online and (getattr(row, 'install_all_header', False)
                                or hasattr(row, 'header_feed_url') or hasattr(row, 'entry_id')
                                or hasattr(row, 'twitch_channel') or hasattr(row, 'youtube_channel')):
            return  # offline: news, feed headers, "install all" and live channels do nothing
        if getattr(row, 'install_all_header', False):
            self.install_all_updates()
            return
        if hasattr(row, 'header_feed_url'):
            self.mark_feed_read(row.header_feed_url)
            return
        if hasattr(row, 'twitch_channel'):
            if self.popup:
                self.popup.hide()
            open_twitch_stream(row.twitch_channel, getattr(row, 'twitch_quality', 'best'),
                               title=getattr(row, 'live_title', None))
            return
        if hasattr(row, 'youtube_channel'):
            if self.popup:
                self.popup.hide()
            open_youtube_stream(row.youtube_channel, getattr(row, 'youtube_quality', 'best'),
                                title=getattr(row, 'live_title', None))
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


def _unix_signal_add(signum, callback):
    """Runs `callback` on the GLib main loop when `signum` arrives. Newer GLib
    moved this to GLibUnix (GLib.unix_signal_add is deprecated there and warns
    on every call); older ones only have the GLib version."""
    if GLibUnix is not None:
        for name in ('signal_add', 'signal_add_full'):
            add = getattr(GLibUnix, name, None)
            if add is not None:
                return add(GLib.PRIORITY_DEFAULT, signum, callback)
    return GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, callback)


def _install_quit_signal_handlers():
    """SIGTERM (session logout, `kill`) and SIGINT (Ctrl+C) leave the GTK main
    loop cleanly, so atexit hooks run and the local streamlink server isn't
    left behind."""
    def quit_loop():
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            _unix_signal_add(sig, quit_loop)
        except (AttributeError, TypeError):
            pass  # very old PyGObject: keep the default behaviour


def main():
    RssTray()
    _install_quit_signal_handlers()
    Gtk.main()


if __name__ == '__main__':
    main()
