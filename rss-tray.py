#!/usr/bin/env python3
"""Minimal tray RSS/Atom reader with Void package-update detection."""
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk, Pango
import cairo
import feedparser
import json
import os
import re
import shutil
import subprocess
import threading
import webbrowser
import hashlib
import calendar
import time as time_module

CONFIG_DIR = os.path.expanduser('~/.config/rss-tray')
FEEDS_FILE = os.path.join(CONFIG_DIR, 'feeds.conf')
STATE_FILE = os.path.join(CONFIG_DIR, 'state.json')
CHECK_INTERVAL = 600  # 10 minutes
MAX_LIST_ITEMS = 40
MAX_TITLE_LEN = 60
MAX_ITEM_AGE_SECONDS = 24 * 3600  # ignore entries older than this on first sight
PRIVILEGE_CMD = ['sudo']  # change to ['doas'] if that's what you use
WINDOW_WIDTH = 456  # 380 * 1.2


def ensure_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(FEEDS_FILE):
        with open(FEEDS_FILE, 'w') as f:
            f.write(
                "# Format: URL|custom display name (optional)|pkgfeed flag (optional)\n"
                "# Add 'pkgfeed' in the third field to enable Void package-update\n"
                "# detection for that feed's entries.\n"
                "# https://example.com/feed.xml\n"
                "# https://example.com/feed.xml|My Blog\n"
                "# https://github.com/void-linux/void-packages/commits/master.atom|void-package|pkgfeed\n"
            )


def load_feeds():
    """Returns list of (url, is_pkgfeed, custom_name)."""
    feeds = []
    if os.path.exists(FEEDS_FILE):
        with open(FEEDS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = [p.strip() for p in line.split('|')]
                url = parts[0]
                custom_name = parts[1] if len(parts) > 1 and parts[1] else None
                is_pkgfeed = len(parts) > 2 and parts[2].lower() == 'pkgfeed'
                feeds.append((url, is_pkgfeed, custom_name))
    return feeds


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": [], "unread": []}


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


def extract_candidate_pkgnames(title):
    if ':' not in title:
        return []
    prefix = title.split(':', 1)[0].strip()
    if prefix.lower() in ('new package', 'removed package', 'srcpkgs'):
        return []
    return [p.strip() for p in prefix.split(',') if p.strip() and ' ' not in p.strip()]


def find_installed_match(title):
    for name in extract_candidate_pkgnames(title):
        if not re.match(r'^[A-Za-z0-9._+-]+$', name):
            continue
        try:
            result = subprocess.run(
                ['xbps-query', name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
            )
            if result.returncode == 0:
                return name
        except Exception:
            continue
    return None


def update_package(pkgname):
    cmd = PRIVILEGE_CMD + ['xbps-install', '-Su', pkgname]
    if shutil.which('xfce4-terminal'):
        subprocess.Popen(['xfce4-terminal', '--hold', '-x'] + cmd)
    elif shutil.which('x-terminal-emulator'):
        subprocess.Popen(['x-terminal-emulator', '-e'] + cmd)
    else:
        print(f"No terminal emulator found — run manually: {' '.join(cmd)}")


def edit_feeds_file():
    try:
        subprocess.Popen(['xdg-open', FEEDS_FILE])
        return
    except Exception:
        pass
    editor = os.environ.get('EDITOR', 'vi')
    if shutil.which('xfce4-terminal'):
        subprocess.Popen(['xfce4-terminal', '-e', f'{editor} "{FEEDS_FILE}"'])
    else:
        try:
            subprocess.Popen([editor, FEEDS_FILE])
        except Exception:
            print(f"Couldn't open an editor — edit manually: {FEEDS_FILE}")


class RssTray:
    def __init__(self):
        ensure_config()
        self.state = load_state()
        self.lock = threading.Lock()
        self.popup = None
        self.listbox = None

        self._apply_compact_css()

        self.status_icon = Gtk.StatusIcon()
        self.status_icon.connect('activate', self.toggle_popup)
        self.status_icon.connect('popup-menu', self.toggle_popup)
        self.update_icon()

        GLib.timeout_add_seconds(1, self.initial_check)
        GLib.timeout_add_seconds(CHECK_INTERVAL, self.periodic_check)
        GLib.timeout_add(800, self.maybe_auto_show_startup)

    def maybe_auto_show_startup(self):
        if self.unread_count() > 0:
            self.show_popup()
        return False

    def initial_check(self):
        self.start_check_thread()
        return False

    def periodic_check(self):
        self.start_check_thread()
        return True

    def start_check_thread(self):
        threading.Thread(target=self.check_feeds, daemon=True).start()

    def check_feeds(self):
        feeds = load_feeds()
        new_items = []
        with self.lock:
            seen = set(self.state.get('seen', []))
        for url, is_pkgfeed, _custom_name in feeds:
            try:
                parsed = feedparser.parse(url)
            except Exception:
                continue
            for entry in parsed.entries:
                eid = entry_id(entry)
                if eid in seen:
                    continue
                seen.add(eid)
                age = entry_age_seconds(entry)
                if age is not None and age > MAX_ITEM_AGE_SECONDS:
                    continue  # too old — mark as seen, don't surface as unread
                title = entry.get('title', '(untitled)')
                pkg_match = find_installed_match(title) if is_pkgfeed else None
                new_items.append({
                    'id': eid,
                    'title': title,
                    'link': entry.get('link', ''),
                    'feed_url': url,
                    'pkg_match': pkg_match,
                })
        with self.lock:
            self.state['seen'] = list(seen)
            if new_items:
                self.state['unread'] = new_items + self.state.get('unread', [])
            save_state(self.state)
        if new_items:
            GLib.idle_add(self.on_new_items)

    def on_new_items(self):
        self.update_icon()
        self.show_popup()  # auto-open whenever new unread items arrive
        return False

    def unread_count(self):
        with self.lock:
            return len(self.state.get('unread', []))

    def has_pkg_update(self):
        with self.lock:
            return any(e.get('pkg_match') for e in self.state.get('unread', []))

    def update_icon(self):
        count = self.unread_count()
        self.status_icon.set_from_pixbuf(self.render_icon(count))
        if self.has_pkg_update():
            tooltip = f"{count} unread — package update available"
        elif count:
            tooltip = f"{count} unread"
        else:
            tooltip = "No unread items"
        self.status_icon.set_tooltip_text(tooltip)
        return False

    def render_icon(self, count):
        size = 24
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        ctx = cairo.Context(surface)
        if self.has_pkg_update():
            ctx.set_source_rgba(0.82, 0.18, 0.18, 1)   # red: update available
        elif count > 0:
            ctx.set_source_rgba(0.92, 0.55, 0.10, 1)   # orange: unread news
        else:
            ctx.set_source_rgba(0.20, 0.65, 0.30, 1)   # green: nothing unread
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

    def feed_name_for(self, url):
        for feed_url, _is_pkgfeed, custom_name in load_feeds():
            if feed_url == url:
                return custom_name or feed_url
        return url

    # --- popup window ---

    def _apply_compact_css(self):
        css = b"""
        list row { padding: 1px 3px; min-height: 0px; }
        button { padding: 1px; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

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

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(420)
        scroller.set_propagate_natural_height(True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect('row-activated', self.on_row_activated)
        scroller.add(self.listbox)
        outer.pack_start(scroller, True, True, 0)

        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 2)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        footer.set_margin_start(6)
        footer.set_margin_end(6)
        footer.set_margin_top(4)
        footer.set_margin_bottom(4)
        edit_btn = Gtk.Button(label='Edit feeds')
        edit_btn.connect('clicked', lambda *_a: edit_feeds_file())
        footer.pack_start(edit_btn, True, True, 0)
        refresh_btn = Gtk.Button(label='Refresh')
        refresh_btn.connect('clicked', lambda *_a: self.start_check_thread())
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

    def show_popup(self):
        if self.popup is None:
            self.build_popup_window()
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
            # Fallback: top-right corner of the primary monitor
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
        if not unread:
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
            shown = unread[:MAX_LIST_ITEMS]
            groups = {}
            order = []
            for entry in shown:
                feed_name = self.feed_name_for(entry.get('feed_url', ''))
                if feed_name not in groups:
                    groups[feed_name] = []
                    order.append(feed_name)
                groups[feed_name].append(entry)

            for feed_name in order:
                self.listbox.add(self.build_header_row(feed_name))
                for entry in groups[feed_name]:
                    self.listbox.add(self.build_row(entry))

            if len(unread) > MAX_LIST_ITEMS:
                row = Gtk.ListBoxRow()
                row.set_selectable(False)
                row.set_activatable(False)
                lbl = Gtk.Label(label=f"... and {len(unread) - MAX_LIST_ITEMS} more")
                row.add(lbl)
                self.listbox.add(row)
        self.listbox.show_all()

    def build_header_row(self, feed_name):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4)
        box.set_margin_end(4)
        box.set_margin_top(4)
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

    def build_row(self, entry):
        row = Gtk.ListBoxRow()
        row.entry_id = entry['id']
        row.link = entry['link']
        row.pkg_match = entry.get('pkg_match')

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        box.set_margin_start(3)
        box.set_margin_end(3)
        box.set_margin_top(0)
        box.set_margin_bottom(0)

        if row.pkg_match:
            img = Gtk.Image.new_from_icon_name('software-update-available-symbolic', Gtk.IconSize.SMALL_TOOLBAR)
            box.pack_start(img, False, False, 0)

        mark_btn = Gtk.Button()
        mark_btn.set_relief(Gtk.ReliefStyle.NONE)
        mark_icon = Gtk.Image.new_from_icon_name('mail-mark-read-symbolic', Gtk.IconSize.MENU)
        mark_btn.add(mark_icon)
        mark_btn.set_tooltip_text('Mark as read')
        mark_btn.connect('clicked', self.on_mark_read_clicked, entry['id'])
        box.pack_start(mark_btn, False, False, 0)

        full_title = entry['title']
        truncated = len(full_title) > MAX_TITLE_LEN
        title = full_title[:MAX_TITLE_LEN - 1] + '…' if truncated else full_title
        text = GLib.markup_escape_text(title)
        label = Gtk.Label()
        label.set_markup(f'<span foreground="#000000"><b>{text}</b></span>')
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)

        tooltip_parts = []
        if row.pkg_match:
            tooltip_parts.append(f"Matches installed package '{row.pkg_match}' — click to update")
        if truncated:
            tooltip_parts.append(full_title)
        if tooltip_parts:
            tooltip_text = "\n".join(tooltip_parts)
            row.set_tooltip_text(tooltip_text)
            label.set_tooltip_text(tooltip_text)

        box.pack_start(label, True, True, 0)

        row.add(box)
        return row

    def _remove_unread(self, item_id):
        with self.lock:
            self.state['unread'] = [e for e in self.state.get('unread', []) if e['id'] != item_id]
            save_state(self.state)

    def on_row_activated(self, _listbox, row):
        if not hasattr(row, 'entry_id'):
            return
        item_id, link, pkg_match = row.entry_id, row.link, row.pkg_match
        self._remove_unread(item_id)
        if pkg_match:
            update_package(pkg_match)
        elif link:
            webbrowser.open(link)
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
