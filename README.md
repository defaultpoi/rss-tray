# rss-tray

A minimal tray-based RSS/Atom reader for Linux, built with Python and GTK3 as a lightweight replacement for QuiteRSS. It also shows weather, Void package updates, Twitch and YouTube live channels, and a countdown timer in the same popup. Written for XFCE on Void Linux; it should work on any desktop with a `Gtk.StatusIcon`-compatible tray.

## Features

- **Tray icon** with three states:
  - green: nothing unread (shows the current temperature)
  - orange: unread news
  - red: package updates available

  Live Twitch channels are deliberately not counted in the badge.
- **Popup list** grouped by feed. Left-click a row to open it and mark it read, right-click a row to mark it read only, click a feed header to mark the whole feed read. The popup auto-opens on new items and closes on focus-out.
- **Feeds:** per-feed check intervals, custom display names, mute phrases. Items older than 24 h are silently marked seen the first time they are seen.
- **Weather** (Open-Meteo): today's temperature, high/low, wind and rain, with a 5-day forecast behind the `›` button.
- **Void package updates:** a system-wide `xbps-install -Mn -u` dry run every hour. Click the "Updates available" header to install everything, one package at a time, with live status.
- **Twitch:** live channels are polled (every 30 minutes by default, `interval=` in `[twitch]`) through Twitch's unofficial GQL API (no app registration). Rows read `<user> - <category>` (the stream title is the tooltip), and the bullet turns into a play triangle on the channel that's playing. Click a row to play it with `streamlink` and `mpv` at the configured quality (default `best`); the popup closes, and a second click on another channel reuses the same maximized mpv window. If either binary isn't installed, or launching fails, it opens the channel in your browser instead.
- **YouTube Live:** live channels are polled (every 30 minutes by default, `interval=` in `[youtube]`) via `streamlink --json <channel>/live` per channel -- no API key, and the same extraction path used for actual playback, so the check can't disagree with what clicking the row does. There's no keyless batch API for YouTube, so this is one streamlink call per channel (heavier than Twitch's single batched request). Shares the same "Live now" list, quality config, and browser fallback as Twitch.
- **Player window titles:** mpv's title is `<Site> · <user> · <stream title>` for both platforms.
- **Timer:** a 0-2 h slider behind the "Timer" footer button. It keeps counting with the popup closed and plays the notification sound twice at zero.
- **Severe-weather alerts** (optional, `alerts=true` in `[weather]`): polls the MeteoAlarm feed for your country every 30 minutes, filtered to the region matching the configured coordinates (country and region are reverse-geocoded via OpenStreetMap and re-checked weekly; Europe only, since MeteoAlarm only covers European countries; if your coordinates resolve to a country it doesn't cover, the app writes `alerts=false` into `config.conf` itself). An active alert is color-coded by severity (yellow/orange/red for moderate/severe/extreme) and pulses the matching weather value and the tray badge, faster for more severe alerts.

## Requirements

- Python 3, PyGObject (GTK3), pycairo, `feedparser`
- `xbps-install` / `xbps-query` for update detection (Void Linux only)
- `streamlink` and `mpv` for Twitch and YouTube Live streams
- A command-line audio player for the notification sound

```bash
sudo xbps-install -S python3-gobject python3-cairo python3-feedparser streamlink mpv
```

## Installation

```bash
git clone https://github.com/defaultpoi/rss-tray.git
cd rss-tray
install -Dm755 rss-tray.py ~/.local/bin/rss-tray.py
~/.local/bin/rss-tray.py   # run once in the foreground to see any tracebacks
```

Autostart: create `~/.config/autostart/rss-tray.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=RSS Tray
Exec=/home/YOUR_USER/.local/bin/rss-tray.py
Terminal=false
```

## Configuration

Everything lives in `~/.config/rss-tray/`:

| File | Purpose |
|------|---------|
| `config.conf` | feeds, mute phrases, Twitch and YouTube channels |
| `state.json` | read/unread state, timestamps, cached updates and live channels (safe to delete to reset) |
| `notification.wav` | sound played on new items and when the timer ends |

`config.conf` is INI-like:

```ini
[feeds]
# URL|display name|check interval in minutes   (name and interval optional)
https://example.com/feed.xml|My Blog|5

[mute]
# case-insensitive phrases, several per line separated by |; titles containing any are dropped
sponsored

[twitch]
# one channel name per line; optionally |quality (streamlink format, e.g.
# 720p60, 1080p60) -- defaults to "best" if omitted.
# interval=<minutes> sets how often to check for live channels (default 30,
# minimum 1).
interval=30
somechannel
somechannel2|720p60

[youtube]
# one channel identifier per line -- whatever goes after youtube.com/, so
# either @handle or channel/UCxxxxxxxxxxxxxxxxxxxxxx (case-sensitive,
# unlike Twitch names). Optionally |quality, same as [twitch].
# interval=<minutes> between live checks (default 30, minimum 5 -- each
# channel costs a full streamlink run).
interval=30
@somehandle
channel/UCxxxxxxxxxxxxxxxxxxxxxx|720p60

[weather]
# lat|lon for the weather bar (Open-Meteo, no key needed); the weather
# bar stays hidden until this is set
<latitude>|<longitude>
alerts=true

[timer]
# max=<minutes> caps the slider (default 120); step=<seconds> sets the
# arrow-key/scroll increment (default 60). Both optional.
max=120
step=60
```

## Passwordless updates (optional)

Installs run `sudo -n xbps-install -Su -y <pkg>`, which fails immediately if a password would be needed. To allow it without a password, add a scoped rule:

```bash
sudo visudo -f /etc/sudoers.d/zz-rss-tray
```

```
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/xbps-install -Su -y *
```

The `zz-` prefix matters: sudoers uses the last matching rule, so this file must sort after anything that might override it.

**Security note:** this lets any process running as your user run `xbps-install -Su -y` as root without a password, not just this app.

## Design notes

- The popup is a borderless `Gtk.Window` that is destroyed and rebuilt on every open. This avoids stuck-size bugs; do not make it persistent or call `resize()` on it.
- Package updates are system-wide rather than feed-based, because GitHub's atom feed is a sliding window and version bumps get missed.
- Threading: `lock` guards state, `_check_lock` RSS cycles, `_xbps_lock` xbps scans and installs, `_twitch_lock`/`_youtube_lock` their respective live-channel checks. All GTK mutations from threads go through `GLib.idle_add`.
- A failed xbps scan or Twitch request keeps the previous state; failed scans back off exponentially up to the normal hourly interval.
- The install timeout kills the whole process group (`sudo` and the `xbps-install` it forked).

## Known limitations

- `Gtk.StatusIcon` is deprecated upstream but still works on XFCE and most X11 panels.
- Twitch's GQL API is unofficial and undocumented; it can break without notice.
- YouTube Live detection depends on streamlink's own YouTube plugin staying current with YouTube's changes; a channel's liveness check takes a few seconds (full streamlink extraction), unlike Twitch's near-instant batched check.
- Update detection and installs are Void-specific.

## Tests

Run `python3 -m unittest discover -s tests -v` (needs PyGObject and feedparser installed). CI runs the same on every push.

## License

MIT, see [LICENSE](LICENSE).
