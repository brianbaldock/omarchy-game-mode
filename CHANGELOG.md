# Changelog

All notable changes to Omarchy Game Mode are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); until 1.0, a minor version can change behavior.

Every pull request that changes what people see or do adds a line under **Unreleased**. The release
command turns that section into a version and brings the version strings, the website and the README into
step; see [Changelog and releases](CONTRIBUTING.md#changelog-and-releases).

## [Unreleased]

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
