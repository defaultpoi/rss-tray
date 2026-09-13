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
- **Void package-update detection (optional, per feed)** — flag a feed (e.g. void-packages' commit feed) as a `pkgfeed`; entries whose title matches an installed package are highlighted with an update icon, and clicking one (after a confirmation prompt) runs `xbps-install -Su <package>` in a terminal.
- **Persistent state, pruned automatically** — read/unread status and per-feed check timestamps survive restarts; seen-item records older than 30 days are pruned so the state file doesn't grow forever.

## Requirements

- Python 3
- PyGObject (GTK3 bindings) — package `python3-gobject` on Void
- `python3-cairo` (pycairo) — for rendering the tray icon badge
- `feedparser` — via your distro's package manager (`python3-feedparser` on Void) or `pip install --user feedparser`
- A GTK3-compatible system tray / notification area in your desktop environment
- `xbps-query` / `xbps-install` on the `PATH` if you use the package-update detection feature (Void Linux only)
- A terminal emulator for running package updates — the script looks for `xfce4-terminal`, falling back to `x-terminal-emulator`

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

On first run, a default config is created at `~/.config/rss-tray/feeds.conf`. Add one feed per line: