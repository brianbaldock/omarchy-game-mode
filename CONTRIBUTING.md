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

HHD/InputPlumber compatibility is not established. Game Mode currently ignores virtual controllers; a stack that hides or owns the physical device may leave native navigation unavailable. Report the input stack as part of your setup; do not disable its protections or change permissions to make a test pass.

## Report hardware results

Use the hardware-validation issue form. Include device/controller model, Omarchy and
Hyprland versions, app commit, mapper setup, exact steps and observed result. Mark
each case pass/fail/not tested rather than treating an untested device as supported.

Useful cases: Home navigation; native Guide in Steam Big Picture; a brief View/Menu
press versus the deliberate hold; game -> desktop -> game focus changes; opening
Home and resuming; controller disconnect/reconnect; normal quit and SIGTERM restoring
the initial mapper set. Test fullscreen and windowed games separately.

`omarchy-game-mode status` helps, but review it before posting: window titles can
contain private information. Never post credentials, full process environments,
account details, controller serial numbers or unredacted desktop recordings.
Do not grant extra device permissions or bypass a lock screen for a report.

## Send a pull request

Keep changes focused and explain the user-visible problem. Include reproduction
steps, relevant checks and screenshots for UI changes. Add focused regression tests
for behavior fixes; run `make test`. Validate desktop-entry changes with
`desktop-file-validate data/*.desktop`.

For translations, edit `data/locales/<locale>.json`, keep every key and placeholder,
and run `make test`. Review visible UI as well as strings, especially Arabic RTL.
Prioritize native review of the existing seven draft locales before adding languages.
For the static website, use `python3 -m http.server 8000 --bind 127.0.0.1 --directory site`
and check narrow screens, keyboard focus and the existing content security policy.

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
