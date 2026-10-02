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
import signal
import subprocess
import threading
import webbrowser
import hashlib
import atexit
import calendar
import time as time_module
import urllib.request
from datetime import datetime, timezone

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
PRIVILEGE_CMD = ['sudo', '-n']  # -n: fail fast rather than hang if a password would be needed;
                          # change to ['doas'] if that's what you use; requires
                          # passwordless (NOPASSWD) rules for xbps-install, since
                          # updates run headlessly with no terminal/tty attached
WINDOW_WIDTH = 471  # 380 * 1.2, +15px total
UPDATE_TIMEOUT_SECONDS = 1800  # 30 minutes
NOTIFICATION_SOUND_CANDIDATES = [
    os.path.join(CONFIG_DIR, 'notification.wav'),  # QuiteRSS's notification sound, if present
    '/usr/share/sounds/alsa/Front_Center.wav',      # fallback if the above is missing
]

def build_weather_api_url(lat, lon):
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,wind_speed_10m,weather_code"
        "&hourly=wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum"
        "&forecast_days=6&timezone=auto"
    )
WEATHER_REFRESH_SECONDS = 1800  # 30 minutes
ALERTS_REGION_REFRESH_SECONDS = 7 * 24 * 3600  # re-resolve country/region from lat/lon weekly
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
            "# Clicking a live channel plays it (streamlink + mpv, one shared maximized window).\n"
            "# examplechannel\n"
            "\n"
            "[weather]\n"
            "# Coordinates for the weather bar: lat|lon (one line, decimal degrees).\n"
            "# Uses Open-Meteo, no account/key needed. The weather bar stays hidden\n"
            "# until this line is set, e.g.:\n"
            "# <latitude>|<longitude>\n"
            "#\n"
            "# alerts=true enables MeteoAlarm severe-weather alerts (European\n"
            "# countries covered by MeteoAlarm) for the country/region matching the\n"
            "# coordinates above (reverse-geocoded automatically via OpenStreetMap,\n"
            "# re-checked weekly). When an alert is active, the matching weather\n"
            "# value and the tray badge pulse in the alert's color.\n"
            "# The app writes country=/region=/region_updated= back into this\n"
            "# section itself once resolved -- leave those alone.\n"
            "alerts=false\n"
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
    sections = {'feeds': [], 'mute': [], 'twitch': [], 'weather': [], 'timer': [], 'youtube': []}
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
    """Returns a list of lowercase Twitch channel login names to watch,
    de-duplicated while preserving the order they appear in the config.
    A line may optionally have |quality after the name (see
    load_twitch_qualities); only the name is used here."""
    seen = set()
    channels = []
    for line in _read_config_sections()['twitch']:
        ch = line.split('|', 1)[0].strip().lower()
        if ch and ch not in seen:
            seen.add(ch)
            channels.append(ch)
    return channels


DEFAULT_TIMER_MAX_MINUTES = 120
DEFAULT_TIMER_STEP_SECONDS = 60


def load_timer_settings():
    """Returns {'max_seconds', 'step_seconds'} from config.conf's [timer]
    section: 'max=<minutes>' and 'step=<seconds>' key=value lines, in any
    order/combination. Missing or unparsable values fall back to the
    built-in defaults (120 minutes, 60 second steps -- the app's previous
    fixed behavior)."""
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


def load_twitch_qualities():
    """Returns {channel: quality} for config lines shaped 'channel|quality'
    (e.g. 'channel1|720p60'). A channel with no '|quality' part is simply
    absent here; callers should default missing entries to 'best'."""
    qualities = {}
    for line in _read_config_sections()['twitch']:
        parts = [p.strip() for p in line.split('|', 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            qualities[parts[0].lower()] = parts[1]
    return qualities


def load_youtube_channels():
    """Returns a list of YouTube channel identifiers to watch (as typed --
    e.g. '@somehandle' or 'channel/UCxxxxxxxxxxxxxxxxxxxxxx' -- YouTube
    identifiers are case-sensitive, unlike Twitch logins), de-duplicated
    case-insensitively while preserving the first-seen casing and the
    order they appear in the config."""
    seen = set()
    channels = []
    for line in _read_config_sections()['youtube']:
        ch = line.split('|', 1)[0].strip()
        key = ch.lower()
        if ch and key not in seen:
            seen.add(key)
            channels.append(ch)
    return channels


def load_youtube_qualities():
    """Returns {channel: quality} for config lines shaped 'channel|quality'
    (e.g. '@somehandle|720p60'). Keyed by the channel identifier exactly as
    typed (case-sensitive). A channel with no '|quality' part is simply
    absent here; callers should default missing entries to 'best'."""
    qualities = {}
    for line in _read_config_sections()['youtube']:
        parts = [p.strip() for p in line.split('|', 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            qualities[parts[0]] = parts[1]
    return qualities


def load_weather_settings():
    """Returns {'lat', 'lon', 'alerts_enabled', 'country', 'region',
    'region_updated'} from config.conf's [weather] section. lat/lon are None
    until a coordinates line is configured. The coordinates line has no '='
    ('lat|lon'); everything else is a 'key=value' line. 'country', 'region'
    and 'region_updated' are written back automatically by the app itself
    (see _update_weather_region_cache) once alerts are enabled and the
    location has been resolved from the coordinates -- not meant to be
    hand-edited, though nothing breaks if they're missing or wrong."""
    lat = lon = None
    alerts_enabled = False
    country = None
    region = None
    region_updated = 0.0
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
            elif key == 'region_updated':
                try:
                    region_updated = float(val)
                except ValueError:
                    pass
        else:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                try:
                    lat, lon = float(parts[0]), float(parts[1])
                except ValueError:
                    pass
    return {
        'lat': lat, 'lon': lon, 'alerts_enabled': alerts_enabled,
        'country': country, 'region': region, 'region_updated': region_updated,
    }


def load_weather_coords():
    """Back-compat wrapper: just the (lat, lon) from load_weather_settings();
    (None, None) if no coordinates are configured."""
    settings = load_weather_settings()
    return settings['lat'], settings['lon']


def _edit_weather_section(transform):
    """Rewrites config.conf in place: `transform` receives the lines of the
    [weather] section (without the header; the section is created at the end
    of the file if it's somehow missing) and returns the replacement lines.
    Every other line -- including comments and other sections -- is left
    untouched. Best-effort: failures are swallowed (callers just redo the work
    next poll)."""
    try:
        with open(CONFIG_FILE) as f:
            lines = f.readlines()
    except OSError:
        lines = []

    def is_section(line, name=None):
        s = line.strip()
        if not (s.startswith('[') and s.endswith(']')):
            return False
        return name is None or s[1:-1].strip().lower() == name

    start = next((i for i, l in enumerate(lines) if is_section(l, 'weather')), None)
    if start is None:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append('[weather]\n')
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if is_section(lines[i])), len(lines))
    lines[start + 1:end] = transform(lines[start + 1:end])

    try:
        tmp = CONFIG_FILE + '.tmp'
        with open(tmp, 'w') as f:
            f.writelines(lines)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        pass


_RESOLVED_LOCATION_KEYS = ('country=', 'region=', 'region_updated=')


def _update_weather_region_cache(region, timestamp, country=None):
    """Sets country=/region=/region_updated= in [weather] (the weekly
    reverse-geocode cache)."""
    def transform(section):
        kept = [l for l in section if not l.strip().lower().startswith(_RESOLVED_LOCATION_KEYS)]
        return kept + ([f'country={country}\n'] if country else []) + [
            f'region={region}\n', f'region_updated={int(timestamp)}\n']
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
    was found. Rate-limited by
    design to once a week (see ALERTS_REGION_REFRESH_SECONDS) by the caller."""
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


def entry_id(entry, feed_url=None):
    """A stable id for entries with a real id/link. Entries lacking both
    (the fallback: title+published) are hashed together with feed_url when
    given, so the same title+date on two different feeds doesn't collide."""
    raw = entry.get('id') or entry.get('link')
    if raw:
        return hashlib.sha1(raw.encode('utf-8', 'ignore')).hexdigest()
    fallback = entry.get('title', '') + entry.get('published', '')
    if feed_url:
        fallback = feed_url + '\x00' + fallback
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
            live[channel] = stream.get('title') or ''
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


def _apply_stream_title(proc, url, site):
    """Replaces the placeholder window title with
    '<site> > <author> > <category> > <title>' from `streamlink --json`
    (external-http mode has no {author}/{category} substitution of its own)."""
    try:
        result = subprocess.run(['streamlink', '--json', url],
                                capture_output=True, text=True,
                                timeout=YOUTUBE_CHECK_TIMEOUT_SECONDS)
        meta = json.loads(result.stdout).get('metadata') or {}
    except Exception:
        return
    parts = [site, meta.get('author'), meta.get('category'), meta.get('title')]
    title = ' > '.join(p for p in parts if p)
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


def _play_in_shared_mpv(url, quality, site, channel, fallback_url):
    """Worker thread: serve the stream via streamlink, load it into the
    shared mpv. Falls back to the browser if anything fails (unless a newer
    click has superseded this one)."""
    _stop_streamlink()  # a new click replaces the previous stream
    port = _free_local_port()
    try:
        proc = subprocess.Popen(
            ['streamlink', '--player-external-http',
             '--player-external-http-interface', '127.0.0.1',
             '--player-external-http-port', str(port), url, quality],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors='replace')
    except Exception:
        webbrowser.open(fallback_url)
        return
    global _streamlink_proc
    with _player_lock:
        _streamlink_proc = proc
    # streamlink logs this line once its local server is listening; mpv can
    # connect from then on (it fetches the actual stream on first request).
    watchdog = threading.Timer(STREAMLINK_READY_TIMEOUT_SECONDS, proc.kill)
    watchdog.start()
    ready = False
    try:
        for line in proc.stderr:
            if 'access with one of' in line:
                ready = True
                break
    finally:
        watchdog.cancel()
    if not ready or not _load_in_mpv(f'http://127.0.0.1:{port}/', f'{site} > {channel}'):
        superseded = not _is_current_streamlink(proc)
        try:
            proc.terminate()
        except OSError:
            pass
        if not superseded:
            webbrowser.open(fallback_url)
        return
    threading.Thread(target=_apply_stream_title, args=(proc, url, site), daemon=True).start()
    threading.Thread(target=_stop_streamlink_when_mpv_exits, args=(proc,), daemon=True).start()
    for _line in proc.stderr:  # drain so streamlink never blocks on a full pipe
        pass


def _open_live_stream(url, quality, site, channel, fallback_url):
    if shutil.which('streamlink') and shutil.which('mpv'):
        try:
            threading.Thread(
                target=_play_in_shared_mpv,
                args=(url, quality, site, channel, fallback_url),
                daemon=True).start()
            return
        except Exception:
            pass
    webbrowser.open(fallback_url)


def open_twitch_stream(channel, quality='best', site='Twitch'):
    """Plays the stream in the shared, maximized mpv window (see above) if
    streamlink and mpv are installed; falls back to opening the channel in
    the browser otherwise, or if starting playback fails."""
    _open_live_stream(f'twitch.tv/{channel}', quality, site, channel,
                      f'https://twitch.tv/{channel}')


YOUTUBE_CHECK_INTERVAL_SECONDS = 1800  # how often to poll YouTube live status
YOUTUBE_CHECK_TIMEOUT_SECONDS = 20  # per-channel; this check shells out to
                                     # streamlink itself (no lightweight
                                     # keyless batch API exists for YouTube),
                                     # so it's checked sequentially, one
                                     # subprocess per channel


def check_youtube_live_channels(channels):
    """Uses `streamlink --json <channel>/live` per channel -- the same
    extraction path used for actual playback, so there's no risk of this
    check disagreeing with what clicking the row would actually do. No
    keyless YouTube API exists for batch-checking many channels in one
    request, so this is sequential, one streamlink invocation per channel.
    Returns {channel: title} for whichever channels are currently live (a
    not-live channel is simply absent, same convention as Twitch's check);
    returns None only if every single channel's check failed (e.g.
    streamlink isn't installed) -- an individual channel's request failing
    is treated the same as that channel not being live, and is simply
    retried next poll."""
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
            parsed = json.loads(result.stdout)
        except Exception:
            continue  # this channel's check failed; left absent, retried next poll
        any_success = True
        metadata = parsed.get('metadata')
        if metadata and metadata.get('title'):
            live[channel] = metadata['title']
    if not any_success:
        return None  # every channel's check failed (e.g. streamlink missing)
    return live


def open_youtube_stream(channel, quality='best'):
    """Same as open_twitch_stream (shared mpv window); falls back to the
    channel's page in the browser."""
    _open_live_stream(f'https://www.youtube.com/{channel}/live', quality,
                      'YouTube', channel, f'https://www.youtube.com/{channel}')


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
    lat, lon = load_weather_coords()
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
        self._youtube_lock = threading.Lock()  # guards overlapping YouTube-check cycles
        self.popup = None
        self.listbox = None
        self.scroller = None
        self.weather_data = None
        self.weather_box = None
        self.weather_view = 'today'
        self.active_alerts = []
        self._alert_pulse_counter = 0
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
        GLib.timeout_add(ALERT_PULSE_INTERVAL_MS, self._alert_pulse_tick)
        GLib.timeout_add_seconds(3, self.initial_twitch_check)
        GLib.timeout_add_seconds(TWITCH_CHECK_INTERVAL_SECONDS, self.periodic_twitch_check)
        GLib.timeout_add_seconds(4, self.initial_youtube_check)
        GLib.timeout_add_seconds(YOUTUBE_CHECK_INTERVAL_SECONDS, self.periodic_youtube_check)
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
                eid = entry_id(entry, url)
                # legacy (pre-collision-fix) hash for the same fallback entry —
                # checked too so items already recorded as seen don't resurface
                legacy_eid = entry_id(entry)
                if eid in seen_ids or legacy_eid in seen_ids:
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
            last_attempt = self.state.get('updates_last_attempt', 0)
            fails = self.state.get('updates_fail_count', 0)
        if not force:
            if (now - last) < PENDING_CHECK_INTERVAL_SECONDS:
                return
            if fails and (now - last_attempt) < min(300 * 2 ** (fails - 1),
                                                     PENDING_CHECK_INTERVAL_SECONDS):
                return  # backing off after failed scans
        pkgnames = list_all_updates()
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
                if live_now is not None:  # failed check: keep last known live state
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

    def initial_youtube_check(self):
        if not is_online():
            GLib.timeout_add_seconds(NETWORK_RETRY_SECONDS, self.initial_youtube_check)
            return False
        self.start_youtube_check()
        return False

    def periodic_youtube_check(self):
        self.start_youtube_check()
        return True

    def start_youtube_check(self):
        if not self._youtube_lock.acquire(blocking=False):
            return  # a check is already in flight — skip this tick
        threading.Thread(target=self._check_youtube_guarded, daemon=True).start()

    def _check_youtube_guarded(self):
        try:
            channels = load_youtube_channels()
            if channels:
                live_now = check_youtube_live_channels(channels)
                if live_now is not None:  # failed check: keep last known live state
                    GLib.idle_add(self._on_youtube_checked, live_now)
        finally:
            self._youtube_lock.release()

    def _on_youtube_checked(self, live_now):
        with self.lock:
            was_live = {e['channel'] for e in self.state.get('live_youtube_channels', [])}
            self.state['live_youtube_channels'] = [
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
        alerts = self._fetch_alerts_bg()
        GLib.idle_add(self._on_weather_fetched, data, alerts)

    def _fetch_alerts_bg(self):
        """Resolves the alert country/region (from lat/lon, cached weekly in
        config.conf) and fetches active MeteoAlarm alerts for it. Returns
        None on failure (caller keeps the previous alerts), [] if alerts
        are disabled or the region genuinely has nothing active."""
        settings = load_weather_settings()
        if not settings['alerts_enabled'] or settings['lat'] is None or settings['lon'] is None:
            return []
        country, region = settings['country'], settings['region']
        now = time_module.time()
        if not (country and region) or (now - settings['region_updated']) >= ALERTS_REGION_REFRESH_SECONDS:
            resolved = reverse_geocode_region(settings['lat'], settings['lon'])
            if resolved:
                new_country, new_region, code = resolved
                if code and code not in METEOALARM_COUNTRY_CODES:
                    _disable_weather_alerts()  # no MeteoAlarm feed for this country
                    return []
                if new_country and new_region:
                    country, region = new_country, new_region
                    _update_weather_region_cache(region, now, country=country)
                elif not (country and region):
                    return None  # country known but no region, nothing cached
            elif not (country and region):
                return None  # never resolved, and this attempt also failed
            # else: geocoding failed but a stale region is cached -- keep
            # using it; region_updated is left untouched so it retries
            # next poll instead of waiting a full week
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

    def _alert_pulse_tick(self):
        self._alert_pulse_counter += 1
        if self.has_active_alerts():
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
        for day in d.get('forecast_days', []):
            segment = ''
            if day.get('rain_prob') is not None and day['rain_prob'] > 0:
                segment += '<span foreground="#2b2b2b">☔</span> '
            if day.get('max_temp') is not None and day.get('min_temp') is not None:
                segment += GLib.markup_escape_text(
                    f"{day['max_temp']:.0f}/{day['min_temp']:.0f}°C"
                )
            if segment:
                segments.append(f'<span size="large"><b>{segment}</b></span>')
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
        if d.get('temp') is not None:
            temp_text = GLib.markup_escape_text(f"{d['temp']:.0f}°C")
            if glyph:
                glyph_color_name = seg_colors.get('glyph')
                if glyph_color_name:
                    bright, dim = ALERT_TEXT_COLORS[glyph_color_name]
                    glyph_color = bright if self._pulse_on_for(glyph_color_name) else dim
                else:
                    glyph_color = '#2b2b2b'
                temp_text = f'<span foreground="{glyph_color}">{glyph}</span> ' + temp_text
            segments.append(f'<span size="large"><b>{temp_text}</b></span>')
        if d.get('today_max_temp') is not None and d.get('today_min_temp') is not None:
            hi_lo = colorize(GLib.markup_escape_text(
                f"{d['today_max_temp']:.0f}/{d['today_min_temp']:.0f}°C"
            ), 'hilo')
            segments.append(f'<span size="large"><b>{hi_lo}</b></span>')
        if d.get('wind') is not None and d.get('today_max_wind') is not None:
            wind_text = colorize(GLib.markup_escape_text(
                f"{d['wind']:.0f}/{d['today_max_wind']:.0f} km/h"
            ), 'wind')
            segments.append(f'<span size="large"><b>{wind_text}</b></span>')
        elif d.get('wind') is not None:
            wind_text = colorize(GLib.markup_escape_text(f"{d['wind']:.0f} km/h"), 'wind')
            segments.append(f'<span size="large"><b>{wind_text}</b></span>')
        if d.get('today_rain_prob') is not None and d.get('today_precip_sum') is not None:
            rain_text = colorize(GLib.markup_escape_text(
                f"{d['today_rain_prob']:.0f}%/{d['today_precip_sum']:.1f}mm"
            ), 'rain')
            segments.append(f'<span size="large"><b>{rain_text}</b></span>')
        elif d.get('today_rain_prob') is not None:
            rain_text = colorize(GLib.markup_escape_text(f"{d['today_rain_prob']:.0f}%"), 'rain')
            segments.append(f'<span size="large"><b>{rain_text}</b></span>')
        return segments

    def _build_weather_row(self, segments, empty_markup):
        """Spreads `segments` evenly across the bar's full width with a bullet
        between each pair; shared by the 'today' and 'forecast' views so
        they look identical."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.set_hexpand(True)
        if not segments:
            segments, separate = [empty_markup], False
        else:
            separate = True
        for n, seg in enumerate(segments):
            if separate and n > 0:
                bullet = Gtk.Label()
                bullet.set_markup(WEATHER_BULLET_MARKUP)
                bullet.set_valign(Gtk.Align.END)
                row.pack_start(bullet, False, False, 0)
            lbl = Gtk.Label()
            lbl.set_markup(seg)
            lbl.set_hexpand(True)
            lbl.set_xalign(0.5)
            lbl.set_valign(Gtk.Align.END)
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

            empty = ('<span size="large">Weather unavailable</span>' if not self.weather_data
                     else '<span size="large">Forecast unavailable</span>')
            row = self._build_weather_row(self.build_forecast_weather_segments(), empty)
            self.weather_box.pack_start(row, True, True, 0)
        else:
            row = self._build_weather_row(
                self.build_today_weather_segments(),
                '<span size="large">Weather unavailable</span>')
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
        if self.has_active_alerts():
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
        list { background-color: #ffffff; }
        viewport { background-color: #ffffff; }  /* ScrolledWindow auto-wraps
                                                      the listbox in a GtkViewport,
                                                      which paints its own
                                                      background over the list's */
        .weather-bar { background-color: #e8eef5; }
        .timer-bar { background-color: #ffffff; }
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
        self.listbox.set_margin_bottom(85)  # timer_box is 60px tall, valign END; the extra
                                             # 25px keeps a visible gap above it instead of
                                             # timer_box's top edge sitting flush against the
                                             # last row
        scroller.add(self.listbox)

        content_overlay = Gtk.Overlay()
        content_overlay.add(scroller)

        timer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        timer_box.set_margin_start(8)
        timer_box.set_margin_end(8)
        timer_box.set_margin_top(3)
        timer_box.set_margin_bottom(4)
        timer_box.set_size_request(-1, 60)
        timer_box.set_halign(Gtk.Align.FILL)
        timer_box.set_valign(Gtk.Align.END)
        timer_box.get_style_context().add_class('timer-bar')
        timer_box.set_no_show_all(True)
        timer_box.set_visible(self.timer_visible)

        timer_label = Gtk.Label()
        timer_label.set_xalign(0.5)
        timer_label.set_text(format_timer_duration(self.timer_remaining_seconds))
        self.timer_label = timer_label
        timer_box.pack_start(timer_label, False, False, 0)

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
        timer_box.pack_start(timer_scale, False, False, 0)

        self.timer_box = timer_box
        content_overlay.add_overlay(timer_box)
        # timer_box has no_show_all(True) so that the popup's own show_all()
        # doesn't reveal it prematurely — but that flag ALSO blocks show_all()
        # called on timer_box itself (no_show_all stops recursion at the
        # widget that has it set, however show_all() was invoked). So its
        # children must be shown individually with .show(), which no_show_all
        # does not affect, rather than via any show_all() call on the box.
        timer_label.show()
        timer_scale.show()

        outer.pack_start(content_overlay, True, True, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        footer.set_margin_start(6)
        footer.set_margin_end(6)
        footer.set_margin_top(4)
        footer.set_margin_bottom(4)
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

    def build_twitch_row(self, entry):
        channel = entry['channel']
        title = entry.get('title') or ''

        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(True)
        row.twitch_channel = channel
        row.twitch_quality = load_twitch_qualities().get(channel, 'best')

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(0)
        box.set_margin_bottom(0)

        dot = Gtk.Label()
        dot.set_markup('<span foreground="#9146FF"><b>●</b></span>')
        box.pack_start(dot, False, False, 0)

        channel_esc = GLib.markup_escape_text(channel)
        trimmed = False
        if title:
            prefix_len = len(channel) + 3  # " - "
            available = max(0, MAX_TITLE_LEN - prefix_len)
            if len(title) > available:
                cut_len = max(available - 1, 0)
                shown_title = (title[:cut_len] + '…') if cut_len > 0 else '…'
                trimmed = True
            else:
                shown_title = title
            title_esc = GLib.markup_escape_text(shown_title)
            markup = f'<span foreground="#000000"><b>{channel_esc}</b> - {title_esc}</span>'
        else:
            markup = f'<span foreground="#000000"><b>{channel_esc}</b></span>'

        label = Gtk.Label()
        label.set_markup(markup)
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        box.pack_start(label, True, True, 0)

        if trimmed:
            row.set_tooltip_text(title)
            label.set_tooltip_text(title)

        row.add(box)
        return row

    def build_youtube_row(self, entry):
        channel = entry['channel']
        title = entry.get('title') or ''

        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(True)
        row.youtube_channel = channel
        row.youtube_quality = load_youtube_qualities().get(channel, 'best')

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(0)
        box.set_margin_bottom(0)

        dot = Gtk.Label()
        dot.set_markup('<span foreground="#FF0000"><b>\u25cf</b></span>')
        box.pack_start(dot, False, False, 0)

        channel_esc = GLib.markup_escape_text(channel)
        trimmed = False
        if title:
            prefix_len = len(channel) + 3  # " - "
            available = max(0, MAX_TITLE_LEN - prefix_len)
            if len(title) > available:
                cut_len = max(available - 1, 0)
                shown_title = (title[:cut_len] + '\u2026') if cut_len > 0 else '\u2026'
                trimmed = True
            else:
                shown_title = title
            title_esc = GLib.markup_escape_text(shown_title)
            markup = f'<span foreground="#000000"><b>{channel_esc}</b> - {title_esc}</span>'
        else:
            markup = f'<span foreground="#000000"><b>{channel_esc}</b></span>'

        label = Gtk.Label()
        label.set_markup(markup)
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        box.pack_start(label, True, True, 0)

        if trimmed:
            row.set_tooltip_text(title)
            label.set_tooltip_text(title)

        row.add(box)
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
                    for line in proc.stdout:
                        stripped = line.strip()
                        if stripped.startswith('[*] Downloading'):
                            self._set_status(pkgname, 'Downloading…')
                        elif stripped.startswith('[*]'):
                            self._set_status(pkgname, 'Installing…')
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
        if getattr(row, 'install_all_header', False):
            self.install_all_updates()
            return
        if hasattr(row, 'header_feed_url'):
            self.mark_feed_read(row.header_feed_url)
            return
        if hasattr(row, 'twitch_channel'):
            if self.popup:
                self.popup.hide()
            open_twitch_stream(row.twitch_channel, getattr(row, 'twitch_quality', 'best'))
            return
        if hasattr(row, 'youtube_channel'):
            if self.popup:
                self.popup.hide()
            open_youtube_stream(row.youtube_channel, getattr(row, 'youtube_quality', 'best'))
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
