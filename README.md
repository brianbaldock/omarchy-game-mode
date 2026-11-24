# Omarchy Game Mode

An optional, controller-first fullscreen launcher for [Omarchy](https://omarchy.org).
It gives you a **Gaming Home** (your library and a Resume card for the current session)
and **Discover**, a small store of gaming services. It hands focus to games through
Hyprland and gets out of the way when you go back to the desktop.

It is a GTK4 / python-gobject app that uses only the Python standard library. It is
not a device driver: it does not remap firmware, grab devices, or inject input.

## Build with us

We need handheld testers, native-language reviewers, Python/GTK builders and
accessibility feedback. Start with the [contribution guide](CONTRIBUTING.md) and
[scoped help wanted issues](https://github.com/brianbaldock/omarchy-game-mode/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22).
Small fixes and reproducible reports are welcome; you do not need a handheld to help.
This is an independent preview, not an official Omarchy component.

[Watch the 56-second silent guided tour](https://brianbaldock.github.io/omarchy-game-mode/#preview), or [download the MP4](site/assets/omarchy-game-mode-demo.mp4). It shows the actual GTK UI with simulated session/controller state, not gameplay footage. [Recording details and chapters](media/README.md).

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
On InputPlumber's virtual gamepad only, a button or direction pressed while Guide is
held is ignored too, until it is released: InputPlumber sends vendor buttons as Guide
chords. Game Mode follows that gamepad event by event, focused or not, and reads what
is held only when it connects or after events were dropped. Such a read cannot tell
how a hold began, so a button or direction held then, other than Guide, has to be
released once before it counts. On every other controller Game Mode ignores just
the Guide button itself.
Vendor-specific buttons (for example ROG Ally's dedicated keys) are not mapped.

Controller input is read **read-only and non-exclusively** from evdev. Devices that
report gamepad buttons or a hat are used. Virtual devices are skipped, except the
virtual gamepad that a running InputPlumber announces (see
[InputPlumber](#inputplumber-read-only)). Your user
needs read access to `/dev/input/event*` (usually the `input` group, or a uaccess rule).
If access is denied, Game Mode says so on screen and keeps working with keyboard and
touch.

### ASUS ROG Xbox Ally controller layout

On the ASUS ROG Xbox Ally (RC73YA) with a kernel that lacks ASUS's Ally HID driver, the
generic HID driver reads the built-in controller, `ASUSTeK Computer Inc. N-KEY Device`
(`0b05:1b4c`). Its descriptor declares 14 buttons in XInput order, and that driver gives
them the codes `BTN_SOUTH` (304) to `BTN_THUMBL` (317) by position. From X on, each
button then arrives under the code of another one, and none as the right stick click
`BTN_THUMBR`:

| Button | Arrives as | Xbox controller code |
| --- | --- | --- |
| A, B | `BTN_SOUTH`, `BTN_EAST` | the same |
| X, Y | `BTN_C`, `BTN_NORTH` | `BTN_NORTH`, `BTN_WEST` |
| LB, RB | `BTN_WEST`, `BTN_Z` | `BTN_TL`, `BTN_TR` |
| View, Menu | `BTN_TL`, `BTN_TR` | `BTN_SELECT`, `BTN_START` |
| L3, R3 | `BTN_TL2`, `BTN_TR2` | `BTN_THUMBL`, `BTN_THUMBR` |
| Xbox | `BTN_SELECT` | `BTN_MODE` |
| Three buttons of unknown meaning | `BTN_START`, `BTN_MODE`, `BTN_THUMBL` | none |

The last column holds the codes the kernel's Xbox controller driver reports, which Game
Mode expects. The table was derived from two independent projects, Handheld Daemon's Ally
X button map and InputPlumber's former Ally X fallback map, both for this USB id, and was
then confirmed by a guided press test on one ROG Xbox Ally (RC73YA, Linux 7.2): each of
the eleven buttons from A to Xbox sent the code in the middle column (see
[Limitations](#limitations) for what that test did not cover).

**How Game Mode compensates.** When Game Mode reads this pad itself, without
InputPlumber, it translates every button through the table before acting on it, for its
own menus only: LB/RB switch pages, Menu opens Settings, holding View + Menu shows Game
Mode, the Xbox button counts as Guide and is left alone, and the three unknown buttons
are ignored. Nothing changes on the device or in the kernel: Steam, browsers, Moonlight,
games and AntiMicroX keep reading the middle column. The translation applies only to a
pad that has that name and USB id, the 14 button codes of the middle column but no
`BTN_THUMBR`, and sticks and a D-pad. A pad whose codes were corrected, by the udev entry
below or by a kernel driver, has `BTN_THUMBR` and is read as it is. Game Mode also
notices a correction made while it runs, at the pad's next input, which it drops while it
reads the pad's state again. udev applies the entry's twelve lines one after another, so
a press in the instant that happens can still be misread once; a restart is the
cleanest. InputPlumber's virtual
gamepad is never translated. `omarchy-game-mode status` shows
`"quirk": "xinput-order-hid"` for a translated device under `controllers`, and `null`
for the others. To read every controller exactly as the kernel reports it, quit Game
Mode and start it with `GAMEMODE_NO_BUTTON_QUIRKS=1`, for example
`GAMEMODE_NO_BUTTON_QUIRKS=1 omarchy-game-mode show`.

**System-wide fix (optional, as root).** For every program to get the Xbox controller
codes, a udev hwdb entry can have the kernel report them. Game Mode ships it as
`contrib/udev/61-rog-xbox-ally-gamepad.hwdb`, installed to
`/usr/share/omarchy-game-mode/contrib/udev/` (or under your `PREFIX`), and never copies
it into the system or applies it. It matches only the controller's gamepad interface on
RC73YA and RC73XA (Xbox Ally X) boards, follows the table above and puts the three
unknown buttons on `BTN_TRIGGER_HAPPY1` to `3`, where they cannot pass for a standard
button. To apply it, install the file and update the database, then reboot:

```bash
sudo install -Dm644 /usr/share/omarchy-game-mode/contrib/udev/61-rog-xbox-ally-gamepad.hwdb /etc/udev/hwdb.d/61-rog-xbox-ally-gamepad.hwdb
sudo systemd-hwdb update
```

The entry takes effect when the controller is set up, which for a built-in one means at
the next boot, and every program then starts with the new codes. Applying it to the
running controller instead is an advanced option, safe only with every button released:
the kernel moves the codes one button at a time, and a button held meanwhile can stay
down under its old code. A held Xbox button, for example, stays down as View
(`BTN_SELECT`) until View is pressed and released, so Menu alone looks like View + Menu
to every program. If you take that route, run
`sudo udevadm trigger --action=change --subsystem-match=input` after the two commands,
with no button held, then restart Steam and AntiMicroX, which had the pad open.
InputPlumber, when it runs, reads the corrected codes too; that combination is untested.

To undo, remove the file, update the database and reboot. The kernel keeps the codes it
was given until the pad is created again, so the original codes return after the reboot:

```bash
sudo rm /etc/udev/hwdb.d/61-rog-xbox-ally-gamepad.hwdb
sudo systemd-hwdb update
```

Remove the entry as well once your kernel has ASUS's Ally HID driver.

How far this has been checked:

- **Verified by automated tests:** the fingerprint, and that another vendor, product or
  name, a pad with `BTN_THUMBR`, `GAMEMODE_NO_BUTTON_QUIRKS=1` and InputPlumber's
  virtual gamepad each leave a pad untranslated; every translation, for presses and for
  buttons held when the pad is opened; that the three unknown buttons are dropped; that
  the Xbox button no longer counts as View and View + Menu forms the Home hold; that an
  open pad's codes are judged on the open device before each batch of input, so that a
  correction is noticed before udev reports anything, input read or queued across it is
  dropped rather than misread, also around dropped events, and a node name that already
  belongs to another device changes nothing for the pad still open; that the hwdb entry
  and Game Mode's table agree.
- **Verified live on an ASUS ROG Xbox Ally (RC73YA), without physical input:** the
  fingerprint matches the built-in pad's capabilities as the kernel reports them in
  sysfs, and does not match InputPlumber's virtual gamepad. The hwdb entry compiles with
  `systemd-hwdb` in a scratch root, where this machine's lookup key for the gamepad
  interface returns its 12 entries and the keys of the other N-KEY interfaces return
  none.
- **Verified by a guided press test on that unit (Linux 7.2):** with the pad untouched,
  A, B, X, Y, LB, RB, View, Menu, L3, R3 and the Xbox button each sent the code in the
  table (`BTN_SOUTH`, `BTN_EAST`, `BTN_C`, `BTN_NORTH`, `BTN_WEST`, `BTN_Z`, `BTN_TL`,
  `BTN_TR`, `BTN_TL2`, `BTN_TR2`, `BTN_SELECT`), and the triggers and sticks arrived on
  the usual axes (`ABS_Z`, `ABS_RZ`, `ABS_X`, `ABS_Y`, `ABS_RX`, `ABS_RY`). With the entry
  installed and applied to the running pad, with no button held, the kernel then reported
  the standard layout: `BTN_THUMBR` and the eleven standard buttons, and the three
  buttons of unknown meaning on `BTN_TRIGGER_HAPPY1` to `3`.
- **Pending physical validation:** a press test with the entry applied; presses of the
  three buttons of unknown meaning (none sent anything on the gamepad node in the press
  test); how Steam, SDL games and browsers handle the pad with the entry applied;
  whether applying the entry to a running pad is safe on other units (it was applied
  once, with no button held); anything on the Xbox Ally X. The small Armoury Crate and
  Library buttons are not on this node: the press test saw the Armoury Crate button only
  as a keyboard key on another interface of the same device, which only root can read.

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

### InputPlumber (read-only)

[InputPlumber](https://github.com/ShadowBlip/InputPlumber) is a system daemon for
handhelds. While it runs, it hides the physical controller nodes from your user and
re-emits the controller through virtual devices. Game Mode does not need it, does not
install or start it, and never changes it: it loads no profile, sets no property and
sends no event. It only reads InputPlumber's state over D-Bus, so that

- its own menus are navigated with InputPlumber's virtual gamepad instead of the
  hidden physical one, and
- Settings and `omarchy-game-mode status` (section `inputplumber`) describe what
  InputPlumber manages: device name, profile name, target types, number of sources,
  whether any source is hidraw, and notes such as `evdev-only` or `no-r3`. No serial
  or persistent identifier is read into that report. Its `state` tells an InputPlumber
  that manages no controller (`empty`) from one that could not be read (`failed`) and
  from one that has not answered yet (`pending`). Settings says which of the first two
  it is and shows nothing for the third.

**How the virtual gamepad is recognized.** This is a heuristic, not proof of origin:
InputPlumber does not say which event node belongs to which target. A virtual node is
used only when all of this holds:

- InputPlumber owns its bus name, and one of its composite devices lists a gamepad
  target of a known type (`xb360`, `gamepad`, `xbox-elite`, `xbox-series`);
- the node is a uinput device (`/devices/virtual/input`);
- its name, bus type, vendor and product are the ones InputPlumber gives that type,
  for example `Microsoft X-Box One Elite 2 pad`, `045e:0b00`.

If more such nodes exist than InputPlumber announces, none of them is used. After a
virtual gamepad went away, the next one is used only once InputPlumber's state has
been read again; if that read fails, none is. Other target types (Steam Deck,
DualSense and so on) are reported as unrecognized rather than guessed, and every
other virtual device (AntiMicroX, Steam Input) stays skipped. A physical node that
InputPlumber hides is not reported as a permission problem. If no virtual gamepad can
be used either, the header says the controller is not readable and the tooltip says
why; keyboard and touch keep working.

**Should you run it?** Not for Game Mode's sake, and it is not a mapping upgrade by
itself. On the ASUS ROG Xbox Ally (RC73YA) with a kernel that lacks the Ally HID
driver, InputPlumber 0.82.0 rejects its hidraw sources ("Device is not using the
asus_rog_ally driver") and runs on evdev sources only. In that state:

- the virtual Elite pad does advertise a right stick click (`BTN_THUMBR`), but nothing
  feeds it: the capabilities InputPlumber reports for its sources include no right
  stick button. HHD's and InputPlumber's own former Ally X maps for this USB ID list
  R3 as `BTN_TR2` on the raw pad, a code InputPlumber's stock configuration does not
  turn into a stick click. Whether a physical R3 press arrives anywhere has not been
  checked (see below for what is and is not verified on the RC73YA);
- all of the controller's hidraw nodes become root-only, so tools that read the vendor
  buttons from hidraw stop working;
- going by InputPlumber's own device configuration, stock profile and target code, the
  Armoury button is sent as a Guide + X chord, Library as Guide + A, a long Library
  press as Screenshot, and the back paddles as paddle buttons of the virtual Elite pad.
  A desktop mapper such as AntiMicroX sees the X or A of such a chord and acts on it.

It only becomes worthwhile there once the kernel has the Ally HID driver; a fuller
upstream driver series has been posted for kernels after 7.2. On the virtual gamepad,
Game Mode ignores what is pressed while Guide is held, so these chords select nothing
in Gaming Home.
AntiMicroX set switching (above) is unchanged and remains the only desktop-pointer
route, with or without InputPlumber.

The commands below try InputPlumber and go back; Game Mode runs none of them. On a
board InputPlumber knows, a udev rule of its package starts the daemon when the package
is installed and again at every boot, so disabling the unit does not keep it off: mask
it.

```bash
omarchy pkg add inputplumber
sudo systemctl enable --now inputplumber.service inputplumber-suspend.service

sudo systemctl disable --now inputplumber.service inputplumber-suspend.service
sudo systemctl mask inputplumber.service        # undo with: systemctl unmask
```

Other programs that had the controller open, such as Steam or AntiMicroX, may need a
restart after either change.

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
`~/.config/autostart/io.github.brianbaldock.OmarchyGameMode.desktop`, which runs the installed
launcher by its absolute path with `--autostart`, so Gaming Home appears at login.
`background` starts the app without showing a window. `autostart off` removes only
the autostart file Game Mode created.

`omarchy-game-mode mapper on|off|status` turns AntiMicroX set switching on or off.

`omarchy-game-mode status` reports what Game Mode sees, including the controllers it
reads and, under `inputplumber`, what a running InputPlumber manages. Review it before
posting: window titles can contain private information.

### Upgrading from an earlier preview

Quit the old running version with `omarchy-game-mode quit` before installing this update: its application ID changed to `io.github.brianbaldock.OmarchyGameMode`, so a new instance cannot quit the old one. Reinstall at the same prefix; old launcher/icon filenames are removed. A login entry owned by Game Mode remains recognized and is migrated by `autostart on` or removed by `autostart off`. Unowned legacy entries are left alone. Update any custom window rules or favorites that refer to the old ID.

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
- Virtual controllers are skipped, except the gamepad a running InputPlumber announces. HHD configurations may hide or exclusively own the physical controller, leaving Game Mode without native navigation; compatibility with HHD is unverified. Do not disable hardware protections to work around it.
- InputPlumber support is read-only recognition, and how far it has been checked differs by level:
  - **Verified by automated tests:** the D-Bus client against a fake InputPlumber on a
    private bus (owner changes, renumbered devices, targets removed and added again,
    signal floods, late replies, and proof that nothing but reads is ever sent);
    recognition and rejection of virtual gamepads against a mocked sysfs; that buttons
    and directions pressed while Guide is held do nothing on the virtual gamepad and
    still act on every other controller. The Settings text was also rendered headless
    with GTK Broadway in English and Arabic, which is a manual smoke script.
  - **Verified live on an ASUS ROG Xbox Ally (RC73YA), without physical input:**
    InputPlumber 0.82.0 active; Game Mode's client and controller reader run against
    it read-only: its state read, the virtual `Microsoft X-Box One Elite 2 pad`
    (`045e:0b00`) recognized and opened, the hidden physical node not reported as an
    error. That pad's key capabilities include `BTN_THUMBR`, while the source
    capabilities of InputPlumber's composite device list no right stick button. The
    controller's hidraw nodes were root-only. No button was pressed.
  - **Pending physical validation, and known limits:** presses of the Xbox, View, Menu,
    Library and Armoury buttons; Guide behavior in Steam; suspend and resume; a real
    game session. On the raw pad the Xbox button has been seen as `BTN_SELECT` (314).
    HHD's Ally X button map and InputPlumber's former Ally X fallback map, both for the
    same USB id (`0b05:1b4c`), describe the pad's buttons as arriving in XInput order
    but with the kernel's generic codes: X as `BTN_C`, Y as `BTN_NORTH`, LB as
    `BTN_WEST`, RB as `BTN_Z`, View as `BTN_TL`, Menu as `BTN_TR`, L3 as `BTN_TL2` and
    R3 as `BTN_TR2`. Only HHD's map also lists the Xbox button, as `BTN_SELECT`, which
    fits the reading above. A guided press test on an RC73YA has since confirmed the
    table: each of the eleven buttons from A to Xbox sent the code listed. What no test
    has covered there is the View + Menu chord in Game Mode on that pad. Game Mode's
    translation of the raw pad and the optional udev entry rest on that table; see
    [ASUS ROG Xbox Ally controller layout](#asus-rog-xbox-ally-controller-layout).
- Recognizing InputPlumber's virtual gamepad stays a heuristic, because InputPlumber does
  not say which event node is its own. If another program creates a virtual pad with the
  same name and IDs while InputPlumber announces one that does not exist, Game Mode
  reads that pad instead.
- Game recognition is a heuristic. A windowed game that wasn't started by a supported
  launcher counts as desktop until you choose **Game input** for it.
- Nothing in the automated tests can show that the physical Guide button still reaches
  Steam. The tests only check that Game Mode ignores Guide; confirm the rest by hand on
  your device.
- CI and CodeQL run on GitHub; passing them does not establish hardware compatibility.

## Credits and provenance

All code, CSS and SVG icons in this repository were written for this project and are
MIT licensed. Nothing was copied from other projects, icon sets (such as Font Awesome
or Material) or fonts, and no font files are bundled. The UI uses your system font
through GTK. The only material taken from another project is a set of D-Bus interface
descriptions used as test fixtures, noted below.

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
- The D-Bus interface of [InputPlumber](https://github.com/ShadowBlip/InputPlumber),
  read-only and only when it is running. The interface descriptions in
  `tests/fixtures/inputplumber` were captured from a running InputPlumber 0.82.0
  (GPL-3.0) with `busctl introspect`, and their documentation comments were removed.
  They are used only by tests; none of its code, profiles or schemas are included.

Translations come from `data/locales` and were drafted for this project.

## Related projects

These are references and inspiration only. No code is copied from them, and Game Mode
does not integrate with them. The one exception is reading InputPlumber's state, as
described under [InputPlumber](#inputplumber-read-only):

- [OpenGamepadUI](https://github.com/ShadowBlip/OpenGamepadUI): Godot controller-native
  launcher and overlay.
- [Handheld Daemon (hhd)](https://github.com/hhd-dev/hhd): handheld controller,
  gyro and TDP support. Not yet validated together: Game Mode skips HHD's virtual controllers, so some HHD configurations may not provide navigation to this app.
- [InputPlumber](https://github.com/ShadowBlip/InputPlumber): input routing daemon
  that turns handheld controls into virtual controllers. Game Mode reads it, never
  changes it.
- [omarchy-rog-ally-codex-plugin](https://github.com/richelle-belle/omarchy-rog-ally-codex-plugin):
  ROG Ally setup automation.
- [omarchy-launcher](https://github.com/headband42/omarchy-launcher): Quickshell
  launcher for Omarchy.

## License

MIT. See [LICENSE](LICENSE).
