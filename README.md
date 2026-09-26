# rss-tray

A compact GTK3 system-tray RSS/Atom reader for Linux desktops, with optional system-wide Void Linux package-update detection, weather, Twitch live-channel alerts, and a small countdown timer.

Originally developed for XFCE on Void Linux. The application uses a legacy `Gtk.StatusIcon` tray item and a borderless GTK popup.

## Features

- **Tray icon with unread badge**
  - green: nothing unread
  - orange: unread RSS items
  - red: system package updates available
  - when there are no unread/package updates, the icon can show the current temperature
- **Compact popup** grouped by feed
- **Mark read** per item, per feed, or all at once
- **Custom feed names**
- **Per-feed polling intervals**
- **24-hour first-seen cutoff** for newly configured feeds
- **Persistent state** in `~/.config/rss-tray/state.json`
- **System-wide XBPS update detection** on Void Linux
- **Twitch live-channel monitoring** using Twitch's internal, unofficial GraphQL endpoint
- **Open-Meteo weather** with today/forecast views
- **Countdown timer** embedded in the popup
- **Notification sound** and optional automatic popup when new RSS/package/Twitch items arrive

## Requirements

- Python 3
- PyGObject / GTK3
- pycairo
- feedparser
- A GTK3-compatible system tray / notification area
- Optional for package updates: `xbps-query` and `xbps-install`
- Optional for Twitch playback: `streamlink` and `mpv`

On Void Linux:

```bash
sudo xbps-install -Sy python3-gobject python3-cairo python3-feedparser
```

## Installation

```bash
git clone https://github.com/defaultpoi/rss-tray.git
cd rss-tray
mkdir -p ~/.local/bin
cp rss-tray.py ~/.local/bin/rss-tray.py
chmod +x ~/.local/bin/rss-tray.py
```

Run it once manually:

```bash
~/.local/bin/rss-tray.py
```

### Autostart

Create `~/.config/autostart/rss-tray.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=RSS Tray
Exec=/home/YOUR_USER/.local/bin/rss-tray.py
Icon=application-rss+xml
Terminal=false
X-GNOME-Autostart-enabled=true
```

Replace `YOUR_USER` with the account that owns the installation.

## Configuration

The canonical configuration file is:

```
~/.config/rss-tray/config.conf
```

On first run the application creates it with these sections:

```ini
[feeds]
# URL|custom display name (optional)|check interval in minutes (optional)
https://example.com/feed.xml
https://example.com/feed.xml|My Blog
https://example.com/feed.xml|My Blog|5

[mute]
# One phrase per line, or several separated by |
Fashion week
another phrase|third phrase

[twitch]
# Twitch login names, one per line
examplechannel
```

Feed intervals are independent. The scheduler checks once per minute and fetches only feeds whose configured interval has elapsed.

Older installations using `feeds.conf` and `mute.conf` are migrated into `config.conf` once; the legacy files are left untouched.

## Package updates

Package detection is **system-wide**. It is not tied to an RSS feed and does not attempt to match package names against void-packages commits.

The application performs a read-only:

```bash
xbps-install -Mn -u
```

scan approximately hourly. Scans and installs are serialized so they cannot access the XBPS database concurrently.

Clicking **Updates available** installs packages sequentially with:

```bash
sudo -n xbps-install -Su -y <package>
```

Each package gets its own Waiting / Downloading / Installing / Done / Failed status.

### Passwordless sudo

Because package updates run from a background thread without a terminal, `sudo -n` must be able to execute the command without prompting.

Create a narrowly scoped sudoers rule with:

```bash
sudo visudo -f /etc/sudoers.d/rss-tray
```

For example:

```text
YOUR_USER ALL=(root) NOPASSWD: /usr/bin/xbps-install -Su *
```

Adjust the path to `xbps-install` if necessary.

This gives processes running as `YOUR_USER` permission to invoke that command as root. Use only if that trade-off is acceptable.

If `sudo -n` cannot run the command, the update is reported as failed rather than waiting for a password.

## Persistent state

State is stored at:

```
~/.config/rss-tray/state.json
```

It contains, among other things:

- seen RSS item IDs
- unread RSS items
- available package updates
- live Twitch channels
- per-feed last-check timestamps
- XBPS last successful check/attempt and retry backoff

The file is written through a temporary file followed by `os.replace()` so a completed write replaces the previous state atomically.

If the file is malformed or individual fields have the wrong type, those fields are reset to safe defaults rather than crashing the application.

## Network and failure behavior

The application uses explicit timeouts for network operations.

At startup it retains a short HTTPS connectivity gate to avoid the known XFCE/network-startup race, but the check uses an actual HTTPS endpoint rather than assuming TCP/53 access to a particular DNS server.

External failures are kept separate from successful empty results:

- a failed RSS request does not advance that feed's successful-check timestamp
- a successfully fetched empty feed is still considered checked
- a failed Twitch request preserves the previous live-channel state
- a successful Twitch response with no live channels clears the live state
- a failed XBPS scan preserves the previous update list and uses retry backoff
- weather failures preserve the previous weather data

## Popup and threading behavior

GTK widgets are updated only on the GTK main thread. Background work uses worker threads and schedules UI changes with `GLib.idle_add()`.

Separate locks protect:

- RSS/feed-check cycles
- XBPS database access
- Twitch-check cycles

The popup is intentionally **destroyed and recreated every time it opens**. This avoids a historical GTK sizing regression and should not be changed to a persistent window without reproducing that issue first.

The countdown timer remains an overlay inside the popup because moving it into a separate window caused focus/lifecycle problems.

Popup placement follows the monitor containing the tray icon, uses that monitor's workarea, remains flush-right, and correctly handles monitors with negative coordinates.

## Testing

Run the regression tests with:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover source parsing plus core state/parser and failure-semantics behavior without requiring a running GTK desktop.

A syntax-only check is also useful:

```bash
python3 -m py_compile rss-tray.py
```

## Known limitations

- `Gtk.StatusIcon` is deprecated upstream and depends on desktop/tray support.
- Twitch monitoring uses an undocumented internal GraphQL endpoint and may break if Twitch changes it.
- Twitch playback depends on external `streamlink`/player installation.
- Package status is inferred from XBPS command output rather than a dedicated machine-readable API.
- The application remains intentionally compact and centered around a single Python module.

## License

MIT — see [LICENSE](LICENSE).
