# rss-tray

A minimal tray-based RSS/Atom reader for Linux, built with Python and GTK3 as a lightweight replacement for QuiteRSS. It also shows weather, Void package updates, Twitch live channels and a countdown timer in the same popup. Written for XFCE on Void Linux; it should work on any desktop with a `Gtk.StatusIcon`-compatible tray.

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
- **Twitch:** live channels are polled every 30 minutes through Twitch's unofficial GQL API (no app registration). Click a row to open the stream with `streamlink --player mpv`.
- **Timer:** a 0-2 h slider behind the "Timer" footer button. It keeps counting with the popup closed and plays the notification sound twice at zero.

## Requirements

- Python 3, PyGObject (GTK3), pycairo, `feedparser`
- `xbps-install` / `xbps-query` for update detection (Void Linux only)
- `streamlink` and `mpv` for Twitch streams
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
| `config.conf` | feeds, mute phrases, Twitch channels |
| `state.json` | read/unread state, timestamps, cached updates and live channels (safe to delete to reset) |
| `notification.wav` | sound played on new items and when the timer ends |

`config.conf` is INI-like:

```ini
[feeds]
# URL|display name|check interval in minutes   (name and interval optional)
https://example.com/feed.xml|My Blog|5

[mute]
# one phrase per line; matching items are dropped
sponsored

[twitch]
# one channel name per line
somechannel
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
- Threading: `lock` guards state, `_check_lock` RSS cycles, `_xbps_lock` xbps scans and installs, `_twitch_lock` Twitch checks. All GTK mutations from threads go through `GLib.idle_add`.
- A failed xbps scan or Twitch request keeps the previous state; failed scans back off exponentially up to the normal hourly interval.
- The install timeout kills the whole process group (`sudo` and the `xbps-install` it forked).

## Known limitations

- `Gtk.StatusIcon` is deprecated upstream but still works on XFCE and most X11 panels.
- Twitch's GQL API is unofficial and undocumented; it can break without notice.
- Update detection and installs are Void-specific.

## License

MIT, see [LICENSE](LICENSE).
