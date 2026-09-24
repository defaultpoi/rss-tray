# rss-tray

A minimal, fast tray-based RSS/Atom reader for Linux desktops — a lightweight alternative to QuiteRSS. Built with Python and GTK3, it sits in your system tray, shows an unread count, and lets you triage feed items from a compact dropdown without a full application window.

Originally built for XFCE on Void Linux, but should work on any Linux desktop with a GTK3-compatible system tray (KDE, GNOME with an extension, most other X11/Wayland-via-XWayland setups).

## Features

- **Tray icon with unread badge** — the icon itself shows the unread count and changes color:
  - 🟢 green — nothing unread
  - 🟠 orange — unread items waiting
  - 🔴 red — a followed package has an update available (see below)
- **Compact dropdown list** — click the tray icon to see unread items, grouped by feed with a header divider between groups. The list auto-opens whenever new items arrive, and stays open as you clear items until the list is empty.
- **Per-item actions** — click a title to open it in your browser and mark it read; click the small mail icon on a row to mark it read without opening it; click a feed's header to mark that entire feed's items read at once.
- **Custom feed names** — override a feed's often-verbose title with a short display name.
- **Per-feed check intervals** — override the global check frequency for individual feeds (e.g. check a fast-moving feed every 5 minutes, a quiet one every hour).
- **Ignores backlog on new feeds** — when you first add a feed, only items from the last 24 hours are surfaced as unread; older entries are silently marked as seen instead of flooding your list.
- **Void package-update detection** — periodically checks the system-wide XBPS update list and shows available package updates in the popup. “Install all” runs one full `xbps-install -Su` transaction.
- **Persistent state, pruned automatically** — read/unread status and per-feed check timestamps survive restarts; seen-item records older than 30 days are pruned so the state file doesn't grow forever.

## Requirements

- Python 3
- PyGObject (GTK3 bindings) — package `python3-gobject` on Void
- `python3-cairo` (pycairo) — for rendering the tray icon badge
- `feedparser` — via your distro's package manager (`python3-feedparser` on Void) or `pip install --user feedparser`
- A GTK3-compatible system tray / notification area in your desktop environment
- `xbps-query` / `xbps-install` on the `PATH` if you use the package-update detection feature (Void Linux only)
- `sudo` (or adapt `PRIVILEGE_CMD` in the script if you use another privilege helper); updates run headlessly and require non-interactive authentication

### Install dependencies on Void Linux

```bash
sudo xbps-install -Sy python3-gobject python3-cairo python3-feedparser
```

## Installation

```bash
git clone https://github.com/defaultpoi/rss-tray.git
cd rss-tray
cp rss-tray.py ~/.local/bin/rss-tray.py
chmod +x ~/.local/bin/rss-tray.py
```

Run it once manually to check for errors and confirm the tray icon appears:

```bash
~/.local/bin/rss-tray.py
```

### Autostart on login (XFCE / most desktop environments)

Create `~/.config/autostart/rss-tray.desktop` with:

```ini
[Desktop Entry]
Type=Application
Name=RSS Tray
Exec=/home/YOUR_USER/.local/bin/rss-tray.py
Icon=application-rss+xml
Terminal=false
X-GNOME-Autostart-enabled=true
```

Replace `YOUR_USER` with your actual username.

## Configuration

On first run, a default config is created at `~/.config/rss-tray/config.conf`. It uses INI-style sections:

```ini
[feeds]
https://example.com/feed.xml
https://example.com/feed.xml|My Blog
https://example.com/feed.xml|My Blog|5

[mute]
# One phrase per line, or several separated by | on one line.
(Fashion)|spoiler

[twitch]
# Twitch channel login names to watch, one per line.
examplechannel
```

Feed fields are positional: URL, optional display name, optional check interval in minutes. The old `feeds.conf` and `mute.conf` files are migrated automatically when no `config.conf` exists; the legacy files are left untouched.

Edit the config directly, or use the **"Edit config"** button in the dropdown. Changes are picked up on the next scheduled or manual check.

### State file

Read/unread status, seen-item history, and per-feed check timestamps are stored in `~/.config/rss-tray/state.json`. Delete this file to reset everything from scratch (e.g. for testing).

## Package updates

The package-update check is read-only and runs `xbps-install -Mn -u`. Installing all detected updates runs one headless `sudo -n xbps-install -Su -y` transaction. If `sudo -n` cannot authenticate, the update is reported as failed rather than opening a terminal or waiting for input.

**Security note:** if you configure passwordless sudo for this command, scope the rule narrowly to the exact command and understand that any process running as your user could invoke it.

## How it works

- A background thread checks feeds on a schedule (default: every 10 minutes per feed, overridable per feed), with network timeouts applied to individual requests.
- New entries are matched by a hash of their `id`/`link`/title+date, so previously-seen items won't reappear even after a restart.
- Entries older than 24 hours are marked seen but not surfaced as unread the first time a feed is checked — this only matters when a feed is brand new to your config.
- The tray icon is drawn on the fly with Cairo (a colored circle with the unread count), avoiding any dependency on icon themes for the badge itself.
- The dropdown is a plain `Gtk.Window` styled as a borderless popup (not a `Gtk.Menu`), which is what allows it to stay open across multiple clicks — necessary for "mark as read without closing" and "auto-reopen with new items" to work.

## Known limitations

- XBPS update detection depends on the local Void package manager and repository metadata being available. A failed scan leaves previously detected updates intact rather than treating the failure as “no updates”.
- `Gtk.StatusIcon` (used for the tray icon) is deprecated upstream in favor of StatusNotifier/AppIndicator APIs, but remains functional on XFCE and most X11 panels. If your desktop environment drops support for it, the tray icon may stop appearing.
- No desktop notifications (e.g. via `notify-send`) are sent when new items arrive — the popup auto-opening is the current mechanism for surfacing new items.

## License

MIT — see [LICENSE](LICENSE).