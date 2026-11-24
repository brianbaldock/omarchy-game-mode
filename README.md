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

<!-- release:line:start -->
**Latest release: [0.2.1](https://github.com/brianbaldock/omarchy-game-mode/releases/tag/v0.2.1)** (2026-11-24) · [What changed](CHANGELOG.md) · [Install it](#install)
<!-- release:line:end -->

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
- **Clock and battery**: the header shows the time and, on a machine with a battery, its
  charge, so you can see both while the desktop bar is covered by Gaming Home. See
  [Clock and battery](#clock-and-battery).
- **Bluetooth**: a header button shows whether Bluetooth is off, on or connected, and its
  sheet turns Bluetooth on or off, connects the devices this computer remembers and adds
  new ones, with the controller, a mouse or touch. See [Bluetooth](#bluetooth).
- **Theme**: uses your current Omarchy theme colors when they can be read, with a dark
  fallback. There are no background images. Nothing redraws by itself while Gaming Home is
  hidden. While it is shown and at rest, only the clock (once a minute), the battery
  (when its level or state changes) and Bluetooth (when a device or Bluetooth itself
  changes) change by themselves. The launcher itself makes no
  network requests. Only the `doctor` command, when you run it, sends the diagnostic
  probes listed under [Doctor](#doctor).

## Clock and battery

The header of Gaming Home shows the time and the battery, because the desktop bar is
hidden behind it. Both are for looking at: neither can be selected or clicked, and neither
is part of controller navigation. They are drawn in Gaming Home only, not over a running
game.

- **Clock.** Hour and minute, no seconds. It counts hours the way the Omarchy bar does,
  and Game Mode has no setting for it. The bar's clock is an entry of Omarchy's
  `shell.json` (`{"id": "omarchy.clock", "format": "dddd HH:mm", ...}`, wherever the file
  has it), and its `format` decides: `HH:mm` and `H:mm` count to 24, `h:mm AP` and
  `hh:mm ap` to 12. Text in single quotes is not read as a format, and the bar's
  `formatAlt` is not looked at.
- **Which file.** Game Mode reads the user's `~/.config/omarchy/shell.json` first (under
  `$XDG_CONFIG_HOME` when that is set), then Omarchy's packaged default,
  `$OMARCHY_PATH/config/omarchy/shell.json` (`/usr/share/omarchy` when `OMARCHY_PATH` is
  not set). A file that is missing, cannot be read, is not what it should be or has no
  clock format with an hour in it is passed over, and when neither file says, the system's
  locale decides: `en_US` has a 12-hour clock (`9:41 PM`), `de_DE` a 24-hour one
  (`21:41`). The files are only read, each time Gaming Home is shown, and nothing watches
  them: a change of the bar's format shows the next time Gaming Home is shown.
- **Date.** The clock's tooltip gives the weekday and the date as the system locale writes
  them (`Monday, 11/23/2026` in `en_US`, `Montag, 23.11.2026` in `de_DE`). Those words and
  their order come from the system locale, not from the language chosen in Settings.
- **Battery.** An icon filled to the charge, and the percentage (`78%`). A bolt and a
  green icon mean it is charging, a green icon alone that it is full. While the battery is
  in use the chip turns amber at 20 % or less and red at 10 % or less. The tooltip names
  the state and, when it is known, the time left, for example "Charging · 1 h 05 min until
  full" or "On battery · 7 h 49 min left".
- **No battery, no chip.** A desktop, a virtual machine or a machine whose battery cannot
  be read shows no battery at all, and no message either.
- **Where it comes from.** UPower's display device, over the system bus: the one object
  that adds up the batteries powering the machine. Game Mode only watches for UPower's bus
  name and reads from it, and never starts it. UPower saying that no battery is present is
  believed. When a UPS is plugged in, UPower makes the UPS its display device and has
  nothing to say about the laptop's own battery, so that case is read from sysfs too.
  Without UPower on the bus, it reads `/sys/class/power_supply` instead, looking at
  entries of type `Battery` whose scope is not `Device`. A battery the kernel marks as a
  device's own (scope `Device`), such as a controller's, a mouse's or a headset's, is left
  out. sysfs has no time estimate to offer for the tooltip.
- **When it runs.** Only while Gaming Home is shown. One timer fires when the minute
  changes. It is set again from the wall clock each time, so a suspend, a change of time
  zone or a clock adjustment corrects itself within a minute. With UPower, the battery is
  read when Gaming Home is shown and again when UPower announces a change, with no timer
  of its own. If UPower is on the bus but does not answer, or its display device is a UPS,
  the battery comes from sysfs and UPower is asked again on the minute tick. Without
  UPower, sysfs is read when Gaming Home is shown and then on that tick, because sysfs
  cannot say when it changes. While Gaming Home is hidden, the clock and the battery hold
  no timer, no bus subscription and no call in flight.

How far this has been checked:

- **Automated tests:** how the bar's Qt clock formats count hours (with and without AM/PM,
  quoted text, `formatAlt` ignored), the clock entry wherever it sits in `shell.json`,
  files that are missing, unreadable, garbled, too large or not files at all, and the
  order of the user's file, Omarchy's default and the locale; the 12-hour and 24-hour
  decision from locale formats, noon and midnight, and the minute-boundary delay
  (including a change of offset); UPower's state codes, the percentage, the 20 % and
  10 % edges, no battery, a UPS (the battery is then read from sysfs) and garbled
  properties; sysfs layouts like the ROG Xbox Ally's, with a controller, USB ports and an
  empty bay ignored, several batteries and unreadable files; the UPower client against a
  fake on a private bus, including that it sends nothing but reads, that watching for
  UPower never starts it (on a bus where it could be started) and that nothing is
  subscribed or asked while it is stopped; the timer's life across showing, hiding,
  rebuilding the widgets and a late timer after a suspend. Every string exists in the
  seven languages.
- **Checked on an ASUS ROG Xbox Ally (RC73YA), read-only; nothing was pressed or
  unplugged:** UPower 1.91.5 reported its display device and Game Mode's client read it.
  The sysfs entries are an `ACAD` adapter, `BAT1` and two USB ports, and only `BAT1` is
  taken for a battery. Omarchy's packaged default `shell.json` there has the bar's clock
  at `dddd HH:mm`, and with it the header showed `21:41` although the locale of the
  device counts to 12. The header was rendered with GTK Broadway in English and Arabic, in
  each of the states discharging, charging, low, critical, full and no battery. In a real
  window the timer, the UPower watch and its subscription existed while it was shown and
  were gone once it was hidden, and an update that changed nothing visible drew no frame.
  With UPower's name made absent on the real bus, sysfs was the only source: the chip
  showed the real battery, sysfs was read again on the minute tick and not while hidden.
- **Not tested on hardware:** plugging in or unplugging the charger, a battery that really
  runs down to 20 % and 10 %, UPower's reports during a charge, suspend and resume or a
  change of time zone while Gaming Home is shown, a bar clock set to a 12-hour format in
  the user's own `shell.json` (only the packaged default was read on the device, the
  user's file only in tests), a non-English system locale on the device (only `en_US` is
  installed there), a machine with a UPS next to the battery, a machine that really has
  no UPower (its absence was only simulated), and how Hyprland treats a Gaming Home that
  stays mapped behind another window.

## Bluetooth

The header has a Bluetooth button between the clock and Settings, so that a controller,
headphones or a keyboard can be connected without leaving Gaming Home. The button is there
only when the computer has a Bluetooth adapter, or when Bluetooth is there but could not be
read. Like the rest of Home, it is selected with the D-pad, a click or a tap. In a window
narrower than 1400 pixels, such as a handheld's 1280x720 screen, the header shows the
logo without the app's name (the logo's tooltip has it), so that the clock, the battery
and the buttons keep their room.

- **Button.** Its icon shows Bluetooth off (struck through and dimmed), on, or connected to
  at least one device (with two dots, in the accent color). The warning color means that
  Bluetooth could not be read. The tooltip says which, and names the connected devices.
- **Sheet.** A, Enter, a click or a tap on the button opens it. From the top: a line when
  something failed, with Dismiss; the Bluetooth switch; **My devices**, the devices this
  computer remembers, each with Connect or Disconnect and Forget; **Add a device**, with
  Search. A device keeps its place in the list when it connects. A device without a name
  is listed as "Unnamed device" and devices with the same name are numbered: an address is
  never shown. While Bluetooth is busy with something, such as connecting a device, the
  buttons that would have to wait for it are dimmed.
- **Switch.** It turns Bluetooth on or off for the whole computer, not only for Gaming
  Home, and the choice stays after a restart.
- **Search and Pair.** Put the device in pairing mode, then press Search: the devices found
  nearby are listed with Pair, and the search ends by itself after 45 seconds or with Stop
  searching. Pair adds the device: it pairs with it, lets it reconnect by itself from then
  on and connects to it. When the device asks for something, the sheet shows it in place
  of the lists: a six-digit number to compare (Yes, it matches, or No), a code to type on
  the device, or a PIN or passkey to enter on a digit pad. A device that was added
  reconnects by itself whenever it is on and in range; Forget, which asks first, removes
  it and stops that.
- **Controller, keyboard, mouse and touch.** The D-pad moves between the controls and A
  presses one. B and Escape take back the Forget question or give up the pairing under
  way, and otherwise close the sheet; Menu closes it at once. Closing the sheet, however it
  happens, ends the search and gives up a pairing. On the digit pad the D-pad moves between
  the keys; a keyboard's digits, Backspace and Enter work too. The pad and the numbers read
  left to right in every language.
- **When it runs.** Only while Gaming Home is shown. Hiding Gaming Home closes the sheet,
  ends the search, gives up a pairing under way and stops looking at Bluetooth until
  Gaming Home is shown again. The sheet holds no timer of its own and nothing in it moves
  while nothing changes: "Searching…" is a static line.
- **Not supported yet:** PINs with letters (the pad has digits only), devices that announce
  no name (they are left out of the nearby list), and pairing that starts on the other
  device: Gaming Home answers only the device it is adding.

How far this has been checked:

- **Automated tests:** what the button and the sheet show for Bluetooth off, on, connected,
  unreadable and gone; the digit pad (length, leading zeros, Erase, Done); the words of
  every failure in the seven languages; where the focus goes after a change; and, against a
  scripted Bluetooth stand-in, that B, Close, Menu, another sheet, rebuilding the window and
  hiding it each end the search and give up the pairing. The sheet was rendered headless
  with GTK Broadway in English, German, Japanese and Arabic, at 1280x720, 1920x1080 and
  1280x600, by a script that fails when the sheet does the wrong thing.
- **Not yet tested on real devices:** none of this has run against real Bluetooth hardware
  yet: connecting, pairing with each kind of question, the switch, a device that goes away,
  and how the D-pad and touch feel on a handheld.

## Controls

| Input | Action |
| --- | --- |
| A | Select |
| B | Back, or go to the Desktop from Home |
| D-pad / left stick | Move selection (repeats only while held) |
| LB / RB | Switch Home / Discover |
| Menu (Start) | Settings, or close Settings or the Bluetooth sheet |
| B in the Bluetooth sheet | Take back the Forget question or give up the pairing, then close the sheet |
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

**The small buttons.** The Armoury Crate (small, top left) and Library (small, top
right) buttons are not part of the gamepad. A press test on one RC73YA saw the Armoury
Crate button as `KEY_F16`, a back paddle as `KEY_F15` and the Library button as
`KEY_F22`, on two keyboard interfaces of the same USB device. Only root can read those
nodes, so Game Mode does not see them, but your compositor has them open and can bind
them. On Omarchy with Hyprland, bind by key code, which is the evdev code plus 8
(`KEY_F22` is 192, so 200), in `~/.config/hypr/bindings.lua`:

```lua
o.bind("code:200", "Gaming Home (Library button)", "omarchy-game-mode show")
```

That opens Gaming Home over whatever has the focus, a game included. Omarchy already
binds `XF86TouchpadOn`, the name the US layout gives that key; the Ally has no touchpad,
so its handler finds nothing to enable and does nothing.

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
  buttons of unknown meaning on `BTN_TRIGGER_HAPPY1` to `3`. A second press test, with
  the entry in place, sent A, B, X, Y, LB, RB, View, Menu, L3, R3 and the Xbox button as
  `BTN_SOUTH`, `BTN_EAST`, `BTN_NORTH`, `BTN_WEST`, `BTN_TL`, `BTN_TR`, `BTN_SELECT`,
  `BTN_START`, `BTN_THUMBL`, `BTN_THUMBR` and `BTN_MODE`, and the triggers and sticks on
  the same axes as before.
- **Pending physical validation:** presses of the three buttons of unknown meaning (none
  sent anything on the gamepad node in the press tests); how Steam, SDL games and
  browsers handle the pad with the entry applied; that the entry is applied again at
  the next boot; whether applying the entry to a running pad is safe on other units (it
  was applied once, with no button held); a physical press of the Library button
  against the binding shown under "The small buttons" (it was tested with a virtual key
  only); anything on the Xbox Ally X.

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

## Touch keyboard

Install the optional Wayland keyboard from the AUR:

```bash
omarchy pkg aur add wvkbd
```

The package provides `wvkbd-mobintl` (with `wvkbd` accepted as a backend as well).
Game Mode accepts only that trusted executable from `/usr/bin`, `/usr/local/bin`
or `/bin` (after resolving symlinks). It verifies the running process's executable
and ownership, binds it to the current Wayland/Hyprland session, checks wvkbd's age
and every thread's signal mask, and then uses a pidfd to signal it. These checks
are a best-effort safety net, not an acknowledgement: wvkbd has no confirmation
API. If multiple valid instances exist, Game Mode refuses to choose one.

It does not grab devices or inject input: wvkbd itself uses Wayland's
virtual-keyboard protocol.

```bash
omarchy-game-mode keyboard toggle
```

`show`, `hide` and `status` are also available. With Hyprland, Game Mode queries the
exact `wvkbd` layer-shell namespace, so toggle can choose show or hide directly and
already-correct show/hide requests do nothing. Without Hyprland, visibility is
`unknown` and toggle is best-effort. If nothing is running, `show` and `toggle`
start the keyboard visible; `hide` is a successful no-op. `hyprctl` and
the keyboard executable use the same trusted-path rule; an unavailable or untrusted
`hyprctl` makes visibility unknown and uses a 720p sizing fallback. Direct CLI
actions are serialized so rapid hotkey invocations do not race one another.

The wvkbd signal mapping was verified against the installed wvkbd 0.21 manual and
upstream source: `SIGUSR1` hides, `SIGUSR2` shows and `SIGRTMIN` toggles.
Real-hardware touch behaviour is not verified by automated tests.

An optional `keyboardArgs` list in
`~/.config/omarchy-game-mode/settings.json` can append only these wvkbd 0.21
arguments:

- `-H` / `-L`: decimal height, 160-600.
- `--fn`: `"<family> <size>"`, with an ASCII family and size 12-32.
- `--no-popup`, `--no-highlight`, `--no-feedback`, `--non-exclusive`.

Everything else is ignored with a reason in `keyboard status`; Game Mode always
chooses the executable. In particular, `--hidden`, `--auto` and output/debug flags
are not accepted. `keyboardCommand` from older experiments is preserved but never
executed. With no `keyboardArgs`, Game Mode supplies only monitor-based `-H`/`-L`
sizes.

In a custom Omarchy Hyprland Lua bindings file, F13-F24 are unbound hardware-key
candidates by default (unlike `SUPER + K`, which opens keybindings):

```lua
o.bind("F13", "Toggle touch keyboard", "omarchy-game-mode keyboard toggle")
```

Hardware-button routing is configured separately. Game Mode exposes
`omarchy-game-mode keyboard toggle` for any launcher, compositor binding or input
daemon to call; Game Mode itself does not claim or remap the ROG Ally
Keyboard/Armoury buttons.

### Known limits

- Aliases or symlinks within trusted, root-owned system binary directories are
  accepted; defending against a compromised system is outside this threat model.
- Concurrent individual pin add/remove operations are not merged; only the
  resident app edits pins, while the settings lock preserves unrelated keys.
- Equivalent but textually different UWSM session environment values are refused;
  Game Mode intentionally does not canonicalize them.

## Install

From a release (Arch and Omarchy):

<!-- release:install:start -->
Download `omarchy-game-mode-0.2.1-1-any.pkg.tar.zst` and `SHA256SUMS` from the
[0.2.1 release](https://github.com/brianbaldock/omarchy-game-mode/releases/tag/v0.2.1), then:

```bash
sha256sum -c --ignore-missing SHA256SUMS
sudo pacman -U omarchy-game-mode-0.2.1-1-any.pkg.tar.zst
omarchy-game-mode quit      # so the new version starts
```

See the [changelog](CHANGELOG.md) for what changed in each version.
<!-- release:install:end -->

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

`omarchy-game-mode doctor` is described in the next section. It always answers in the
terminal you ran it from, even while Game Mode is running.

### Upgrading from an earlier preview

Quit the old running version with `omarchy-game-mode quit` before installing this update: its application ID changed to `io.github.brianbaldock.OmarchyGameMode`, so a new instance cannot quit the old one. Reinstall at the same prefix; old launcher/icon filenames are removed. A login entry owned by Game Mode remains recognized and is migrated by `autostart on` or removed by `autostart off`. Unowned legacy entries are left alone. Update any custom window rules or favorites that refer to the old ID.

## Doctor

`omarchy-game-mode doctor` checks what Xbox Remote Play, Xbox Cloud Gaming and your
controller need on this device. Every result says what it proves and what it does not.

```
omarchy-game-mode doctor                  Everything below
omarchy-game-mode doctor streaming        Network, Wi-Fi, firewall, clients, video decode
omarchy-game-mode doctor input            Controllers and input daemons
omarchy-game-mode doctor system           Device, system and GPU facts only
omarchy-game-mode doctor --console-ip 192.168.1.50   Also probe your console's address
omarchy-game-mode doctor --markdown       Redacted report to paste into an issue
omarchy-game-mode doctor --json           The same report for tools
omarchy-game-mode doctor --timeout 3      No check gets more than 3 seconds
omarchy-game-mode doctor buttons          Guided button test (see below)
```

What it checks:

- **Network path:** default route, IPv6 consistency, VPN or proxy in the way, which DNS
  resolver answers, the Microsoft and Xbox service names, TLS to them, the clock, MTU.
- **Streaming transport:** UDP to Microsoft's relay and two public STUN servers, the
  relay's TURN challenge, and how your router maps one connection to several servers.
- **Wi-Fi:** band, channel width, radar-shared (DFS) spectrum, signal, power saving,
  roams and disconnects in the last 10 minutes, loss between the device and the router.
- **Firewall:** which firewall is active and, for ufw, what its saved policy and rules
  would do to outbound UDP, for IPv4 and IPv6 separately. Saved rules are not the live
  ones, which only root can read.
- **Clients and video:** browsers, Greenlight, browser flag files, hardware H.264 decode.
- **Controller:** which control codes each gamepad lists (read without a handheld daemon,
  the ROG Xbox Ally's built-in pad lists generically numbered buttons and no standard
  right-stick click), InputPlumber, Handheld Daemon and AntiMicroX, and controller
  interfaces that another daemon has hidden. On the ROG Xbox Ally boards a pad that does
  not list the standard controls is pointed to the udev entry Game Mode ships (see
  [ASUS ROG Xbox Ally controller layout](#asus-rog-xbox-ally-controller-layout)), not to
  InputPlumber, which is not a mapping upgrade there; a pad that already lists them is
  reported as fine.
- **Vibration:** whether the kernel offers games rumble (`FF_RUMBLE`) on each controller
  and, for a physical controller, whether its HID report descriptor declares a haptics
  output report. On Linux 7.2 the ROG Xbox Ally's built-in pad declares one (report 0x0D,
  laid out like the Xbox One controller's) but the kernel offers no force feedback for
  it, so games cannot make it vibrate through evdev. The doctor verifies these two facts,
  both read from sysfs, and not that any program can make the controller vibrate. A
  direct test on one RC73YA, writing to that report outside the doctor, made the left
  and right grip motors vibrate through the third and fourth magnitudes; the first two,
  which the Xbox One layout gives to the triggers, produced nothing.

What it never does:

- **No root.** It never runs `sudo`. Where only root can answer, it prints the command
  for you to run and marks the result "cannot verify without root".
- **No changes.** Nothing is written (not even Python bytecode caches), installed,
  enabled, started or stopped. No controller is grabbed and no input is injected. Advice
  is text you can act on or ignore.
- **No account access.** It does not sign in and never reads tokens, cookies, browser
  profiles or keyrings. Greenlight is never started.
- **No discovery.** Nothing is broadcast and no network is scanned. Your console is
  contacted only when you pass `--console-ip` with its private address, and then with an
  anonymous STUN request on UDP 9002 and one connection attempt on TCP 3074.

What it sends, only while you run it: DNS lookups for the names below, and TLS handshakes
(no request, no credentials) with `login.live.com`, `user.auth.xboxlive.com`,
`xsts.auth.xboxlive.com`, `www.xbox.com`, `xhome.gssv-play-prod.xboxlive.com` and
`gssv-play-prod.xboxlive.com`; anonymous STUN to `relay.communication.microsoft.com`,
`stun.cloudflare.com` and `stun.l.google.com`; one unauthenticated TURN request to
Microsoft's relay (its answer, usually a redirect to a regional relay, is reported; the
regional relay is never contacted); one time query to `time.cloudflare.com`; and a few
pings to your router and to `stun.cloudflare.com`. Names are looked up as absolute names,
so no search domain is ever appended, and only probed at public unicast addresses, whatever
the resolver answers. The tools it runs get no display, D-Bus address, proxy or resolver
setting from your environment, and hardware video decode is read from the local GPU render
node (`vainfo --display drm`). With `--console-ip`, the console probes described above go
to that one address.

Reading the result:

```
Streaming transport (UDP)
  [PASS] UDP and STUN: All 3 STUN servers answered (Microsoft relay 80 ms, Cloudflare 4 ms,
         Google 10 ms); public address <public-ip-1>.
         proves: Outbound UDP works and replies come back. Not: That the ports a real stream
                 negotiates are open, or that the console is reachable.
...
Not verifiable from this device
  [SKIP] Live firewall rules: cannot verify without root — run: sudo ufw status numbered verbose

Result: INCONCLUSIVE: no failures in what was tested, 2 warnings. Not verified: Live firewall
        rules, Console settings and account, Live failed attempt.
```

`[PASS]`, `[WARN]`, `[FAIL]`, `[SKIP]` and `[INFO]` mark each check. A streaming report
ends with **INCONCLUSIVE** even when every check passes, because three things cannot be
seen from this device: the live firewall rules, the console's own settings, and what
happens during a real connection attempt. A clean report rules causes out; it is not a
promise that a stream connects. The exit status is 0 when no check failed, 1 when at
least one failed and 2 for a usage error. Ctrl-C, SIGTERM and SIGHUP stop it with 128 plus
the signal number (130 for Ctrl-C), and only after every tool it started has been ended.

Microsoft's supported-browser list for `xbox.com/play` and `xbox.com/remoteplay` does
not include desktop Linux, so the doctor always warns that the web client can fail here
whatever the network looks like. Greenlight is the client to compare against.

**Privacy.** Reports are redacted before they are printed. Public IP addresses become
tokens such as `<public-ip-1>` (the same address gets the same token within one report),
private addresses keep only their first two numbers (`192.168.x.x`), and the Wi-Fi network
name, access-point and device MAC addresses, host name, user name, home directory, DNS
search domains and serial numbers are never included. A controller or key device is
named only when its name is a model name the doctor knows (such as `Microsoft X-Box One
Elite 2 pad` or `ASUSTeK Computer Inc. N-KEY Device`); any other name was chosen by the
firmware or by whoever paired it, and is shown as `<controller>` or `<device>`. Network
interfaces keep generic names such as `wlp3s0`, `wg0` or `tailscale0`; a name someone chose
(`wg-office`) becomes an alias such as `vpn-1`, the same within one report. Values
are scrubbed wherever they appear, not only in known fields: serial-like tokens and
joined hex groups (`AB12-CD34-EF56`) show up as `<id>`, MAC addresses as `<mac>`, and a
private path as `<home-path>`. `--no-redact` shows IP addresses in full for your own
debugging; it cannot be combined with `--markdown`.

**Sharing a report.** Run `omarchy-game-mode doctor --markdown`, read it once, and paste
it into the "Optional redacted evidence" field of the
[hardware-validation issue form](https://github.com/brianbaldock/omarchy-game-mode/issues/new?template=hardware-validation.yml).

The doctor's output is English only by design, so reports read the same for everyone who
helps with them; it adds no strings to the translated interface.

### Hardware button test

`omarchy-game-mode doctor buttons` asks you to press each control once and shows the code
that really arrives for it: the Xbox button, View, Menu, A, B, X, Y, the bumpers and
triggers, both stick clicks, the D-pad, both sticks, the back paddles, the Armoury Crate
button (small, top left) and the Library button (small, top right). Run it in a terminal;
Enter skips a control and `q` stops the test.

```
omarchy-game-mode doctor buttons                        Test the controller games see
omarchy-game-mode doctor buttons --markdown > buttons.md   Prompts stay on the terminal
omarchy-game-mode doctor buttons --raw                  Also watch the raw HID report
omarchy-game-mode doctor buttons --device /dev/input/event21   Read only this device
```

It is read-only. Controllers are read non-exclusively from evdev and never grabbed;
nothing is remapped and no input is sent. Every press, the Xbox (Guide) button
included, therefore still reaches Steam, your games and other apps while the test runs,
so leave nothing open that a stray press could affect. It tests the controller that
games see: the virtual controller of a handheld daemon such as InputPlumber when one is
running, otherwise the built-in one. Devices it is not allowed to read are listed with
the reason.

On the ROG Xbox Ally the small buttons (Armoury Crate, top left; Library, top right) and
the back paddles arrive as keyboard keys on a node only root can read, so without a
handheld daemon the test may see nothing for them. On one RC73YA a press test run as
root saw the Armoury Crate button as `KEY_F16` and a back paddle as `KEY_F15` on one
interface, and the Library button as `KEY_F22` on another. The test marks them not
tested instead of failed and prints the command that shows them, `sudo evtest` with
each such node it found (for example `sudo evtest /dev/input/event5`); it never runs it.

Nothing about your controller is assumed. At the end the test lists where it differs
from an Xbox-style pad on Linux, for example an Xbox button that sends the View code (so
Steam does not open its menu), a right-stick click that never arrives, paddles that
arrive as keyboard keys, or two buttons that send the same code. It also prints an SDL
mapping line built only from the presses it saw and names the controls it left out.

`--raw` additionally reads the controller's raw HID report, when your user is allowed
to, and tells you when a press changes bits the controller never declared. That is how a
button that exists in the hardware but is invisible to the kernel shows up. Only a raw
interface of the same physical device as the controller under test (the same USB device
in sysfs) is ever read, never another controller's. When there is no such interface, or
the controller under test is a daemon's virtual one, the test says `raw report
unavailable` and why, and goes on without it; with `--device` it does not start.

To share a result, run it with `--markdown` and paste the file into the
hardware-validation issue form. It holds the device model, kernel and input-stack
versions and one row per control (pass, fail or not tested). Serial numbers and device
addresses are never read.

## Xbox Remote Play: before you troubleshoot

Remote Play streams from your own console, so most of what decides whether it connects
is set on the console. Check these there first; none of them can be seen from the handheld.

1. **Update and restart** the console, and make sure nobody else is in a remote session.
2. **Remote features:** press the Xbox button, then go to *Profile & system > Settings >
   Devices & connections > Remote features*. Tick **Enable remote features**. Under
   **Power options** choose **Sleep**; a console set to *Shutdown (energy saving)* cannot
   be woken remotely.
3. **Same profile:** sign in to Greenlight with the Xbox profile that is signed in on the
   console. If *Remote features* shows **Xbox app preferences**, allow connections there.
4. **Test remote play:** on the *Remote features* page, run **Test remote play** and note
   what it reports. Away from home, Microsoft asks for at least 4.75 Mbps upload (9 Mbps
   recommended) and less than 150 ms latency.
5. **Network:** in *Profile & system > Settings > General > Network settings* run **Test
   network connection** and **Test NAT type**. *Open* or *Moderate* is fine.
6. **If the NAT type is Strict:** turn on UPnP on your router, or forward UDP port 9002
   to the console (never to the handheld), and remove a second router in between if there
   is one. Then test again.
7. **Same network:** for a first test put the console and the handheld on the same
   trusted network, not a guest network, and wire the console if you can. The console's
   address is under *Network settings > Advanced settings*; pass it to
   `omarchy-game-mode doctor --console-ip` to check that the handheld can reach it.

None of this applies to Xbox Cloud Gaming, which runs on Microsoft's servers: there the
things to check are the account, the region and the subscription.

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
- The D-Bus interface of [UPower](https://upower.freedesktop.org), read-only, only while
  Gaming Home is shown and only when UPower is already running; `/sys/class/power_supply`
  is the fallback. UPower (GPL-2.0-or-later) was read to learn what its display device
  reports. Nothing of it is copied: the fake UPower in `tests/fakes` was written by hand
  from the names and types of a few properties of its public D-Bus interface.

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
