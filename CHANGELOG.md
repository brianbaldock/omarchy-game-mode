# Changelog

All notable changes to Omarchy Game Mode are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); until 1.0, a minor version can change behavior.

Every pull request that changes what people see or do adds a line under **Unreleased**. The release
command turns that section into a version and brings the version strings, the website and the README into
step; see [Changelog and releases](CONTRIBUTING.md#changelog-and-releases).

## [Unreleased]

### Added

- **Clock and battery in the Gaming Home header** (#18). The clock counts its hours the way the Omarchy bar does
  (12 or 24 hour), and the battery shows its percentage, whether it is charging and, when UPower knows, the time
  left. It turns amber at 20 % and red at 10 % while discharging, and is hidden on a machine without a battery.
  Both update only while Gaming Home is shown, so a hidden Gaming Home still wakes nothing. Battery data comes
  from UPower, with `/sys/class/power_supply` as the fallback; peripheral batteries are not shown.
- **Bluetooth in the Gaming Home header** (#22). A button opens a sheet to switch Bluetooth on or off for the whole
  computer, connect, disconnect or forget remembered devices, and add new ones: search, then pair by comparing a
  number, typing a code on the device, or entering a PIN or passkey on an on-screen number pad. It works with the
  D-pad, mouse and touch. A search or a pairing starts only when you ask and ends when you leave the sheet, and a
  hidden Gaming Home still runs nothing. Below 1400 px the header shows the logo without the title to make room.
  Not yet tested with real devices.
- **`omarchy-game-mode power`** (#23). `power status` prints the power profile and the ones the machine offers,
  `power set power-saver|balanced|performance` changes it, and `power cycle` steps to the next one, for example from
  a key bind. It asks power-profiles-daemon, which applies the profile before it answers, never starts the daemon
  and never asks for a password; a session the system does not allow is refused. It also tells Omarchy to keep the
  choice for the current power source.
- **Power profile in the Gaming Home header** (#24). A gauge button opens a sheet to switch between the power
  profiles the machine can really apply (power saver, balanced, performance), with the D-pad, mouse or touch. A
  switch happens at once. Notes say why a switch failed, that another program changed the profile or is asking for
  one, and that the firmware or the processor does not fit the active profile. It never asks for a password, and
  nothing in it runs while Gaming Home is hidden. The README now says what the profiles measured on a ROG Xbox
  Ally. Not yet tested on the device.
- **Controller mapping in Settings.** Open the guided button test, edit a layout in AntiMicroX, or
  measure the layout on a supported Xbox Ally with the optional local helper. Nothing is remapped
  automatically (#25).

### Fixed

- A click that reaches a header button late no longer opens a sheet on a hidden Gaming Home or replaces Settings
  (#24). GTK clicks a button about 250 ms after A or Enter, so pressing B or Menu right after A could leave the
  Settings, Bluetooth or power sheet open on the hidden window, or open it over Settings. Library cards and Discover
  tiles are not covered yet.

## [0.2.1] - 2026-11-24

### Fixed

- The doctor no longer tells a ROG Xbox Ally owner to install InputPlumber, or warns that apps get the raw
  controller, when the udev entry already gives the pad the standard button codes (#16). On the Xbox Ally boards
  a pad that is still not standard is pointed to the udev entry; other handhelds keep the InputPlumber advice.

## [0.2.0] - 2026-11-24

### Added

- **ROG Xbox Ally controller** (#12). On kernels without ASUS's Ally HID driver the built-in pad's buttons arrive
  under the wrong codes. Game Mode now translates them for its own menus, and ships an optional udev entry (never
  installed or applied for you) that makes the kernel report the standard Xbox controller codes to every program.
  The root steps are in the README. Confirmed with press tests on one ROG Xbox Ally (RC73YA); the Xbox Ally X is
  matched but untested.
- **`omarchy-game-mode doctor`** (#14): read-only checks for Xbox Remote Play and Xbox Cloud Gaming (network path,
  UDP and NAT, Wi-Fi, firewall, browsers, video decode, controllers, vibration support and input daemons),
  `doctor buttons` to press each control and see the code that really arrives, and `--markdown` for a redacted
  report to paste into an issue. It never runs sudo and never changes anything.
- **InputPlumber support, read-only** (#11): while InputPlumber runs, Game Mode reads its virtual gamepad instead
  of losing the controller, and `status` reports what it found. Game Mode never installs, configures or stops
  InputPlumber.
- **`omarchy-game-mode keyboard toggle|show|hide|status`** (#10) shows or hides the optional touch keyboard from
  a launcher, a compositor binding or a controller shortcut.

## [0.1.0] - 2026-11-03

### Added

- **Gaming Home** with a library of pinned platforms (Steam Big Picture, Xbox Remote Play through Greenlight,
  Xbox Cloud Gaming, GeForce NOW, Moonlight, RetroArch, Heroic, Lutris, Battle.net and Minecraft) and a Resume
  card for the current game session.
- **Discover** shows whether each platform is installed, pinned or installable. Installs start only when you
  choose them and run in a visible terminal with Omarchy's own installers.
- **Handoff to games** through Hyprland: Game Mode hides once a launch is accepted, waits for the real game
  window and comes back when the session ends.
- **Controller navigation** read non-exclusively from evdev: A selects, B goes back, LB and RB switch pages, Menu
  opens Settings, and holding View and Menu for 0.8 seconds shows Game Mode from anywhere. The Guide button is
  left alone. Keyboard, mouse and touch work everywhere.
- **Controller modes** (Automatic, Game input, Desktop navigation) with optional AntiMicroX profile switching,
  off by default.
- Seven languages (English, Español, Français, Deutsch, Português (Brasil), 日本語 and العربية with a
  right-to-left layout) that follow Omarchy's language setting.
- Omarchy theme colors, and an optional login start that is off until you turn it on.
- Command line: `show`, `discover`, `desktop`, `resume`, `launch`, `mode`, `autostart`, `mapper`, `status` and
  `quit`.

[Unreleased]: https://github.com/brianbaldock/omarchy-game-mode/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/brianbaldock/omarchy-game-mode/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/brianbaldock/omarchy-game-mode/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/brianbaldock/omarchy-game-mode/releases/tag/v0.1.0
