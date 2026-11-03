"""Gtk.Application, runtime wiring, effects executor and command line."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, VERSION, core, settings as settings_mod  # noqa: E402
from .catalog import Availability, application_dirs, load_catalog, machine_arch  # noqa: E402
from .i18n import Translator  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LAUNCH_TIMEOUT_S = 60
TERMINALS = ("omarchy-launch-floating-terminal-with-presentation", "xdg-terminal-exec")
USAGE = """Usage: omarchy-game-mode [COMMAND]
  show                    Show Home (default)
  --autostart             Login start: show Home (used by the autostart entry)
  background              Start resident without showing Home
  hide | desktop          Hide Home and return to the desktop
  resume                  Focus the tracked game session
  discover                Show the Discover store
  launch <app-id>         Launch a catalog app
  mode auto|game|desktop  Controller mode for the focused app
  autostart on|off|status Login autostart (owned XDG entry)
  mapper on|off|status    Let Game Mode switch AntiMicroX sets (off by default)
  status                  Print JSON status
  quit                    Restore controller sets and exit
  --version, help"""


class Runtime:
    def __init__(self, app: Gtk.Application):
        self.app = app
        self.settings = settings_mod.Settings()
        self.catalog = load_catalog(DATA_DIR / "catalog.json")
        self.translator = Translator(DATA_DIR / "locales", self.settings.language)
        self.arch = machine_arch()
        self.availability = Availability()
        self.installed = self._probe_installed()
        self.installing: set[str] = set()
        self.model = core.HomeModel(core.Classifier(self.catalog))
        self.window = None
        self._timeouts: dict[int, int] = {}
        self._launch_timer = None
        self._ui_dirty = None
        self._monitors = []
        self.interpreter = core.ControllerInterpreter(self._schedule, GLib.source_remove, self._on_controller_action)
        from .hypr import HyprlandIPC
        from .input import ControllerMonitor
        from .mapper import AntimicroxMapper
        self.input = ControllerMonitor(self.interpreter, self.on_ui_changed)
        self.hypr = HyprlandIPC(self._on_windows_changed, self._on_ipc_changed)
        self.mapper = AntimicroxMapper(self._on_mapper_changed, managed=self.settings.mapper_managed)
        self._stopped = False
        self._claim_focus = False

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        from .ui import load_css
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(DATA_DIR / "icons"))
        self.css = load_css(display, settings_mod.theme_colors())
        self._watch_dirs()
        self.mapper.start()
        self.input.start()
        if not self.hypr.start():
            self._run(self.model.ipc_lost())
            if self.hypr.dir is not None:
                self.hypr._schedule_retry()

    def stop(self):
        """Idempotent cleanup: restore mapper sets, close devices and sockets."""
        if self._stopped:
            return
        self._stopped = True
        self._cancel_timeout()
        self.interpreter.set_nav_enabled(False)
        self.interpreter.reset()
        self.mapper.stop()
        self.input.stop()
        self.hypr.stop()
        for monitor in self._monitors:
            monitor.cancel()
        self._monitors = []

    def _watch_dirs(self):
        dirs = [(d, self._on_apps_changed) for d in application_dirs()]
        dirs.append((settings_mod.theme_dir() / "theme", self._on_theme_changed))
        for path, callback in dirs:
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            except GLib.Error:
                continue
            monitor.connect("changed", lambda *_a, cb=callback: cb())
            self._monitors.append(monitor)

    def _on_theme_changed(self):
        from gi.repository import Gdk
        Gtk.StyleContext.remove_provider_for_display(Gdk.Display.get_default(), self.css)
        from .ui import load_css
        self.css = load_css(Gdk.Display.get_default(), settings_mod.theme_colors())

    def _probe_installed(self) -> set[str]:
        return {a.id for a in self.catalog.apps if a.supported_on(self.arch) and self.availability.installed(a)}

    def _on_apps_changed(self):
        installed = self._probe_installed()
        for app_id in sorted(installed - self.installed):
            if app_id in self.installing:
                self.installing.discard(app_id)
                if app_id not in self.library():
                    self.settings.set_pinned(self.catalog, app_id, True)
                self.model.message = ("info", "status.installedNow", {"service": app_id})
        if installed != self.installed:
            self.installed = installed
            self.on_ui_changed()

    # ---- windows and IO events --------------------------------------------
    def _on_windows_changed(self):
        try:
            clients, active = self.hypr.snapshot()
        except (OSError, ValueError):
            return
        windows = [w for w in (core.Window.from_hypr(c) for c in clients) if w]
        self._run(self.model.update_windows(windows, core.Window.from_hypr(active)))
        self._focus_home_if_needed()
        self._sync_nav()

    def _focus_home_if_needed(self):
        """Hyprland may map Home without focusing it (autostart without an
        activation token, or focus lost to nothing); claim focus once per show."""
        if not self._claim_focus:
            return
        if not (self.window and self.window.get_visible()):
            self._claim_focus = False
            return
        address = self.model.home_address(os.getpid())
        if address is None:
            return  # not mapped yet; the openwindow event retries
        self._claim_focus = False
        if self.model.active is None or self.model.active.address != address:
            self.hypr.focus(address)

    def _on_ipc_changed(self, connected):
        self._run(self.model.ipc_restored() if connected else self.model.ipc_lost())

    def _on_mapper_changed(self):
        self._sync_nav()
        self.on_ui_changed()

    def window_state_changed(self, *_):
        win = self.window
        visible = bool(win and win.get_visible())
        self._run(self.model.set_home_state(visible, bool(visible and win.is_active())))
        if visible:
            self._on_apps_changed()
        self._sync_nav()

    def _sync_nav(self):
        self.interpreter.set_nav_enabled(self.model.home_focused and self.mapper.raw_navigation_allowed())

    def _schedule(self, ms, callback):
        def fire():
            callback()
            return False
        return GLib.timeout_add(ms, fire)

    def _on_controller_action(self, action):
        if action == core.HOME:
            self.show()
        elif self.window and self.model.home_focused:
            self.window.handle_action(action)

    # ---- effects -------------------------------------------------------------
    def _run(self, effects):
        for effect in effects or []:
            kind, args = effect[0], effect[1:]
            if kind == "show":
                self.show()
            elif kind == "hide":
                if self.window:
                    self.window.set_visible(False)
            elif kind == "focus":
                self.hypr.focus(args[0])
            elif kind == "mapper":
                self.mapper.request(args[0])
            elif kind == "spawn":
                self._spawn(*args)
            elif kind == "arm_launch_timeout":
                self._cancel_timeout()
                token = args[0]
                self._launch_timer = GLib.timeout_add_seconds(LAUNCH_TIMEOUT_S, self._on_launch_timeout, token)
            elif kind == "cancel_launch_timeout":
                self._cancel_timeout()
            elif kind == "reset_input":
                self.interpreter.reset()
        self.on_ui_changed()

    def _cancel_timeout(self):
        if self._launch_timer:
            GLib.source_remove(self._launch_timer)
            self._launch_timer = None

    def _on_launch_timeout(self, token):
        self._launch_timer = None
        self._run(self.model.launch_timed_out(token))
        return False

    def _spawn(self, service, token):
        app = self.catalog.get(service)
        argv = list(app.launch)
        if shutil.which("uwsm-app") and argv[0] != "uwsm-app":
            argv = ["uwsm-app", "--", *argv]
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            self._run(self.model.launch_failed(token, "status.error", reason=error.message))
            return
        proc.wait_async(None, self._on_spawn_exit, token)

    def _on_spawn_exit(self, proc, result, token):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            return
        ok = proc.get_if_exited() and proc.get_exit_status() == 0
        self._run(self.model.launch_exited(token, ok, proc.get_exit_status() if proc.get_if_exited() else -1))

    # ---- UI runtime API ------------------------------------------------------
    def t(self, key, **params):
        if "service" in params:
            app = self.catalog.get(params["service"])
            params.setdefault("name", app.name if app else params["service"])
        return self.translator(key, **params)

    def library(self):
        return self.settings.pinned(self.catalog)

    def app_state(self, app_id):
        app = self.catalog.get(app_id)
        if not app.supported_on(self.arch):
            return "unsupported"
        pending = self.model.pending
        if pending and pending.service == app_id:
            return "starting"
        if app_id in self.installed:
            return "running" if self.model._session_window(app_id) else "ready"
        if app_id in self.installing:
            return "installing"
        return "install" if app.install else "missing"

    def pending_app(self):
        pending = self.model.pending
        return self.catalog.get(pending.service).name if pending and self.catalog.get(pending.service) else None

    def session_summary(self):
        window = self.model.session_window()
        if window is None:
            return None
        app = self.catalog.get(self.model.service(window) or "")
        return {"name": app.name if app else window.wm_class or "Game", "title": window.title,
                "icon": app.icon if app else "controller", "category": app.category if app else "native"}

    def controller_summary(self):
        status = self.input.status()
        if status["devices"] and self.mapper.external:
            return {"label": self.t("status.mapperExternal"), "state": "warn", "tooltip": self.t("status.mapperExternalHelp")}
        if status["devices"]:
            names = ", ".join(d["name"] for d in status["devices"])
            return {"label": self.t("status.controllerConnected"), "state": "ok", "tooltip": names}
        if any("permission" in e["error"] for e in status["errors"]):
            return {"label": self.t("status.controllerPermission"), "state": "warn", "tooltip": "; ".join(e["error"] for e in status["errors"])}
        return {"label": self.t("status.controllerUnavailable"), "state": None, "tooltip": ""}

    def mode_summary(self):
        choice_keys = {core.AUTO: "settings.modeAuto", core.GAME: "settings.modeGame", core.DESKTOP: "settings.modeDesktop"}
        effective = self.model.target_effective_mode() or core.DESKTOP
        label = self.t("status.modeGame" if effective == core.GAME else "status.modeDesktop")
        if self.mapper.error:
            tooltip = self.mapper.error
        elif self.mapper.external:
            tooltip = self.t("status.mapperExternalHelp")
        else:
            tooltip = "" if self.mapper.available else self.t("status.mapperMissing")
        return {"label": label, "state": "accent" if effective == core.GAME else "ok", "tooltip": tooltip,
                "choice_label": self.t(choice_keys[self.model.target_mode_choice()])}

    def mode_choice(self):
        return self.model.target_mode_choice()

    def target_name(self):
        target = self.model.target_window()
        if target is None:
            return None
        app = self.catalog.get(self.model.service(target) or "")
        return app.name if app else (target.title or target.wm_class)

    def mapper_available(self):
        return self.mapper.available

    def mapper_managed(self):
        return self.mapper.managed

    def set_mapper_managed(self, managed: bool):
        self.settings.mapper_managed = managed
        self.settings.save()
        self.mapper.set_managed(managed)
        if managed and self.model.requested_mode:
            self.mapper.request(self.model.requested_mode)
        self._sync_nav()
        self.on_ui_changed()

    def autostart_enabled(self):
        return settings_mod.autostart_enabled()

    def toggle_autostart(self):
        ok, detail = settings_mod.set_autostart(not self.autostart_enabled())
        if not ok:
            self.model.message = ("error", "status.error", {"name": self.t("settings.autostart"), "reason": detail})
        self.on_ui_changed()

    def set_mode(self, mode):
        self._run(self.model.set_manual_mode(mode))

    def set_language(self, code):
        self.settings.language = code
        self.settings.save()
        self.translator = Translator(DATA_DIR / "locales", code)
        self._apply_direction()
        if self.window:
            self.window.sheet = None
            self.window.build()
            self.window.open_settings(focus="language")

    def _apply_direction(self):
        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL if self.translator.rtl else Gtk.TextDirection.LTR)

    def set_pinned(self, app_id, pinned):
        self.settings.set_pinned(self.catalog, app_id, pinned)
        self.on_ui_changed()

    def install(self, app_id):
        app = self.catalog.get(app_id)
        if not app or not app.install or not app.supported_on(self.arch):
            return
        terminal = next((t for t in TERMINALS if shutil.which(t)), None)
        if terminal is None:
            self.model.message = ("error", "status.noTerminal", {"service": app_id})
            return self.on_ui_changed()
        try:
            Gio.Subprocess.new([terminal, *app.install], Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            self.model.message = ("error", "status.error", {"service": app_id, "reason": error.message})
            return self.on_ui_changed()
        self.installing.add(app_id)
        self.model.message = ("info", "status.installOpened", {"service": app_id})
        if self.window:
            self.window.close_sheet()
            self.window.set_visible(False)
        self.on_ui_changed()

    def open_info(self, app_id):
        app = self.catalog.get(app_id)
        if app and app.information_url:
            Gio.AppInfo.launch_default_for_uri(app.information_url, None)

    def activate_app(self, app_id):
        state = self.app_state(app_id)
        if state in ("ready", "running"):
            self._run(self.model.begin_launch(app_id))
        elif self.window:
            self.window.open_app(app_id)

    def resume(self):
        self._run(self.model.resume())

    def desktop(self):
        self._run(self.model.desktop())

    def message(self):
        message = self.model.message
        if message is None:
            return None
        level, key, params = message
        action = None
        if level == "error" and params.get("service") and key in ("status.error", "status.launchTimeout"):
            action = ("button.retry", lambda s=params["service"]: self.activate_app(s))
        return level, self.t(key, **params), action

    def clear_message(self):
        self.model.clear_message()
        self.on_ui_changed()

    def on_ui_changed(self):
        if self.window and self._ui_dirty is None:
            self._ui_dirty = GLib.idle_add(self._refresh_ui)

    def _refresh_ui(self):
        self._ui_dirty = None
        if self.window:
            self.window.refresh()
        return False

    # ---- show / status -------------------------------------------------------
    def show(self, page=None):
        from .ui import HomeWindow
        if self.window is None:
            self._apply_direction()
            self.window = HomeWindow(self.app, self)
            self.window.connect("notify::visible", self.window_state_changed)
            self.window.connect("notify::is-active", self.window_state_changed)
        if page:
            self.window.show_page(page)
        self.window.fullscreen()
        self.window.present()
        self._claim_focus = True
        self._focus_home_if_needed()

    def status(self):
        m = self.model
        win = self.window
        pending = m.pending
        return {
            "running": True, "version": VERSION,
            "visible": bool(win and win.get_visible()), "focused": m.home_focused,
            "page": win.page if win else None,
            "sheet": (win.sheet if isinstance(win.sheet, str) else list(win.sheet)) if win and win.sheet else None,
            "selected": win.selected_name() if win else None,
            "active_window": m.describe(m.active),
            "target": {**(m.describe(m.target_window()) or {}), "mode_choice": m.target_mode_choice(),
                       "intended_mode": m.target_effective_mode()} if m.target_window() else None,
            "mode": {"desired": m.desired_mode(), "requested": m.requested_mode, "mapper": self.mapper.status(),
                     "raw_navigation": self.interpreter.nav_enabled},
            "controllers": self.input.status(),
            "ipc": {"connected": self.hypr.connected, "error": self.hypr.error},
            "launch": {"service": pending.service, "handoff": pending.handoff} if pending else None,
            "session": {"service": m.session_service, "window": m.describe(m.session_window())},
            "library": self.library(), "installed": sorted(self.installed), "installing": sorted(self.installing),
            "language": self.translator.language, "rtl": self.translator.rtl,
            "autostart": self.autostart_enabled(),
            "catalog": {"source": self.catalog.source, "errors": self.catalog.errors},
            "message": (lambda msg: {"level": msg[0], "text": msg[1]} if msg else None)(self.message()),
        }


class GameModeApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.runtime: Runtime | None = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, self._on_signal)

    def _on_signal(self):
        self.shutdown_runtime()
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self):
        self.shutdown_runtime()
        Gtk.Application.do_shutdown(self)

    def shutdown_runtime(self):
        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            runtime.stop()
            self.release()

    def ensure_runtime(self) -> Runtime:
        if self.runtime is None:
            self.runtime = Runtime(self)
            self.runtime.start()
            self.hold()
        return self.runtime

    def do_command_line(self, cmdline):
        args = list(cmdline.get_arguments()[1:])
        code, out = self.dispatch(args)
        if out:
            cmdline.print_literal(out + "\n") if code == 0 else cmdline.printerr_literal(out + "\n")
        return code

    def dispatch(self, args):
        command = args[0] if args else "show"
        rest = args[1:]
        if command in ("help", "--help", "-h"):
            return 0, USAGE
        if command == "--version":
            return 0, VERSION
        if command == "autostart":
            if rest and rest[0] in ("on", "off"):
                ok, detail = settings_mod.set_autostart(rest[0] == "on")
                if self.runtime:
                    self.runtime.on_ui_changed()
                return (0 if ok else 1), detail
            return 0, "on" if settings_mod.autostart_enabled() else "off"
        if command == "mapper":
            if rest and rest[0] in ("on", "off"):
                if self.runtime:
                    self.runtime.set_mapper_managed(rest[0] == "on")
                else:
                    stored = settings_mod.Settings()
                    stored.mapper_managed = rest[0] == "on"
                    stored.save()
                return 0, rest[0]
            if rest and rest[0] != "status":
                return 2, USAGE
            managed = self.runtime.mapper.managed if self.runtime else settings_mod.Settings().mapper_managed
            return 0, "on" if managed else "off"
        if command == "status":
            return 0, json.dumps(self.runtime.status() if self.runtime else {"running": False}, ensure_ascii=False)
        if command == "quit":
            self.shutdown_runtime()
            GLib.idle_add(self.quit)
            return 0, ""
        rt = self.ensure_runtime()
        if command in ("show", "--autostart"):
            rt.show()
        elif command == "discover":
            rt.show("discover")
        elif command == "background":
            pass
        elif command in ("hide", "desktop"):
            rt.desktop()
        elif command == "resume":
            rt.resume()
        elif command == "launch" and rest and rt.catalog.get(rest[0]):
            rt.show()
            rt.activate_app(rest[0])
        elif command == "mode" and rest and rest[0] in core.MODES:
            rt.set_mode(rest[0])
        else:
            return 2, USAGE
        return 0, ""


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    app = GameModeApplication()
    command = argv[1] if len(argv) > 1 else "show"
    if command in ("status", "quit", "help", "--help", "-h", "--version", "autostart", "mapper"):
        # Answer locally when no instance runs, without starting watchers.
        try:
            app.register(None)
        except GLib.Error:
            pass
        if not app.get_is_remote():
            code, out = app.dispatch(argv[1:])
            if out:
                print(out, file=sys.stdout if code == 0 else sys.stderr)
            return code
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
