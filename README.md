# Omarchy Game Mode

An optional, controller-first fullscreen launcher for [Omarchy](https://omarchy.org).
It gives you a **Gaming Home** (your library and a Resume card for the current session)
and **Discover**, a small store of gaming services. It hands focus to games through
Hyprland and gets out of the way when you go back to the desktop.

It is a GTK4 / python-gobject app that uses only the Python standard library. It is
not a device driver: it does not remap firmware, grab devices, or inject input.

## Features

- **Library**: pinned services (Steam Big Picture, Xbox Remote Play via Greenlight,
  Xbox Cloud Gaming, GeForce NOW, Moonlight, RetroArch, Heroic, Lutris, Battle.net,
  Minecraft). Resume becomes active only while a tracked game window exists.
- **Discover**: shows whether each service is installed, pinned, or installable. You
  start installs yourself, and they run in a visible terminal using Omarchy's own
  installers (`omarchy install gaming …`, `omarchy pkg …`) so you see every privilege
  prompt. The app never installs anything at startup. Services your CPU architecture
  doesn't support are shown with the reason.
- **Handoff**: Game Mode hides only after a launch is accepted. It waits for the real
  game window, so wrappers that exit early (for example Steam) are handled. When the
  tracked session ends, Game Mode comes back, unless you're already busy in another
  window. Gamescope is tracked as one opaque session.
- **Languages**: English, Español, Français, Deutsch, Português (Brasil), 日本語 and
  العربية (right-to-left layout). By default it follows `~/.config/omarchy/language`,
  then `LANG`, and you can pick a language in Settings. Missing strings fall back to
  English.
- **Theme**: uses your current Omarchy theme colors when they can be read, with a dark
  fallback. There are no background images, network fetches or idle redraws.

## Controls

| Input | Action |
| --- | --- |
| A | Select |
| B | Back, or go to the Desktop from Home |
| D-pad / left stick | Move selection (repeats only while held) |
| LB / RB | Switch Home / Discover |
| Menu (Start) | Settings |
| **Hold View + Menu for 0.8 s** | Show Game Mode from anywhere |

Keyboard (arrows, Enter, Escape), mouse and touch work everywhere.

**About the Home chord.** The global gesture is holding **View (Back) and Menu (Start)
together for at least 0.8 seconds**. A single or brief press does nothing. The native
Guide/Xbox button is deliberately left alone, so Steam and other apps keep using it.
Vendor-specific buttons (for example ROG Ally's dedicated keys) are not mapped.

Controller input is read **read-only and non-exclusively** from evdev. Devices that
report gamepad buttons or a hat are used, and virtual devices are skipped. Your user
needs read access to `/dev/input/event*` (usually the `input` group, or a uaccess rule).
If access is denied, Game Mode says so on screen and keeps working with keyboard and
touch.

## Controller modes

Settings → Controller mode:

- **Automatic**: game input inside games, desktop navigation on the desktop.
- **Game input**: the controller goes to the focused app unchanged.
- **Desktop navigation**: the controller drives the desktop through your mapper.

A manual choice lasts until focus moves to a different game or to the desktop. Opening
the Omarchy menu or other overlays does not reset it.

**How games are recognised.** Game Mode treats a window as a game when it:

- matches a catalog app (for example `steam_app_*` or Moonlight), or
- is gamescope, or reports `contentType` game, or
- is fullscreen and is not a known desktop app (terminals, browsers and web apps, file
  managers, video players, editors), or
- belongs to a process started by Steam, Heroic, Lutris, Battle.net or the Minecraft
  launcher. Game Mode checks this by reading the parent pid and start time from
  `/proc/<pid>/stat`; it reads nothing else. A game found this way stays a game for as
  long as its process runs, even if the launcher closes its window or exits. Start
  times guard against reused pids. At most 64 such processes are remembered, and only
  while they are alive. A game started by a launcher that had already exited, or one
  that runs through a helper outside the launcher's process tree, falls back to the
  fullscreen rule or to manual mode.

Anything else counts as desktop. If a game is missed, pick **Game input** for it in
Settings.

### AntiMicroX (optional, off by default)

[AntiMicroX](https://github.com/AntiMicroX/antimicrox) is optional. Game Mode only
switches its profile sets after you turn on **Settings → Switch AntiMicroX sets**, or
run `omarchy-game-mode mapper on`. With this on:

- **Desktop:** set 1, your pointer and keys mapping.
- **Games and Gaming Home:** set 2. Leave it empty so games get the raw controller.

Before its first change, Game Mode records each controller's current set. It puts
those sets back when it quits, when it gets SIGTERM, SIGINT or SIGHUP, or when you
turn the option off. It never edits AntiMicroX profiles.

If AntiMicroX is running but Game Mode isn't allowed to switch it, Game Mode leaves it
alone. It also stops reading controller buttons for its own menus, so no button acts
twice. The header says the controller is paused; keyboard and touch keep working. Game
Mode also uses the controller directly when AntiMicroX isn't running.

## Install

From source (needs `python`, `python-gobject` and `gtk4`):

```bash
make test
make install PREFIX="$HOME/.local"      # or: sudo make install (PREFIX=/usr)
```

Arch package from the local checkout:

```bash
make dist && makepkg -si
```

## Command line

```
omarchy-game-mode [show]             Show Game Mode (starts it if needed)
omarchy-game-mode --autostart        Show Home at login (used by the login entry)
omarchy-game-mode background         Start resident without showing a window
omarchy-game-mode desktop            Hide and return to the desktop
omarchy-game-mode resume             Focus the tracked game session
omarchy-game-mode discover           Open Discover
omarchy-game-mode launch <app-id>    e.g. steam, remote, cloud, geforce-now
omarchy-game-mode mode auto|game|desktop
omarchy-game-mode autostart on|off|status
omarchy-game-mode status             Print JSON status
omarchy-game-mode mapper on|off|status  Let Game Mode switch AntiMicroX sets
omarchy-game-mode quit               Restore the original AntiMicroX sets and exit
```

Autostart is **off** by default. `autostart on` writes
`~/.config/autostart/io.github.omarchy.GameMode.desktop`, which runs the installed
launcher by its absolute path with `--autostart`, so Gaming Home appears at login.
`background` starts the app without showing a window. `autostart off` removes only
the autostart file Game Mode created.

`omarchy-game-mode mapper on|off|status` turns AntiMicroX set switching on or off.

## Uninstall

```bash
omarchy-game-mode autostart off
omarchy-game-mode quit
sudo pacman -R omarchy-game-mode      # or: make uninstall PREFIX=...
```

Your settings live in `~/.config/omarchy-game-mode/`; delete that folder if you want
them gone. Game Mode never edits AntiMicroX profiles or any other app's configuration.

## Limitations

- Needs Hyprland for window tracking and handoff. Without it, the app still opens and
  shows the IPC status.
- Firmware "hold" gestures and hidraw-only buttons are not supported.
- Game recognition is a heuristic. A windowed game that wasn't started by a supported
  launcher counts as desktop until you choose **Game input** for it.
- Nothing in the automated tests can show that the physical Guide button still reaches
  Steam. The tests only check that Game Mode ignores Guide; confirm the rest by hand on
  your device.
- CI workflows exist in the repository but are not yet enabled on GitHub.

## Credits and provenance

All code, CSS and SVG icons in this repository were written for this project and are
MIT licensed. Nothing was copied from other projects, icon sets (such as Font Awesome
or Material) or fonts, and no font files are bundled. The UI uses your system font
through GTK.

The service icons are simple original glyphs that only suggest each service. Steam,
Xbox, GeForce NOW, Moonlight, RetroArch, Heroic, Lutris, Battle.net, Minecraft and
other names are trademarks of their owners. This project is not affiliated with or
endorsed by any of them.

Built on, but not bundling:

- [GTK 4](https://gtk.org) and [PyGObject](https://pygobject.gnome.org), used through
  the system packages.
- [Omarchy](https://omarchy.org): its commands are called at runtime (`omarchy install
  gaming …`, `omarchy pkg …`, `omarchy launch webapp`, `uwsm-app`), and the theme
  colors are read from `~/.local/state/omarchy/current/theme/colors.toml` when that file
  exists.
- [Hyprland](https://hyprland.org) IPC sockets, and the optional
  [AntiMicroX](https://github.com/AntiMicroX/antimicrox) D-Bus interface.

Translations come from `data/locales` and were drafted for this project.

## Related projects

These are references and inspiration only. No code is copied from them, and Game Mode
does not integrate with them (not even HHD or InputPlumber):

- [OpenGamepadUI](https://github.com/ShadowBlip/OpenGamepadUI): Godot controller-native
  launcher and overlay.
- [Handheld Daemon (hhd)](https://github.com/hhd-dev/hhd): handheld controller,
  gyro and TDP support. It complements Game Mode and is a good choice for Ally hardware.
- [omarchy-rog-ally-codex-plugin](https://github.com/richelle-belle/omarchy-rog-ally-codex-plugin):
  ROG Ally setup automation.
- [omarchy-launcher](https://github.com/headband42/omarchy-launcher): Quickshell
  launcher for Omarchy.

## License

MIT. See [LICENSE](LICENSE).
