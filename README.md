# rss-tray

A minimal tray-based RSS/Atom reader for Linux, built with Python and GTK3 as a lightweight replacement for QuiteRSS. It also shows weather, Void package updates, Twitch and YouTube live channels, and a countdown timer in the same popup. Written for XFCE on Void Linux; it should work on any desktop with a `Gtk.StatusIcon`-compatible tray.

## Features

### Tray icon, popup and feeds

- **Tray icon** with three states:
  - green: nothing unread (shows the current temperature)
  - orange: unread news
  - red: package updates available

  Live Twitch and YouTube channels are deliberately not counted in the badge.
- **Popup list** grouped by feed. Left-click a row to open it and mark it read, right-click a row to mark it read only, click a feed header to mark the whole feed read. The popup auto-opens on new items and closes on focus-out; if it is already open when something new arrives, the list is updated in place (scroll position and an open Launch list are kept).
- **Feeds:** per-feed check intervals, custom display names, mute phrases. Items older than 24 h are silently marked seen the first time they are seen. A feed that fails to load is retried after 2 minutes rather than a full interval, and feeds are not polled while offline. At most 500 unread items are kept; the oldest beyond that are dropped (they stay marked as seen and never come back).

### Live streams

- **Twitch:** live channels are polled (every 15 minutes by default, `interval=` in `[twitch]`; optionally only during per-channel schedules, see below) through Twitch's unofficial GQL API (no app registration), in batches of 20 channels. Rows read `<user> - <category>` (the stream title is the tooltip), and the bullet turns into a play triangle on the channel that's playing. Click a row to play it with `streamlink` and `mpv` at the configured quality (default `best`); the popup closes, and a second click on another channel reuses the same maximized mpv window. If either binary isn't installed, or launching fails, it opens the channel in your browser instead.
- **YouTube Live:** live channels are polled (every 15 minutes by default, `interval=` in `[youtube]`; same per-channel schedules) via `streamlink --json <channel>/live` per channel -- no API key, and the same extraction path used for actual playback, so the check can't disagree with what clicking the row does. There's no keyless batch API for YouTube, so this is one streamlink call per channel (heavier than Twitch's single batched request). Shares the same "Live now" list, quality config, and browser fallback as Twitch.
- **Player window titles:** mpv's title is `<Site> · <user> · <stream title>` for both platforms.

### Weather

- **Weather** (Open-Meteo): today's temperature, high/low, wind and rain, with a 5-day forecast behind the `›` button. Refreshed every 30 minutes by default (`interval=<minutes>` in `[weather]`, minimum 5).
- **Severe-weather alerts** (optional, `alerts=true` in `[weather]`): polls the MeteoAlarm feed for your country on the same cycle as the weather, filtered to the region matching the configured coordinates (country and region are reverse-geocoded via OpenStreetMap once and then stored in `config.conf`; delete the `country=`/`region=` lines to resolve them again; Europe only, since MeteoAlarm only covers European countries; if your coordinates resolve to a country it doesn't cover, the app writes `alerts=false` into `config.conf` itself). An active alert is color-coded by severity (yellow/orange/red for moderate/severe/extreme) and pulses the matching weather value and the tray badge, faster for more severe alerts.

### Package updates

- **Void package updates:** a system-wide `xbps-install -Mn -u` dry run twice a day by default (`interval=<hours>` in `[updates]`). Click the "Updates available" header to install everything, one package at a time, with live status. A scan is skipped while an install is running.

### Tools

- **Timer:** a slide behind the "Timer" footer button with two modes, switched at its top left:
  - **Countdown:** a slider (0 to 2 h in 1-minute steps by default; the maximum and the step are configurable with `max=<minutes>` and `step=<seconds>` in `[timer]`, see below). It counts down against a fixed deadline, keeps counting with the popup closed, and plays the notification sound twice at zero.
  - **Schedule:** recurring actions instead of a sound, e.g. suspend the machine every night and lock the screen on weekday mornings (see "Scheduled actions" below). Add as many schedules as you like; each has its own time, days and action, set right in the slide and stored in `config.conf`.
- **Launcher:** a "Launch" footer button opens a list of your own commands (the `[launcher]` section, `Label|command` per line); clicking one runs it detached through the shell and closes the popup. The list is read when you open it, so edits apply immediately.

### Connectivity

- **Offline indicator:** a red "Offline" banner appears under the weather bar when the internet stops working, and goes away when it is back.
  - **Detection:** a probe runs every 10 s and when the popup opens. It checks that a public address accepts a connection **and** that a hostname resolves, so a DNS-only outage (the link looks fine, but nothing with a name works) counts too. A failed probe is re-checked after 3 s, and a failing feed, weather or live-channel request triggers a probe at once (at most one every 5 s), so the banner shows up within seconds. It takes two failed probes in a row to go offline, so one dropped probe doesn't flash it.
  - **Cause:** hover the banner for what failed and since when: DNS lookups failing, the router answering but the internet being unreachable (provider or upstream), the router not answering, or no default route (the link is down). The same line goes to the debug log (`RSS_TRAY_DEBUG=1`).
  - **Reset network button:** if `reset=<command>` is set in `[network]` (for example `reset=~/.local/bin/reset-network.sh`), the banner gets a "Reset network" button that runs it. It only runs when you click it; the button is disabled for 20 s while the connection comes back.
  - **While it shows:** left-clicking news items, feed headers, live channels and the "Updates available" header does nothing; right-clicking a news item still dismisses it (as does "Mark all read"). On the first successful probe the banner goes away and feeds, weather and live channels refresh immediately instead of waiting out their intervals.

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
| `notification.wav` | sound played on new items and when the timer ends (no sound if the file is absent) |

`config.conf` is INI-like:

```ini
[feeds]
# URL|display name|check interval in minutes   (name and interval optional)
https://example.com/feed.xml|My Blog|5

[mute]
# case-insensitive phrases, several per line separated by |; titles containing any are dropped
sponsored

[twitch]
# one channel name per line: channel|quality|schedule  (quality and schedule
# optional; quality is a streamlink format such as 720p60, default "best")
# interval=<minutes> sets how often to check for live channels (default 15,
# minimum 1).
interval=15
somechannel
somechannel2|720p60
weeklychannel||wed 18:00-23:00
dailychannel|720p60|tue-sun 13:00-; sat 10:00-12:00

[youtube]
# one channel identifier per line -- whatever goes after youtube.com/, so
# either @handle or channel/UCxxxxxxxxxxxxxxxxxxxxxx (case-sensitive,
# unlike Twitch names). Optionally |quality|schedule, same as [twitch].
# interval=<minutes> between live checks (default 15, minimum 5 -- each
# channel costs a full streamlink run).
interval=15
@somehandle
channel/UCxxxxxxxxxxxxxxxxxxxxxx|720p60

[weather]
# lat|lon for the weather bar (Open-Meteo, no key needed); the weather
# bar stays hidden until this is set
<latitude>|<longitude>
# interval=<minutes> between weather (and alert) refreshes (default 30, minimum 5)
interval=30
alerts=true

[network]
# Command behind the "Reset network" button on the Offline banner (no button if
# empty). It runs only when you click the button.
reset=~/.local/bin/reset-network.sh

[updates]
# interval=<hours> between package update scans (default 12, minimum 1)
interval=12

[actions]
# Label|command lines the timer's Schedule mode can run (the built-in default,
# used when this section is empty, is Suspend and Power off).
Suspend|loginctl suspend
Power off|loginctl poweroff

[schedule]
# Managed by the popup's Timer > Schedule; you can edit it by hand too.
# One schedule per line: on|off, HH:MM, days, action label from [actions]
# (days: daily, weekdays, weekends or e.g. mon,wed,fri).
on|00:30|daily|Suspend
off|07:00|weekdays|Lock screen
# Shared by all schedules: seconds of warning (0 = none), minutes the Snooze
# button postpones, and the seconds after which a missed run is skipped.
warn=60
snooze=30
grace=120

[launcher]
# Label|command per line for the Launch button. Commands run through the
# shell (so ~, quotes and && work), detached, from your home directory.
Files|thunar ~
Update system|xfce4-terminal -e "sudo xbps-install -Su"

[timer]
# max=<minutes> caps the slider (default 120); step=<seconds> sets the
# arrow-key/scroll increment (default 60). Both optional.
max=120
step=60
```

### Scheduled actions

Timer > Schedule runs one of your `[actions]` at a time of day, on the days you pick, with no cron job, `snooze` loop or other service involved (this replaces a script such as `snooze -H0 -M30 loginctl poweroff` in a `while` loop).

- **Several schedules:** the selector at the top of the slide lists them (`00:30 daily · Suspend`, with "(off)" on disabled ones); `+` adds one (off by default, so it can't surprise you) and `−` removes the selected one. A schedule is one line in `[schedule]`: `on|00:30|daily|Suspend`.
- **Controls** (for the selected schedule): the "On" checkbox, the time (hours and minutes), the action (from `[actions]`), and a Mo..Su button per day (at least one stays selected). Changes are saved to `config.conf` immediately, and edits made to the file by hand are picked up within a second.
- **Warning:** `warn=` seconds before a run (60 by default; `warn=0` for none) the notification sound plays, the popup opens, the tray icon flashes a red "!" and a red bar reads "Suspend in 42 s" with two buttons:
  - **Cancel** drops that run (the schedule's next run still happens).
  - **Snooze N min** postpones that run by `snooze=` minutes (30 by default). Pressing it again postpones further. The Snooze button in the slide does the same for the selected schedule before the warning starts.
  - If two schedules warn at once, the one that is due first is shown; each is handled separately.
- **Late runs are skipped:** the app only acts within `grace=` seconds (120 by default) of the scheduled time. If the machine was asleep or off at that moment, or you enabled or edited the schedule after the time had passed, that run is dropped instead of firing late. A run is never repeated after a wake-up.
- **Time:** the system's local clock, DST included. Every handled run is recorded in `state.json`, so restarting the app doesn't repeat it. Editing a schedule's time, days or action starts it afresh.
- **Actions:** any command works (`Lock screen|loginctl lock-session`, `Hibernate|systemctl hibernate`, ...); a schedule refers to it by label. A label that no longer exists is shown flagged in the slide and nothing runs.
- **From the first single-schedule version:** a `[schedule]` section with `enabled=`, `time=`, `days=` and `action=` lines is still read as one schedule, and is rewritten in the new format the first time you change anything in the slide.

### Channel schedules

A schedule limits live checks to the times a channel is expected to stream, so you aren't polling it around the clock. It is the third `|` field of a channel line (leave the quality empty to skip it: `channel||wed 18:00-23:00`).

- A schedule is one or more windows separated by `;`, each `[days] HH:MM[-HH:MM]` (`:` or `.` between hours and minutes).
- Days: `mon`...`sun` (or full names), ranges such as `tue-sun` (they may wrap, `fri-mon`), lists such as `mon,wed,fri`, `daily`, `weekdays`, `weekends`. Leave the days out for every day; leave the time out for the whole day.
- No end time means until midnight. An end earlier than the start runs past midnight into the next day (`fri 22:00-02:00`).
- Times are the system's local time (DST included). The app reads the system clock itself, so it needs no extra service or dependency.
- Inside a window the channel is checked at the normal `interval=`, and once right when the window opens. A channel that is live when its window ends keeps being checked until it goes offline. An unreadable schedule is ignored (the channel is then checked all the time).

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

- The popup is a borderless `Gtk.Window` that is destroyed and rebuilt on every manual open (an auto-open while it is already showing just refreshes the list). This avoids stuck-size bugs; do not make it persistent or call `resize()` on it.
- Package updates are system-wide rather than feed-based, because GitHub's atom feed is a sliding window and version bumps get missed.
- Threading: `lock` guards state, `_check_lock` RSS cycles, `_xbps_lock` xbps scans and installs, `_twitch_lock`/`_youtube_lock` their respective live-channel checks. All GTK mutations from threads go through `GLib.idle_add`.
- A failed xbps scan or Twitch request keeps the previous state; failed scans back off exponentially up to the normal update interval.
- The install timeout kills the whole process group (`sudo` and the `xbps-install` it forked).

## Known limitations

- `Gtk.StatusIcon` is deprecated upstream but still works on XFCE and most X11 panels; the app silences its deprecation warnings. If a future GTK drops it, the tray icon will need porting to an AppIndicator (which would replace the popup with a menu).
- Twitch's GQL API is unofficial and undocumented; it can break without notice.
- YouTube Live detection depends on streamlink's own YouTube plugin staying current with YouTube's changes; a channel's liveness check takes a few seconds (full streamlink extraction), unlike Twitch's near-instant batched check.
- Update detection and installs are Void-specific.

## Tests

Run `python3 -m unittest discover -s tests -v` (needs PyGObject and feedparser installed). CI runs the same on every push.

## License

MIT, see [LICENSE](LICENSE).
