"""GTK4 user interface: Home (library) and Discover (store), plus sheets.

The window is an ordinary fullscreen Gtk.ApplicationWindow. Navigation is a
single explicit model shared by controller, keyboard and pointer: the
"selected" item is always the GTK focus widget, hovering selects, and
arrow keys / D-pad / left stick move geometrically between rows.
"""

from __future__ import annotations

from string import Template

from gi.repository import Gdk, GLib, Gtk, Pango

from . import APP_ID
from .core import (ACTIVATE, AUTO, BACK, DESKTOP, DOWN, GAME, LEFT, MENU, NEXT_TAB, PREV_TAB, RIGHT, UP)

CATEGORY_ICONS = {"native": "controller", "streaming": "cloud", "retro": "retro", "stores": "store"}
WIDE_WIDTH = 1600
WIDE_HEIGHT = 950
COMPACT_HEIGHT = 800

CSS = Template("""
@define-color gh_bg $background;
@define-color gh_surface $surface;
@define-color gh_fg $foreground;
@define-color gh_muted $muted;
@define-color gh_accent $accent;
@define-color gh_success $success;
@define-color gh_warning $warning;
@define-color gh_danger $danger;

.gh-card, .gh-tile, .gh-pill, .gh-option, .gh-lang, .gh-tab, .gh-icon-button, .gh-toast-button { background-image: none; }

window.gh-window {
  background-color: @gh_bg;
  background-image:
    radial-gradient(ellipse at 8% -20%, alpha(@gh_accent, 0.22), transparent 55%),
    radial-gradient(ellipse at 110% 120%, alpha(@gh_accent, 0.10), transparent 55%),
    linear-gradient(180deg, mix(@gh_bg, @gh_surface, 0.55), @gh_bg);
  color: @gh_fg;
}
.gh-root { padding: 24px 40px 20px 40px; }
.gh-wide .gh-root { padding: 40px 72px 32px 72px; }
.gh-compact .gh-root { padding: 16px 32px 14px 32px; }
.gh-compact .gh-hero { min-height: 0; padding: 14px 22px; }
.gh-compact .gh-hero-badge { min-width: 68px; min-height: 68px; border-radius: 34px; }
.gh-compact .gh-hero-title { font-size: 24px; }
.gh-compact .gh-card { min-height: 188px; padding: 18px; }
.gh-compact .gh-badge { min-width: 56px; min-height: 56px; border-radius: 16px; }
.gh-compact .gh-tile { min-height: 96px; }
.gh-compact .gh-sheet { padding: 18px 24px; }
.gh-compact .gh-option { min-height: 64px; padding: 10px 14px; }

/* Header */
.gh-brand-title { font-size: 20px; font-weight: 800; letter-spacing: 0.2px; }
.gh-brand-sub { font-size: 12px; font-weight: 600; color: @gh_muted; letter-spacing: 1.5px; }
.gh-tab {
  min-height: 52px; min-width: 64px; padding: 0 22px; border-radius: 26px;
  background: transparent; color: @gh_muted; font-size: 16px; font-weight: 700;
  border: 2px solid transparent; box-shadow: none;
}
.gh-tab:hover { color: @gh_fg; }
.gh-tab.current { background: alpha(@gh_fg, 0.10); color: @gh_fg; }
.gh-icon-button {
  min-height: 52px; min-width: 52px; padding: 0; border-radius: 26px;
  background: alpha(@gh_fg, 0.06); color: @gh_fg; border: 2px solid transparent; box-shadow: none;
}
.gh-chip {
  min-height: 36px; padding: 0 14px; border-radius: 18px;
  background: alpha(@gh_fg, 0.07); font-size: 13px; font-weight: 600; color: @gh_fg;
}
.gh-chip.warn { background: alpha(@gh_warning, 0.16); color: @gh_warning; }
.gh-dot { min-width: 8px; min-height: 8px; border-radius: 4px; background: @gh_muted; }
.gh-dot.ok { background: @gh_success; }
.gh-dot.accent { background: @gh_accent; }
.gh-dot.warn { background: @gh_warning; }
.gh-dot.bad { background: @gh_danger; }

/* Focus ring shared by every navigable control */
.gh-nav { transition: box-shadow 120ms ease-out, background-color 120ms ease-out, border-color 120ms ease-out; outline: none; }
.gh-nav.selected {
  border-color: @gh_accent;
  box-shadow: 0 0 0 4px alpha(@gh_accent, 0.28), 0 14px 36px alpha(@gh_accent, 0.22);
}
.gh-tab.selected, .gh-icon-button.selected { background: alpha(@gh_accent, 0.18); color: @gh_fg; }

/* Hero */
.gh-hero {
  min-height: 150px; padding: 22px 28px; border-radius: 28px;
  border: 2px solid alpha(@gh_fg, 0.06);
  background-image: linear-gradient(105deg, alpha(@gh_accent, 0.34), alpha(@gh_surface, 0.92) 62%);
  color: @gh_fg; box-shadow: 0 10px 30px alpha(black, 0.30);
}
.gh-hero.empty {
  background-image: none; background-color: alpha(@gh_surface, 0.45);
  border: 2px dashed alpha(@gh_fg, 0.14); box-shadow: none;
}
.gh-hero-badge {
  min-width: 96px; min-height: 96px; border-radius: 48px;
  background-image: linear-gradient(135deg, @gh_accent, mix(@gh_accent, @gh_bg, 0.45));
  color: @gh_bg;
}
.gh-hero.empty .gh-hero-badge { background-image: none; background-color: alpha(@gh_fg, 0.08); color: @gh_muted; }
.gh-eyebrow { font-size: 12px; font-weight: 800; letter-spacing: 2px; color: @gh_accent; }
.gh-hero.empty .gh-eyebrow { color: @gh_muted; }
.gh-hero-title { font-size: 30px; font-weight: 800; }
.gh-hero-body { font-size: 15px; color: alpha(@gh_fg, 0.78); }
.gh-hero-action {
  min-height: 52px; padding: 0 22px; border-radius: 26px;
  background: @gh_fg; color: @gh_bg; font-weight: 800; font-size: 16px;
}
.gh-wide .gh-hero { min-height: 220px; }
.gh-wide .gh-hero-title { font-size: 40px; }

/* Section headings */
.gh-section { font-size: 15px; font-weight: 800; letter-spacing: 1.5px; color: @gh_muted; }
.gh-page-title { font-size: 30px; font-weight: 800; }
.gh-page-sub { font-size: 15px; color: @gh_muted; }

/* Library cards */
.gh-card {
  min-width: 260px; min-height: 230px; padding: 22px; border-radius: 26px;
  background-color: alpha(@gh_surface, 0.88); color: @gh_fg;
  border: 2px solid alpha(@gh_fg, 0.06); box-shadow: 0 8px 24px alpha(black, 0.30);
}
.gh-card:hover, .gh-tile:hover { background-color: mix(@gh_surface, @gh_fg, 0.05); }
.gh-card.selected, .gh-tile.selected { background-color: mix(@gh_surface, @gh_accent, 0.10); }
.gh-card.add { background-color: transparent; border: 2px dashed alpha(@gh_fg, 0.16); box-shadow: none; }
.gh-wide .gh-card { min-width: 340px; min-height: 300px; padding: 28px; }
.gh-card-title { font-size: 21px; font-weight: 800; }
.gh-card-body { font-size: 14px; color: @gh_muted; }
.gh-card-status { font-size: 13px; font-weight: 700; color: alpha(@gh_fg, 0.86); }

.gh-badge {
  min-width: 68px; min-height: 68px; border-radius: 20px;
  background-color: alpha(@gh_accent, 0.16); color: @gh_accent;
}
.gh-badge.small { min-width: 56px; min-height: 56px; border-radius: 16px; }
.gh-badge.large { min-width: 92px; min-height: 92px; border-radius: 26px; }
.gh-badge.streaming { background-color: alpha(@gh_success, 0.15); color: @gh_success; }
.gh-badge.retro { background-color: alpha(@gh_warning, 0.15); color: @gh_warning; }
.gh-badge.stores { background-color: alpha(mix(@gh_accent, @gh_danger, 0.5), 0.17); color: mix(@gh_accent, @gh_danger, 0.5); }
.gh-badge.muted { background-color: alpha(@gh_fg, 0.07); color: @gh_muted; }

/* Discover tiles */
.gh-tile {
  min-height: 112px; padding: 16px 18px; border-radius: 22px;
  background-color: alpha(@gh_surface, 0.88); color: @gh_fg;
  border: 2px solid alpha(@gh_fg, 0.06);
}
.gh-tile-title { font-size: 17px; font-weight: 800; }
.gh-tile-meta { font-size: 13px; color: @gh_muted; }
.gh-pill-status {
  min-height: 26px; padding: 0 10px; border-radius: 13px; font-size: 12px; font-weight: 800;
  background: alpha(@gh_fg, 0.08); color: @gh_muted;
}
.gh-pill-status.ok { background: alpha(@gh_success, 0.16); color: @gh_success; }
.gh-pill-status.accent { background: alpha(@gh_accent, 0.18); color: @gh_accent; }
.gh-pill-status.warn { background: alpha(@gh_warning, 0.16); color: @gh_warning; }

/* Footer */
.gh-pill {
  min-height: 64px; padding: 0 24px; border-radius: 32px;
  background-color: alpha(@gh_fg, 0.07); color: @gh_fg; font-size: 16px; font-weight: 700;
  border: 2px solid transparent; box-shadow: none;
}
.gh-pill:hover { background-color: alpha(@gh_fg, 0.12); }
.gh-pill.primary { background-color: @gh_accent; color: @gh_bg; }
.gh-pill.primary.selected { background-color: mix(@gh_accent, @gh_fg, 0.15); border-color: @gh_fg; }
.gh-legend { font-size: 14px; font-weight: 600; color: @gh_muted; }
.gh-key {
  min-width: 28px; min-height: 28px; padding: 0 8px; border-radius: 14px;
  font-size: 12px; font-weight: 900; background: alpha(@gh_fg, 0.14); color: @gh_fg;
}
.gh-key.a { background: @gh_success; color: @gh_bg; padding: 0; }
.gh-key.b { background: @gh_danger; color: @gh_bg; padding: 0; }

/* Toast */
.gh-toast {
  padding: 14px 20px; border-radius: 20px; margin-bottom: 104px;
  background-color: mix(@gh_surface, @gh_fg, 0.06); color: @gh_fg;
  border: 2px solid alpha(@gh_fg, 0.10); box-shadow: 0 16px 40px alpha(black, 0.45);
  font-size: 15px; font-weight: 600;
}
.gh-toast.error { border-color: alpha(@gh_danger, 0.75); }
.gh-toast.progress { border-color: alpha(@gh_accent, 0.6); }
.gh-toast-button { min-height: 44px; padding: 0 16px; border-radius: 22px; background: alpha(@gh_fg, 0.12); color: @gh_fg; font-weight: 800; }

/* Sheets */
.gh-scrim { background-color: alpha(black, 0.62); }
.gh-sheet {
  padding: 24px 30px; border-radius: 30px; background-color: @gh_surface; color: @gh_fg;
  border: 2px solid alpha(@gh_fg, 0.08); box-shadow: 0 30px 80px alpha(black, 0.55);
}
.gh-sheet-title { font-size: 26px; font-weight: 800; }
.gh-sheet-label { font-size: 13px; font-weight: 800; letter-spacing: 1.5px; color: @gh_muted; }
.gh-sheet-body { font-size: 15px; color: alpha(@gh_fg, 0.82); }
.gh-option {
  min-height: 72px; min-width: 64px; padding: 12px 16px; border-radius: 20px;
  background-color: alpha(@gh_fg, 0.05); color: @gh_fg; border: 2px solid alpha(@gh_fg, 0.06); box-shadow: none;
}
.gh-option.checked { background-color: alpha(@gh_accent, 0.14); border-color: alpha(@gh_accent, 0.55); }
.gh-option-title { font-size: 16px; font-weight: 800; }
.gh-option-body { font-size: 13px; color: @gh_muted; }
.gh-option-check { color: @gh_accent; }
.gh-lang {
  min-height: 64px; min-width: 64px; padding: 0 16px; border-radius: 18px;
  background-color: alpha(@gh_fg, 0.05); color: @gh_fg; font-size: 15px; font-weight: 700;
  border: 2px solid alpha(@gh_fg, 0.06); box-shadow: none;
}
.gh-lang.checked { background-color: alpha(@gh_accent, 0.16); border-color: alpha(@gh_accent, 0.55); }
.gh-toggle-state { min-height: 32px; min-width: 56px; padding: 0 12px; border-radius: 16px; background: alpha(@gh_fg, 0.12); font-weight: 800; }
.gh-toggle-state.on { background: @gh_accent; color: @gh_bg; }
.gh-disabled-note { font-size: 13px; color: @gh_warning; }
""")


def icon_name(theme: Gtk.IconTheme, name: str, category: str | None = None) -> str:
    candidate = f"gh-{name}-symbolic"
    if theme.has_icon(candidate):
        return candidate
    return f"gh-{CATEGORY_ICONS.get(category or '', 'controller')}-symbolic"


def label(text="", css=None, xalign=0.0, wrap=False, lines=0, ellipsize=False, max_chars=0, **props):
    widget = Gtk.Label(label=text, xalign=xalign, **props)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    if wrap:
        widget.set_wrap(True)
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
    if lines:
        widget.set_lines(lines)
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    elif ellipsize:
        widget.set_ellipsize(Pango.EllipsizeMode.END)
    if max_chars:
        widget.set_max_width_chars(max_chars)
    return widget


def box(orientation=Gtk.Orientation.VERTICAL, spacing=0, css=None, **props):
    widget = Gtk.Box(orientation=orientation, spacing=spacing, **props)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    return widget


def hbox(spacing=0, css=None, **props):
    return box(Gtk.Orientation.HORIZONTAL, spacing, css, **props)


def badge(theme, name, category=None, size=40, css="gh-badge"):
    holder = box(css=css, halign=Gtk.Align.START, valign=Gtk.Align.CENTER, hexpand=False, vexpand=False)
    if category in ("streaming", "retro", "stores"):
        holder.add_css_class(category)
    image = Gtk.Image.new_from_icon_name(icon_name(theme, name, category))
    image.set_pixel_size(size)
    image.set_hexpand(True)
    image.set_vexpand(True)
    holder.append(image)
    return holder


class HomeWindow(Gtk.ApplicationWindow):
    def __init__(self, app, runtime):
        super().__init__(application=app, title=runtime.t("app.name"))
        self.runtime = runtime
        self.add_css_class("gh-window")
        self.set_default_size(1280, 720)
        self.set_icon_name(APP_ID)
        self.page = "home"
        self.sheet = None  # None | "settings" | ("app", id)
        self._selected = None
        self._layout = None
        self._toast_timer = None
        self._library_ids: list[str] = []
        self._library_cards: dict[str, Gtk.Button] = {}
        self._tiles: dict[str, Gtk.Button] = {}
        self.connect("notify::focus-widget", self._on_focus_changed)
        self.connect("close-request", self._on_close_request)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.build()

    # ---- construction --------------------------------------------------
    @property
    def t(self):
        return self.runtime.t

    @property
    def icons(self):
        return Gtk.IconTheme.get_for_display(self.get_display())

    def build(self):
        self._library_ids = []
        self._library_cards = {}
        self._tiles = {}
        self._selected = None
        self.set_title(self.t("app.name"))
        overlay = Gtk.Overlay()
        self.overlay = overlay
        self.root = box(spacing=14, css="gh-root")
        overlay.set_child(self.root)

        self.root.append(self._build_header())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=140, vexpand=True)
        self.stack.add_named(self._build_home(), "home")
        self.stack.add_named(self._build_discover(), "discover")
        self.root.append(self.stack)
        self.root.append(self._build_footer())

        self.toast = self._build_toast()
        overlay.add_overlay(self.toast)
        self.scrim = box(css="gh-scrim", visible=False)
        overlay.add_overlay(self.scrim)
        self.set_child(overlay)
        self.stack.set_visible_child_name(self.page)
        self.refresh()

    def _build_header(self):
        header = hbox(16, valign=Gtk.Align.CENTER)
        brand = hbox(12)
        logo = Gtk.Image.new_from_icon_name(APP_ID)
        logo.set_pixel_size(44)
        brand.append(logo)
        titles = box(valign=Gtk.Align.CENTER)
        titles.append(label(self.t("app.name"), "gh-brand-title"))
        brand.append(titles)
        header.append(brand)

        tabs = hbox(6, margin_start=18)
        self.tab_home = self._nav_button(self.t("nav.home"), "gh-tab", "tab:home", lambda *_: self.show_page("home"))
        self.tab_discover = self._nav_button(self.t("nav.discover"), "gh-tab", "tab:discover", lambda *_: self.show_page("discover"))
        tabs.append(self.tab_home)
        tabs.append(self.tab_discover)
        header.append(tabs)
        header.append(box(hexpand=True))

        self.controller_chip = hbox(8, "gh-chip", valign=Gtk.Align.CENTER)
        self.controller_dot = box(css="gh-dot", valign=Gtk.Align.CENTER)
        self.controller_label = label("", ellipsize=True, max_chars=26)
        self.controller_chip.append(self.controller_dot)
        self.controller_chip.append(self.controller_label)
        header.append(self.controller_chip)

        self.mode_chip = hbox(8, "gh-chip", valign=Gtk.Align.CENTER)
        self.mode_dot = box(css="gh-dot", valign=Gtk.Align.CENTER)
        self.mode_label = label("")
        self.mode_chip.append(self.mode_dot)
        self.mode_chip.append(self.mode_label)
        header.append(self.mode_chip)

        self.settings_button = Gtk.Button(css_classes=["gh-icon-button", "gh-nav"], valign=Gtk.Align.CENTER)
        self.settings_button.set_name("settings")
        gear = Gtk.Image.new_from_icon_name(icon_name(self.icons, "settings"))
        gear.set_pixel_size(24)
        self.settings_button.set_child(gear)
        self.settings_button.set_tooltip_text(self.t("nav.settings"))
        self.settings_button.connect("clicked", lambda *_: self.open_settings())
        self._hover_selects(self.settings_button)
        header.append(self.settings_button)
        return header

    def _nav_button(self, text, css, name, callback):
        button = Gtk.Button(label=text, css_classes=css.split() + ["gh-nav"])
        button.set_name(name)
        button.connect("clicked", callback)
        self._hover_selects(button)
        return button

    def _hover_selects(self, widget):
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: widget.get_sensitive() and widget.grab_focus())
        widget.add_controller(motion)

    def _build_home(self):
        page = box(spacing=18)
        self.hero = Gtk.Button(css_classes=["gh-hero", "gh-nav"])
        self.hero.set_name("hero")
        self.hero.connect("clicked", lambda *_: self.runtime.resume())
        self._hover_selects(self.hero)
        row = hbox(24)
        self.hero_badge = box(css="gh-hero-badge", valign=Gtk.Align.CENTER, halign=Gtk.Align.START, hexpand=False, vexpand=False)
        self.hero_icon = Gtk.Image.new_from_icon_name(icon_name(self.icons, "play"))
        self.hero_icon.set_pixel_size(44)
        self.hero_icon.set_hexpand(True)
        self.hero_icon.set_vexpand(True)
        self.hero_badge.append(self.hero_icon)
        row.append(self.hero_badge)
        texts = box(spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        self.hero_eyebrow = label("", "gh-eyebrow")
        self.hero_title = label("", "gh-hero-title", ellipsize=True)
        self.hero_body = label("", "gh-hero-body", wrap=True, lines=2)
        texts.append(self.hero_eyebrow)
        texts.append(self.hero_title)
        texts.append(self.hero_body)
        row.append(texts)
        self.hero_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, width_request=36, height_request=36)
        row.append(self.hero_spinner)
        self.hero_action = hbox(10, "gh-hero-action", valign=Gtk.Align.CENTER)
        key = label("A", "gh-key a", xalign=0.5)
        key.set_valign(Gtk.Align.CENTER)
        self.hero_action.append(key)
        self.hero_action.append(label(self.t("hero.resume")))
        row.append(self.hero_action)
        self.hero.set_child(row)
        page.append(self.hero)

        page.append(label(self.t("section.library").upper(), "gh-section"))
        self.library_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vscrollbar_policy=Gtk.PolicyType.NEVER)
        self.library_row = hbox(18, margin_top=6, margin_bottom=14, margin_start=6, margin_end=6)
        self.library_scroller.set_child(self.library_row)
        page.append(self.library_scroller)
        # The page scrolls vertically (focus stays in view) so the footer is
        # never pushed off screen on short displays.
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(page)
        return scroller

    def _build_discover(self):
        page = box(spacing=6)
        page.append(label(self.t("discover.title"), "gh-page-title"))
        page.append(label(self.t("discover.subtitle"), "gh-page-sub", wrap=True))
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, margin_top=12)
        self.discover_grid = Gtk.Grid(column_spacing=16, row_spacing=16, column_homogeneous=True, margin_top=6, margin_bottom=10, margin_start=6, margin_end=6)
        apps = self.runtime.catalog.apps
        if not apps:
            self.discover_grid.attach(label(self.t("discover.empty"), "gh-page-sub"), 0, 0, 1, 1)
        for index, app in enumerate(apps):
            tile = self._build_tile(app)
            self._tiles[app.id] = tile
            self.discover_grid.attach(tile, index % 4, index // 4, 1, 1)
        scroller.set_child(self.discover_grid)
        page.append(scroller)
        return page

    def _build_tile(self, app):
        tile = Gtk.Button(css_classes=["gh-tile", "gh-nav"])
        tile.set_name(f"tile:{app.id}")
        tile.connect("clicked", lambda *_: self.open_app(app.id))
        self._hover_selects(tile)
        row = hbox(14)
        row.append(badge(self.icons, app.icon, app.category, 30, "gh-badge small"))
        texts = box(spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        texts.append(label(app.name, "gh-tile-title", wrap=True, lines=2))
        texts.append(label(self.t(f"category.{app.category}"), "gh-tile-meta", ellipsize=True))
        status = label("", "gh-pill-status", xalign=0.5)
        status.set_halign(Gtk.Align.START)
        status.set_margin_top(4)
        texts.append(status)
        row.append(texts)
        tile.set_child(row)
        tile.status_label = status
        return tile

    def _build_library_card(self, app):
        card = Gtk.Button(css_classes=["gh-card", "gh-nav"])
        card.set_name(f"app:{app.id}")
        card.connect("clicked", lambda *_: self.runtime.activate_app(app.id))
        self._hover_selects(card)
        content = box(spacing=8)
        content.append(badge(self.icons, app.icon, app.category, 38))
        content.append(box(vexpand=True, height_request=10))
        content.append(label(app.name, "gh-card-title", ellipsize=True))
        content.append(label(self.t(app.description_key), "gh-card-body", wrap=True, lines=2, max_chars=24))
        status_row = hbox(8, margin_top=6)
        dot = box(css="gh-dot", valign=Gtk.Align.CENTER)
        status = label("", "gh-card-status", ellipsize=True, hexpand=True)
        spinner = Gtk.Spinner(visible=False)
        status_row.append(dot)
        status_row.append(status)
        status_row.append(spinner)
        content.append(status_row)
        card.set_child(content)
        card.status_dot, card.status_label, card.spinner = dot, status, spinner
        return card

    def _build_add_card(self):
        card = Gtk.Button(css_classes=["gh-card", "gh-nav", "add"])
        card.set_name("app:+discover")
        card.connect("clicked", lambda *_: self.show_page("discover"))
        self._hover_selects(card)
        content = box(spacing=8)
        content.append(badge(self.icons, "add", None, 38, "gh-badge muted"))
        content.append(box(vexpand=True, height_request=10))
        content.append(label(self.t("library.addMore"), "gh-card-title", ellipsize=True))
        content.append(label(self.t("library.addMoreBody"), "gh-card-body", wrap=True, lines=2, max_chars=24))
        content.append(box(height_request=26))
        card.set_child(content)
        return card

    def _build_footer(self):
        footer = hbox(14, valign=Gtk.Align.CENTER)
        self.desktop_button = Gtk.Button(css_classes=["gh-pill", "gh-nav"])
        self.desktop_button.set_name("desktop")
        content = hbox(10)
        icon = Gtk.Image.new_from_icon_name(icon_name(self.icons, "desktop"))
        icon.set_pixel_size(22)
        content.append(icon)
        content.append(label(self.t("button.desktop")))
        self.desktop_button.set_child(content)
        self.desktop_button.connect("clicked", lambda *_: self.runtime.desktop())
        self._hover_selects(self.desktop_button)
        footer.append(self.desktop_button)

        self.controls_button = Gtk.Button(css_classes=["gh-pill", "gh-nav"])
        self.controls_button.set_name("controls")
        content = hbox(10)
        icon = Gtk.Image.new_from_icon_name(icon_name(self.icons, "controller"))
        icon.set_pixel_size(22)
        content.append(icon)
        self.controls_label = label("")
        content.append(self.controls_label)
        self.controls_button.set_child(content)
        self.controls_button.connect("clicked", lambda *_: self.open_settings(focus="mode"))
        self._hover_selects(self.controls_button)
        footer.append(self.controls_button)
        footer.append(box(hexpand=True))

        legend = hbox(10, "gh-legend", valign=Gtk.Align.CENTER)
        legend.set_tooltip_text(self.t("footer.guide"))

        def key(text, css="gh-key"):
            widget = label(text, css, xalign=0.5)
            widget.set_valign(Gtk.Align.CENTER)
            return widget

        def sep():
            return label("·", "gh-legend")

        def keyed(text, css):
            letter, _, rest = text.partition(" ")
            if len(letter) == 1 and rest:
                legend.append(key(letter, css))
                text = rest
            widget = label(text)
            legend.append(widget)
            return widget

        keyed(self.t("footer.select"), "gh-key a")
        legend.append(sep())
        self.back_legend = keyed(self.t("footer.desktop"), "gh-key b")
        legend.append(sep())
        legend.append(label(self.t("footer.home")))
        footer.append(legend)
        return footer

    def _build_toast(self):
        revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP, transition_duration=160,
                                halign=Gtk.Align.CENTER, valign=Gtk.Align.END, can_target=True)
        content = hbox(14, "gh-toast")
        self.toast_icon = Gtk.Image(pixel_size=22)
        self.toast_label = label("", wrap=True, max_chars=70)
        self.toast_button = Gtk.Button(css_classes=["gh-toast-button"], visible=False)
        self.toast_button.connect("clicked", self._on_toast_button)
        content.append(self.toast_icon)
        content.append(self.toast_label)
        content.append(self.toast_button)
        revealer.set_child(content)
        revealer.content = content
        return revealer

    # ---- dynamic state -------------------------------------------------
    def refresh(self):
        """Re-render every state-derived part. Called only on state changes."""
        rt = self.runtime
        t = self.t
        self.tab_home.remove_css_class("current")
        self.tab_discover.remove_css_class("current")
        (self.tab_home if self.page == "home" else self.tab_discover).add_css_class("current")

        controller = rt.controller_summary()
        self.controller_label.set_text(controller["label"])
        self._set_state_class(self.controller_dot, controller["state"])
        self.controller_chip.set_tooltip_text(controller["tooltip"])
        self.controller_chip.remove_css_class("warn")
        if controller["state"] == "warn":
            self.controller_chip.add_css_class("warn")

        mode = rt.mode_summary()
        self.mode_label.set_text(mode["label"])
        self._set_state_class(self.mode_dot, mode["state"])
        self.mode_chip.set_tooltip_text(mode["tooltip"])
        self.controls_label.set_text(f"{t('button.controls')} · {mode['choice_label']}")

        self._refresh_hero()
        self._refresh_library()
        for app in rt.catalog.apps:
            tile = self._tiles.get(app.id)
            if tile:
                state = rt.app_state(app.id)
                text, css = self._tile_status(app.id, state)
                tile.status_label.set_text(text)
                for name in ("ok", "accent", "warn"):
                    tile.status_label.remove_css_class(name)
                if css:
                    tile.status_label.add_css_class(css)
        self.back_legend.set_text(t("button.back") if (self.sheet or self.page != "home") else t("footer.desktop").partition(" ")[2] or t("footer.desktop"))
        if self.sheet == "settings":
            self._refresh_settings_sheet()
        elif isinstance(self.sheet, tuple):
            self._render_app_sheet(self.sheet[1], keep_focus=True)
        self._refresh_message()

    def _set_state_class(self, dot, state):
        for name in ("ok", "accent", "warn", "bad"):
            dot.remove_css_class(name)
        if state:
            dot.add_css_class(state)

    def _refresh_hero(self):
        rt, t = self.runtime, self.t
        pending = rt.pending_app()
        session = rt.session_summary()
        self.hero.remove_css_class("empty")
        self.hero_spinner.set_visible(False)
        self.hero_spinner.stop()
        self.hero_action.set_visible(False)
        if pending:
            self.hero_eyebrow.set_text(pending.upper())
            self.hero_title.set_text(t("status.starting", name=pending))
            self.hero_body.set_text(t("hero.startingBody"))
            self.hero_spinner.set_visible(True)
            self.hero_spinner.start()
            self.hero_icon.set_from_icon_name(icon_name(self.icons, "play"))
            resumable = False
        elif session:
            self.hero_eyebrow.set_text(t("status.running").upper())
            self.hero_title.set_text(session["name"])
            self.hero_body.set_text(session["title"] or session["name"])
            self.hero_action.set_visible(True)
            self.hero_icon.set_from_icon_name(icon_name(self.icons, session["icon"], session["category"]))
            resumable = True
        else:
            self.hero.add_css_class("empty")
            self.hero_eyebrow.set_text(t("hero.noSession").upper())
            self.hero_title.set_text(t("hero.title"))
            self.hero_body.set_text(t("hero.noSessionBody"))
            self.hero_icon.set_from_icon_name(icon_name(self.icons, "play"))
            resumable = False
        if not resumable and self.get_focus() is self.hero:
            self._focus_default()
        self.hero.set_focusable(resumable)
        self.hero.set_can_target(resumable)

    def _refresh_library(self):
        rt = self.runtime
        ids = rt.library()
        if ids != self._library_ids:
            focused = self.get_focus().get_name() if self.get_focus() else None
            child = self.library_row.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.library_row.remove(child)
                child = nxt
            self._library_cards = {}
            for app_id in ids:
                app = rt.catalog.get(app_id)
                if app:
                    card = self._build_library_card(app)
                    self._library_cards[app_id] = card
                    self.library_row.append(card)
            self.library_row.append(self._build_add_card())
            self._library_ids = list(ids)
            if focused and focused.startswith("app:"):
                self._focus_by_name(focused) or self._focus_default()
        t = self.t
        for app_id, card in self._library_cards.items():
            state = rt.app_state(app_id)
            text, css = {
                "running": (t("status.running"), "accent"),
                "starting": (t("status.starting", name=rt.catalog.get(app_id).name), "accent"),
                "ready": (t("status.ready"), "ok"),
                "installing": (t("status.installing"), "warn"),
                "install": (t("status.notInstalled"), None),
                "missing": (t("status.notInstalled"), None),
                "unsupported": (t("status.unavailable"), "bad"),
            }[state]
            card.status_label.set_text(text)
            self._set_state_class(card.status_dot, css)
            card.spinner.set_visible(state == "starting")
            card.spinner.set_spinning(state == "starting")

    def _tile_status(self, app_id, state):
        t = self.t
        pinned = app_id in self.runtime.library()
        if state == "unsupported":
            return t("status.unavailable"), None
        if state == "installing":
            return t("status.installing"), "warn"
        if state in ("install", "missing"):
            return (t("button.install") if state == "install" else t("status.notInstalled")), None
        if pinned:
            return t("status.inLibrary"), "accent"
        return t("status.installed"), "ok"

    # ---- message / toast -----------------------------------------------
    def _refresh_message(self):
        message = self.runtime.message()
        if message is None:
            self.toast.set_reveal_child(False)
            return
        level, text, action = message
        for name in ("error", "progress", "info"):
            self.toast.content.remove_css_class(name)
        self.toast.content.add_css_class(level)
        self.toast_icon.set_from_icon_name(icon_name(self.icons, {"error": "warning", "progress": "play"}.get(level, "check")))
        self.toast_label.set_text(text)
        self.toast_button.set_visible(bool(action))
        if action:
            self.toast_button.set_label(self.t(action[0]))
            self.toast_button.action = action[1]
        self.toast.set_reveal_child(True)
        if self._toast_timer:
            GLib.source_remove(self._toast_timer)
            self._toast_timer = None
        if level == "info":
            self._toast_timer = GLib.timeout_add_seconds(5, self._on_toast_timeout)

    def _on_toast_timeout(self):
        self._toast_timer = None
        self.runtime.clear_message()
        return False

    def _on_toast_button(self, button):
        callback = getattr(button, "action", None)
        if callback:
            callback()

    # ---- pages and sheets ----------------------------------------------
    def show_page(self, page):
        if self.sheet:
            self.close_sheet()
        self.page = page
        self.stack.set_visible_child_name(page)
        self.refresh()
        self._focus_default()
        self.runtime.on_ui_changed()

    def open_settings(self, focus=None):
        self.sheet = "settings"
        self._set_sheet(self._build_settings_sheet())
        target = {"mode": f"mode:{self.runtime.mode_choice()}", "language": f"lang:{self.runtime.settings.language}"}.get(focus)
        if not (target and self._focus_by_name(target)):
            self._focus_first_in_sheet()
        self.runtime.on_ui_changed()

    def open_app(self, app_id):
        self.sheet = ("app", app_id)
        self._render_app_sheet(app_id)
        self._focus_first_in_sheet()
        self.runtime.on_ui_changed()

    def close_sheet(self):
        previous = self.sheet
        self.sheet = None
        self.scrim.set_visible(False)
        self.root.set_sensitive(True)
        name = {"settings": "settings"}.get(previous) if isinstance(previous, str) else f"tile:{previous[1]}" if previous else None
        if self.page == "home" and isinstance(previous, tuple):
            name = f"app:{previous[1]}"
        if not (name and self._focus_by_name(name)):
            self._focus_default()
        self.refresh()
        self.runtime.on_ui_changed()

    def _set_sheet(self, sheet_widget):
        child = self.scrim.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.scrim.remove(child)
            child = nxt
        # Sheets scroll instead of overflowing on short screens.
        holder = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                    propagate_natural_width=True, halign=Gtk.Align.CENTER,
                                    valign=Gtk.Align.CENTER, vexpand=True, hexpand=True)
        holder.set_child(sheet_widget)
        self._sheet_widget = sheet_widget
        self.scrim.append(holder)
        self.root.set_sensitive(False)
        self.scrim.set_visible(True)

    def _sheet_root(self):
        return getattr(self, "_sheet_widget", None) if self.scrim.get_first_child() else None

    def _focus_first_in_sheet(self):
        rows = self.nav_rows()
        if rows and rows[0]:
            rows[0][0].grab_focus()

    def _build_settings_sheet(self):
        t, rt = self.t, self.runtime
        sheet = box(spacing=10, css="gh-sheet", width_request=820)
        sheet.nav_rows = []
        title_row = hbox(12)
        title_row.append(label(t("settings.title"), "gh-sheet-title", hexpand=True))
        sheet.append(title_row)

        sheet.append(label(t("settings.controllerMode").upper(), "gh-sheet-label"))
        self.mode_target_label = label("", "gh-sheet-body", wrap=True)
        sheet.append(self.mode_target_label)
        modes = hbox(12, homogeneous=True)
        self.mode_options = {}
        for mode, title, help_key in ((AUTO, "settings.modeAuto", "settings.modeAutoHelp"),
                                      (GAME, "settings.modeGame", "settings.modeGameHelp"),
                                      (DESKTOP, "settings.modeDesktop", "settings.modeDesktopHelp")):
            option = Gtk.Button(css_classes=["gh-option", "gh-nav"])
            option.set_name(f"mode:{mode}")
            content = box(spacing=4)
            head = hbox(8)
            head.append(label(t(title), "gh-option-title", hexpand=True, ellipsize=True))
            check = Gtk.Image.new_from_icon_name(icon_name(self.icons, "check"))
            check.add_css_class("gh-option-check")
            check.set_pixel_size(18)
            head.append(check)
            content.append(head)
            content.append(label(t(help_key), "gh-option-body", wrap=True, lines=3))
            option.set_child(content)
            option.check = check
            option.connect("clicked", lambda _b, m=mode: rt.set_mode(m))
            self._hover_selects(option)
            modes.append(option)
            self.mode_options[mode] = option
        sheet.append(modes)
        self.mode_note = label("", "gh-disabled-note", wrap=True)
        sheet.append(self.mode_note)
        sheet.nav_rows.append(list(self.mode_options.values()))

        sheet.append(label(t("settings.language").upper(), "gh-sheet-label", margin_top=6))
        languages = [("system", t("settings.systemLanguage"))] + [(code, rt.translator.endonym(code)) for code in rt.translator.available]
        self.language_buttons = {}
        per_row = 4
        for start in range(0, len(languages), per_row):
            row = hbox(10, homogeneous=True)
            nav = []
            for code, name in languages[start:start + per_row]:
                button = Gtk.Button(css_classes=["gh-lang", "gh-nav"])
                button.set_name(f"lang:{code}")
                text = label(name, xalign=0.5, ellipsize=True)
                if code != "system":
                    text.set_direction(Gtk.TextDirection.RTL if code.split("-")[0] in ("ar", "fa", "he", "ur") else Gtk.TextDirection.LTR)
                button.set_child(text)
                button.connect("clicked", lambda _b, c=code: rt.set_language(c))
                self._hover_selects(button)
                row.append(button)
                nav.append(button)
                self.language_buttons[code] = button
            for _ in range(per_row - len(nav)):
                row.append(box())
            sheet.append(row)
            sheet.nav_rows.append(nav)

        toggles = hbox(12, homogeneous=True, margin_top=6)
        self.autostart_button, self.autostart_state = self._toggle(
            "autostart", t("settings.autostart"), t("settings.autostartHelp"), lambda *_: rt.toggle_autostart())
        self.mapper_button, self.mapper_state = self._toggle(
            "mapper", t("settings.mapper"), t("settings.mapperHelp"),
            lambda *_: rt.set_mapper_managed(not rt.mapper_managed()))
        toggles.append(self.autostart_button)
        toggles.append(self.mapper_button)
        sheet.append(toggles)
        sheet.nav_rows.append([self.autostart_button, self.mapper_button])

        buttons = hbox(12, halign=Gtk.Align.END, margin_top=6)
        close = self._nav_button(t("button.close"), "gh-pill", "sheet:close", lambda *_: self.close_sheet())
        buttons.append(close)
        sheet.append(buttons)
        sheet.nav_rows.append([close])
        self._refresh_settings_sheet()
        return sheet

    def _toggle(self, name, title, body, callback):
        button = Gtk.Button(css_classes=["gh-option", "gh-nav"])
        button.set_name(name)
        content = hbox(14)
        texts = box(spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        texts.append(label(title, "gh-option-title", wrap=True))
        texts.append(label(body, "gh-option-body", wrap=True, lines=3))
        content.append(texts)
        state = label("", "gh-toggle-state", xalign=0.5, valign=Gtk.Align.CENTER)
        content.append(state)
        button.set_child(content)
        button.connect("clicked", callback)
        self._hover_selects(button)
        return button, state

    @staticmethod
    def _set_toggle(state_label, enabled, t):
        state_label.set_text(t("settings.on") if enabled else t("settings.off"))
        (state_label.add_css_class if enabled else state_label.remove_css_class)("on")

    def _refresh_settings_sheet(self):
        rt, t = self.runtime, self.t
        choice = rt.mode_choice()
        target = rt.target_name()
        self.mode_target_label.set_text((t("settings.modeTarget", name=target) + " · " + t("settings.modeHelp")) if target else t("settings.modeNoTarget"))
        for mode, option in self.mode_options.items():
            checked = mode == choice
            option.check.set_visible(checked)
            (option.add_css_class if checked else option.remove_css_class)("checked")
        if not rt.mapper_available():
            note = t("status.mapperMissing")
        elif not rt.mapper_managed():
            note = t("status.mapperExternalHelp")
        else:
            note = ""
        self.mode_note.set_text(note)
        self.mode_note.set_visible(bool(note))
        current = rt.settings.language
        for code, button in self.language_buttons.items():
            checked = code == current
            (button.add_css_class if checked else button.remove_css_class)("checked")
            if checked:
                button.set_name(f"lang:{code}")
        self._set_toggle(self.autostart_state, rt.autostart_enabled(), t)
        self._set_toggle(self.mapper_state, rt.mapper_managed(), t)

    def _render_app_sheet(self, app_id, keep_focus=False):
        rt, t = self.runtime, self.t
        app = rt.catalog.get(app_id)
        if app is None:
            return
        focused = self.get_focus().get_name() if (keep_focus and self.get_focus()) else None
        state = rt.app_state(app_id)
        pinned = app_id in rt.library()
        sheet = box(spacing=16, css="gh-sheet", width_request=720)
        sheet.nav_rows = []
        head = hbox(20)
        head.append(badge(self.icons, app.icon, app.category, 50, "gh-badge large"))
        titles = box(spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        titles.append(label(app.name, "gh-sheet-title", ellipsize=True))
        meta = hbox(8)
        meta.append(label(t(f"category.{app.category}"), "gh-pill-status", xalign=0.5))
        text, css = self._tile_status(app_id, state)
        status = label(text, "gh-pill-status", xalign=0.5)
        if css:
            status.add_css_class(css)
        meta.append(status)
        titles.append(meta)
        head.append(titles)
        sheet.append(head)
        sheet.append(label(t(app.description_key), "gh-sheet-body", wrap=True))

        help_key = f"app.{app.id}.installHelp"
        if state == "unsupported":
            sheet.append(label(f'{t("discover.unsupported")} ({rt.arch})', "gh-disabled-note", wrap=True))
        elif state == "install":
            sheet.append(label(t(help_key) if rt.translator.has(help_key) else t("discover.installHelp"), "gh-sheet-body", wrap=True))
        elif state == "missing":
            sheet.append(label(t(help_key) if rt.translator.has(help_key) else t("status.notInstalledHelp", name=app.name), "gh-sheet-body", wrap=True))
        elif state == "installing":
            sheet.append(label(t("status.installing"), "gh-disabled-note", wrap=True))

        actions = hbox(12, margin_top=8)
        nav = []
        if state in ("ready", "running", "starting"):
            nav.append(self._sheet_action(t("hero.resume") if state == "running" else t("button.launch"), "primary", "sheet:launch",
                                          lambda: (self.close_sheet(), rt.activate_app(app.id))))
        elif state == "install":
            nav.append(self._sheet_action(t("button.install"), "primary", "sheet:install", lambda: rt.install(app.id)))
        if state != "unsupported":
            nav.append(self._sheet_action(t("button.remove") if pinned else t("button.add"), "", "sheet:pin",
                                          lambda: rt.set_pinned(app.id, not pinned)))
        if app.information_url:
            nav.append(self._sheet_action(t("button.info"), "", "sheet:info", lambda: rt.open_info(app.id)))
        for widget in nav:
            actions.append(widget)
        actions.append(box(hexpand=True))
        close = self._sheet_action(t("button.close"), "", "sheet:close", self.close_sheet)
        actions.append(close)
        sheet.append(actions)
        sheet.nav_rows.append(nav + [close])
        self._set_sheet(sheet)
        if focused:
            self._focus_by_name(focused) or self._focus_first_in_sheet()

    def _sheet_action(self, text, css, name, callback):
        button = Gtk.Button(label=text, css_classes=["gh-pill", "gh-nav"] + ([css] if css else []))
        button.set_name(name)
        button.connect("clicked", lambda *_: callback())
        self._hover_selects(button)
        return button

    # ---- navigation ----------------------------------------------------
    def nav_rows(self):
        def usable(widgets):
            return [w for w in widgets if w.get_visible() and w.get_focusable() and w.get_sensitive() and w.get_mapped()]

        if self.sheet:
            sheet = self._sheet_root()
            rows = getattr(sheet, "nav_rows", []) if sheet else []
            return [r for r in (usable(row) for row in rows) if r]
        header = [self.tab_home, self.tab_discover, self.settings_button]
        if self.page == "home":
            library = [c for c in self._iter_children(self.library_row)]
            rows = [header, [self.hero], library, [self.desktop_button, self.controls_button]]
        else:
            grid_rows = {}
            for tile in self._tiles.values():
                row = self.discover_grid.query_child(tile)[1]
                grid_rows.setdefault(row, []).append(tile)
            rows = [header] + [grid_rows[k] for k in sorted(grid_rows)] + [[self.desktop_button, self.controls_button]]
        return [r for r in (usable(row) for row in rows) if r]

    @staticmethod
    def _iter_children(widget):
        child = widget.get_first_child()
        while child:
            yield child
            child = child.get_next_sibling()

    def _center_x(self, widget):
        ok, rect = widget.compute_bounds(self)
        return rect.get_x() + rect.get_width() / 2 if ok else 0

    def navigate(self, direction):
        rows = self.nav_rows()
        if not rows:
            return
        focus = self.get_focus()
        location = next(((r, row) for r, row in enumerate(rows) if focus in row), None)
        if location is None:
            self._focus_default()
            return
        index, row = location
        x = self._center_x(focus)
        if direction in (LEFT, RIGHT):
            sign = -1 if direction == LEFT else 1
            candidates = [w for w in row if w is not focus and (self._center_x(w) - x) * sign > 1]
            if candidates:
                min(candidates, key=lambda w: abs(self._center_x(w) - x)).grab_focus()
            return
        target = index + (-1 if direction == UP else 1)
        if 0 <= target < len(rows):
            min(rows[target], key=lambda w: abs(self._center_x(w) - x)).grab_focus()

    def _focus_by_name(self, name):
        for row in self.nav_rows():
            for widget in row:
                if widget.get_name() == name:
                    widget.grab_focus()
                    return True
        return False

    def _focus_default(self):
        if self.sheet:
            self._focus_first_in_sheet()
            return
        if self.page == "home":
            if self.hero.get_focusable():
                self.hero.grab_focus()
                return
            first = self.library_row.get_first_child()
            if first:
                first.grab_focus()
                return
        else:
            tiles = list(self._tiles.values())
            if tiles:
                tiles[0].grab_focus()
                return
        self.desktop_button.grab_focus()

    def _on_focus_changed(self, *_):
        focus = self.get_focus()
        if self._selected is not None and self._selected is not focus:
            self._selected.remove_css_class("selected")
        self._selected = focus if focus is not None and focus.has_css_class("gh-nav") else None
        if self._selected is not None:
            self._selected.add_css_class("selected")

    def selected_name(self):
        return self._selected.get_name() if self._selected is not None else None

    def handle_action(self, action):
        if action in (UP, DOWN, LEFT, RIGHT):
            self.set_focus_visible(True)
            self.navigate(action)
        elif action == ACTIVATE:
            focus = self.get_focus()
            if focus is not None and focus.get_sensitive():
                focus.activate()
        elif action == BACK:
            self.back()
        elif action == MENU:
            self.close_sheet() if self.sheet == "settings" else self.open_settings()
        elif action in (PREV_TAB, NEXT_TAB) and not self.sheet:
            self.show_page("discover" if self.page == "home" else "home")

    def back(self):
        if self.sheet:
            self.close_sheet()
        elif self.page != "home":
            self.show_page("home")
        else:
            self.runtime.desktop()

    def _on_key(self, _controller, keyval, _keycode, state):
        mapping = {Gdk.KEY_Up: UP, Gdk.KEY_Down: DOWN, Gdk.KEY_Left: LEFT, Gdk.KEY_Right: RIGHT}
        if keyval in mapping and not state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            self.navigate(mapping[keyval])
            return True
        if keyval == Gdk.KEY_Escape:
            self.back()
            return True
        if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_Page_Down) and not self.sheet:
            self.show_page("discover" if self.page == "home" else "home")
            return True
        return False

    def _on_close_request(self, *_):
        self.runtime.desktop()
        return True

    # ---- responsive layout ----------------------------------------------
    def do_size_allocate(self, width, height, baseline):
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        if width >= WIDE_WIDTH and height >= WIDE_HEIGHT:
            layout = "gh-wide"
        elif height < COMPACT_HEIGHT:
            layout = "gh-compact"
        else:
            layout = None
        if layout != self._layout:
            self._layout = layout
            GLib.idle_add(self._apply_layout, layout)

    def _apply_layout(self, layout):
        for name in ("gh-wide", "gh-compact"):
            (self.add_css_class if name == layout else self.remove_css_class)(name)
        return False


def load_css(display, colors) -> Gtk.CssProvider:
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS.substitute(colors))
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return provider
