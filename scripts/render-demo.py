"""Render the real GTK UI with isolated fixture state into a silent guided video.

Run with a local GTK Broadway display and browser connected; see media/README.md.
No clients are launched, controllers read, settings saved or installs performed.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from ui_smoke import FakeRuntime  # noqa: E402
from gaminghome import application as application_mod, core  # noqa: E402
from gaminghome.settings import FALLBACK_COLORS  # noqa: E402
from gaminghome.ui import HomeWindow, load_css  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

FPS = 12
DURATION = 56
CHAPTERS = [
    (0, 7, "YOUR GAMES. YOUR PLATFORMS. YOUR DESKTOP.",
     "Home brings pinned platforms and a Resume card together."),
    (7, 14, "DISCOVER YOUR NEXT WAY TO PLAY",
     "Browse ten platforms. See what is installed, pinned or available."),
    (14, 21, "YOU CHOOSE WHAT GETS INSTALLED",
     "Inspect a platform first. Installs require an explicit action."),
    (21, 28, "MAKE HOME YOUR OWN",
     "Pin a platform without installing it. Remove a pin without uninstalling."),
    (28, 35, "AUTOMATIC WHEN IT FITS. MANUAL WHEN YOU NEED IT.",
     "Choose Automatic, Game input or Desktop navigation in Settings."),
    (35, 42, "YOUR LANGUAGE, YOUR LAYOUT",
     "Seven interface languages, including Arabic with right-to-left layout."),
    (42, 49, "OPTIONAL STARTUP. EXPLICIT MAPPER CONTROL.",
     "Show Home at login if you choose. AntiMicroX set switching is opt-in."),
    (49, 56, "HELP MAKE HANDHELD GAMING FEEL AT HOME",
     "Test a handheld, review a translation, or build a focused improvement."),
]


class DemoRuntime(FakeRuntime):
    def install(self, app_id):
        raise RuntimeError("Demo must never install applications")

    def activate_app(self, app_id):
        raise RuntimeError("Demo must never launch applications")

    def toggle_autostart(self):
        raise RuntimeError("Demo must never change login startup")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    app = Gtk.Application(application_id="io.github.brianbaldock.OmarchyGameModeDemo")

    with tempfile.TemporaryDirectory(prefix="game-mode-demo-") as directory:
        work = Path(directory)
        filters = [
            "scale=1280:720:force_original_aspect_ratio=decrease",
            "pad=1280:900:(ow-iw)/2:0:color=0x10121b",
            "drawbox=x=0:y=720:w=iw:h=180:color=0x10121b:t=fill",
            "drawbox=x=40:y=744:w=64:h=4:color=0xa7cd75:t=fill",
        ]
        font = "/usr/share/fonts/liberation/LiberationSans-Regular.ttf"
        for i, (start, end, title, guidance) in enumerate(CHAPTERS):
            for name, text, size, y, color in [
                ("title", title, 26, 764, "0xa7cd75"),
                ("guidance", guidance, 23, 807, "0xe1e5f2"),
            ]:
                path = work / f"{i}-{name}.txt"
                path.write_text(text)
                filters.append(
                    f"drawtext=fontfile={font}:textfile={path}:fontsize={size}:"
                    f"fontcolor={color}:x=40:y={y}:enable='gte(t,{start})*lt(t,{end})'"
                )
        footer = work / "footer.txt"
        footer.write_text("REAL APP UI / SIMULATED SESSION  |  COMMUNITY PREVIEW  |  github.com/brianbaldock/omarchy-game-mode")
        filters.append(
            f"drawtext=fontfile={font}:textfile={footer}:fontsize=16:"
            "fontcolor=0xa8afc5:x=40:y=862"
        )
        encoder = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "image2pipe", "-framerate", str(FPS), "-i", "-",
             "-vf", ",".join(filters), "-an", "-c:v", "libx264",
             "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(args.output)],
            stdin=subprocess.PIPE,
        )
        state = {"frame": 0, "error": None, "last_frame": None}

        def activate(application):
            display = Gdk.Display.get_default()
            Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
            Gtk.IconTheme.get_for_display(display).add_search_path(str(application_mod.DATA_DIR / "icons"))
            load_css(display, {**FALLBACK_COLORS, "accent": "#a7cd75"})
            runtime = DemoRuntime(application, "en")
            runtime.settings.language = "en"
            runtime._apply_direction()
            window = HomeWindow(application, runtime)
            runtime.window = window
            window.set_decorated(False)
            window.set_default_size(1280, 720)
            window.present()

            def pin():
                runtime.set_pinned("geforce-now", True)
                window.refresh()
                window._focus_by_name("sheet:pin")

            def english():
                runtime.set_language("en")
                window.open_settings()

            actions = {
                0: window._focus_default,
                7: lambda: window.show_page("discover"),
                10: lambda: window._focus_by_name("tile:geforce-now"),
                14: lambda: window.open_app("geforce-now"),
                21: pin,
                24: lambda: runtime.set_pinned("geforce-now", False),
                26: lambda: (window.close_sheet(), window.show_page("home")),
                28: lambda: window.open_settings(focus="mode"),
                31: lambda: runtime.set_mode(core.GAME),
                33: lambda: runtime.set_mode(core.AUTO),
                35: lambda: runtime.set_language("ar"),
                38: lambda: (window.close_sheet(), window.show_page("discover")),
                42: english,
                49: lambda: (window.close_sheet(), window.show_page("home")),
            }

            def frame():
                try:
                    number = state["frame"]
                    if number == DURATION * FPS:
                        application.quit()
                        return False
                    if number % FPS == 0 and number // FPS in actions:
                        actions[number // FPS]()
                    paintable = Gtk.WidgetPaintable.new(window)
                    snapshot = Gtk.Snapshot()
                    paintable.snapshot(snapshot, window.get_width(), window.get_height())
                    node = snapshot.to_node()
                    if node is not None:
                        texture = window.get_renderer().render_texture(node, None)
                        state["last_frame"] = texture.save_to_png_bytes().get_data()
                    if state["last_frame"] is None:
                        raise RuntimeError(f"No rendered UI at frame {number}")
                    encoder.stdin.write(state["last_frame"])
                    state["frame"] += 1
                    return True
                except Exception as error:
                    state["error"] = error
                    application.quit()
                    return False

            GLib.timeout_add(2000, lambda: (GLib.timeout_add(round(1000 / FPS), frame), False)[1])

        app.connect("activate", activate)
        try:
            app.run([])
        finally:
            encoder.stdin.close()
            result = encoder.wait()
        if state["error"]:
            raise state["error"]
        if result or state["frame"] != DURATION * FPS:
            raise RuntimeError(f"Encoding failed: exit={result}, frames={state['frame']}")
    print(args.output)


if __name__ == "__main__":
    main()
