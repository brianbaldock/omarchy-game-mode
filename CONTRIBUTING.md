# Build with us

Omarchy Game Mode is an independent community preview, not an official Omarchy
component. Help make the existing experience reliable before expanding its scope.
You do not need to own a handheld or write code to contribute.

## Pick a small contribution

- **Handheld testers:** record reproducible input and focus behavior on your device.
  Final physical Guide/chord and real-game validation is still pending.
- **Native-language reviewers:** improve one locale's wording, truncation or RTL
  layout. The seven translations are drafts, not certified translations.
- **Python/GTK builders and accessibility reviewers:** improve a specific interaction
  with a regression test or clear before/after evidence.
- **Platform maintainers:** verify an existing launch/install route before proposing
  another integration. Do not copy proprietary assets or unsupported install scripts.

Start with [help wanted issues](https://github.com/brianbaldock/omarchy-game-mode/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22).
Comment with the small part you want to tackle so others can coordinate; you do not
need permission for a small fix. Discuss major dependencies, input ownership changes
or new platform architecture before implementing them.

## Run from a checkout

On Omarchy/Arch, install `python`, `python-gobject` and `gtk4` if missing. The build
also uses `make`; package validation uses `desktop-file-utils`.

```bash
git clone https://github.com/brianbaldock/omarchy-game-mode.git
cd omarchy-game-mode
make test
python3 omarchy-game-mode.py show
```

Tests need no controller, network or running compositor. Translation and keyboard/accessibility review can use a graphical GTK session without Omarchy or Hyprland; they do not establish window-handoff support. The UI needs a graphical
session; window tracking and handoff need Hyprland. Source and installed copies share
one application ID, so quit an existing instance before testing changed source:
`omarchy-game-mode quit`. If that command is not installed, use
`python3 omarchy-game-mode.py quit`.

The app uses your real Game Mode settings, catalog clients and optional mapper. It is
not a sandbox. Do not enable autostart, install clients or enable mapper management
just to run the tests. AntiMicroX management defaults off; opt in only with a profile
whose set 1 is Desktop and set 2 is empty. Never test controller changes in a game
where accidental input could lose progress.

For isolated preferences, create a temporary directory with `mktemp -d` and launch with `XDG_CONFIG_HOME=/path/to/that/directory python3 omarchy-game-mode.py show`. Quit any existing instance first: otherwise the single-instance command reaches that resident process. This isolates preferences, not device access or applications. Keep mapper management and autostart off.

InputPlumber compatibility is read-only: Game Mode reads its D-Bus state and uses the virtual gamepad it announces, and never changes it. This is tested against a fake service (those tests start a private `dbus-daemon`, never touch your session or system bus, and are skipped when `dbus-daemon` is not installed) and was checked on one device, an ASUS ROG Xbox Ally with InputPlumber 0.82.0, without physical button presses. HHD compatibility is not established: its virtual controllers are still ignored, so a stack that hides or owns the physical device may leave native navigation unavailable. Report the input stack as part of your setup; do not disable its protections or change permissions to make a test pass. Changes that would write to InputPlumber (profiles, target devices, events) are input ownership changes: discuss them first.

The clock and battery in the Gaming Home header follow the same rules. UPower is only read, and only while Gaming Home is shown: its name is watched without starting it, `/sys/class/power_supply` is the fallback, and the tests use a fake UPower on a private bus (skipped without `dbus-daemon`). While the window is hidden they must hold no timer, no subscription and no call, so add no polling there. The clock counts its hours the way Omarchy's bar does, which means reading the bar's `shell.json` (the user's own, then Omarchy's default): that is a read-only look each time the window is shown, with no watcher and no timer of its own, and the tests give it scratch files and never the developer's own.

Bluetooth is different: Gaming Home can change it, so it acts only on the user's action. While the window is shown it talks to BlueZ over a D-Bus connection of its own: it watches BlueZ's name without starting it, reads it once and then follows its signals. A search, a pairing, a connection, forgetting a device and switching the power each start from a press in the window, and nothing polls. A hidden Gaming Home holds no connection, no timer, no search and no pairing agent: closing the connection ends a search and drops the agent. Every call goes through one allow-list in `gaminghome/bluez.py`, which never sets a discovery filter, never asks to become the default agent and writes no property but an adapter's `Powered` and a device's `Trusted`; keep it that way. The pairing agent lives for one pairing and answers only BlueZ, only about that device. Omarchy runs `bt-agent` as BlueZ's default agent: Gaming Home's agent queues behind it and is asked only about the pairing Gaming Home started. The tests run against a fake bluetoothd on a private bus (skipped without `dbus-daemon`) and never touch the system bus or a radio. To report hardware results, use the hardware-validation issue form and say which adapter and which kinds and models of device you used, whether `bt-agent` was running, what you pressed and what the window showed: a search, pairing by number, passkey, PIN or without a code, connect, disconnect, forget, and switching off and on. Leave out device names and addresses.

## Report hardware results

Use the hardware-validation issue form. Include device/controller model, Omarchy and
Hyprland versions, app commit, mapper setup, exact steps and observed result. Mark
each case pass/fail/not tested rather than treating an untested device as supported.

Useful cases: Home navigation; native Guide in Steam Big Picture; a brief View/Menu
press versus the deliberate hold; game -> desktop -> game focus changes; opening
Home and resuming; controller disconnect/reconnect; normal quit and SIGTERM restoring
the initial mapper set. Test fullscreen and windowed games separately. With InputPlumber:
whether its virtual gamepad navigates Home, what the vendor buttons do there (nothing
should be selected), and suspend and resume.

Attach `omarchy-game-mode doctor --markdown` as the report: it lists the device,
versions, input stack and network checks, and it is redacted before it is printed (no
addresses in full, no network or host names, no serial numbers). Read it once before
posting. `omarchy-game-mode status` helps too, but review it first: window titles can
contain private information. Never post credentials, full process environments,
account details, controller serial numbers or unredacted desktop recordings.
Do not grant extra device permissions or bypass a lock screen for a report.

## Send a pull request

Keep changes focused and explain the user-visible problem. Include reproduction
steps, relevant checks and screenshots for UI changes. Add focused regression tests
for behavior fixes; run `make test`. Validate desktop-entry changes with
`desktop-file-validate data/*.desktop`. A change that people can see or feel also
adds a line to the changelog (see [Changelog and releases](#changelog-and-releases)).

For translations, edit `data/locales/<locale>.json`, keep every key and placeholder,
and run `make test`. Review visible UI as well as strings, especially Arabic RTL.
Prioritize native review of the existing seven draft locales before adding languages.
For the static website, use `python3 -m http.server 8000 --bind 127.0.0.1 --directory site`
and check narrow screens, keyboard focus and the existing content security policy.
`site/changelog.html` and the release parts of `site/index.html` are generated from the
changelog: edit `CHANGELOG.md` and run `python3 scripts/release.py sync` instead.

Do not grab controllers exclusively, inject synthetic input, capture Guide, run
automatic privileged installers or add idle polling. Preserve opt-in behavior and
restoration of state owned by another app. Document recognition heuristics honestly.

All contributions must be compatible with the project's MIT license. Identify any
borrowed material and its license; do not copy code with incompatible terms.
Be respectful, keep feedback actionable and credit others. Review is best-effort;
there is no promised response time. Security reports belong in
[private vulnerability reporting](https://github.com/brianbaldock/omarchy-game-mode/security/advisories/new),
not public issues.

External fork workflows require maintainer approval before running, so run relevant checks locally and expect that approval step. Maintainers review catalog executable arguments, package names, source provenance and privilege prompts manually; passing CI is not approval of a new install route. Start with existing routes rather than expanding the catalog.

## Changelog and releases

[CHANGELOG.md](CHANGELOG.md) is where every release is described, and `gaminghome/__init__.py` is where the
version lives. The version in the `Makefile` and `PKGBUILD`, the website (its release line, its "What's new"
section, `site/changelog.html` and the sitemap) and the README's release line and install steps all come from those
two, and `make test` fails when any of them is out of step.

**With every pull request** that changes what people see or do (a feature, a fix, a changed default, a removed
option), add a line under `## [Unreleased]` in the matching group: Added, Changed, Deprecated, Removed, Fixed or
Security. Write one short entry that says what changed for the user and ends with the pull request number, like
`(#123)`. Documentation, tests and internal changes need none. Unreleased entries are not published on the website
until they are released.

**To release a version** (maintainers):

1. Run `python3 scripts/release.py bump X.Y.Z`. It turns Unreleased into `X.Y.Z` with today's date, sets the
   version in all three places and regenerates the website, the sitemap and the README. Review the diff and read the
   entries as a user would.
2. Open a pull request titled `Release X.Y.Z`. When it is merged, the website deploys by itself.
3. Tag the merge commit (`git tag -a vX.Y.Z -m "Omarchy Game Mode X.Y.Z"`, then push the tag), build the artifacts
   from a clean checkout of that tag (`make dist`, then `makepkg` on the tarball) and write `SHA256SUMS` for both.
4. Publish them with `gh release create vX.Y.Z --verify-tag --title "Omarchy Game Mode X.Y.Z" --notes-file <(python3
   scripts/release.py notes X.Y.Z) TARBALL PACKAGE SHA256SUMS`.

`python3 scripts/release.py check` reports anything that is out of step, and `sync` regenerates the derived files
after you edit the changelog by hand (`bump` does that for you). Do not edit `site/changelog.html` or the parts of
`site/index.html` and `README.md` between `release:` markers; they are generated.
