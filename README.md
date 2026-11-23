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
  fallback. There are no background images or idle redraws, and the launcher itself makes
  no network requests. Only the `doctor` command, when you run it, sends the diagnostic
  probes listed under [Doctor](#doctor).

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
`~/.config/autostart/io.github.brianbaldock.OmarchyGameMode.desktop`, which runs the installed
launcher by its absolute path with `--autostart`, so Gaming Home appears at login.
`background` starts the app without showing a window. `autostart off` removes only
the autostart file Game Mode created.

`omarchy-game-mode mapper on|off|status` turns AntiMicroX set switching on or off.

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
omarchy-game-mode doctor buttons          Guided button test (see below)
```

What it checks:

- **Network path:** default route, IPv6 consistency, VPN or proxy in the way, which DNS
  resolver answers, the Microsoft and Xbox service names, TLS to them, the clock, MTU.
- **Streaming transport:** UDP to Microsoft's relay and two public STUN servers, the
  relay's TURN challenge, and how your router maps one connection to several servers.
- **Wi-Fi:** band, channel width, radar-shared (DFS) spectrum, signal, power saving,
  roams and disconnects in the last 10 minutes, loss between the device and the router.
- **Firewall:** which firewall is active and, for ufw, the saved policy.
- **Clients and video:** browsers, Greenlight, browser flag files, hardware H.264 decode.
- **Controller:** which control codes each gamepad lists (read without a handheld daemon,
  the ROG Xbox Ally's built-in pad lists generically numbered buttons and no standard
  right-stick click), InputPlumber, Handheld Daemon and AntiMicroX, and controller
  interfaces that another daemon has hidden.

What it never does:

- **No root.** It never runs `sudo`. Where only root can answer, it prints the command
  for you to run and marks the result "cannot verify without root".
- **No changes.** Nothing is written, installed, enabled, started or stopped. No
  controller is grabbed and no input is injected. Advice is text you can act on or ignore.
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
Microsoft's relay, plus one to the regional Microsoft relay it redirects to (followed once,
and only to a public address on UDP 3478); one time query to `time.cloudflare.com`; and a
few pings to your router and to `stun.cloudflare.com`. Names are only probed at public
unicast addresses, whatever the resolver answers. With `--console-ip`, the console
probes described above go to that one address.

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
least one failed and 2 for a usage error.

Microsoft's supported-browser list for `xbox.com/play` and `xbox.com/remoteplay` does
not include desktop Linux, so the doctor always warns that the web client can fail here
whatever the network looks like. Greenlight is the client to compare against.

**Privacy.** Reports are redacted before they are printed. Public IP addresses become
tokens such as `<public-ip-1>` (the same address gets the same token within one report),
private addresses keep only their first two numbers (`192.168.x.x`), and the Wi-Fi network
name, access-point and device MAC addresses, host name, user name, home directory, DNS
search domains and serial numbers are never included. Values are scrubbed wherever they
appear, not only in known fields: a controller named after its serial number or an
address shows up as `<id>` or `<mac>`, and a private path as `<home-path>`. `--no-redact`
shows IP addresses in full for your own debugging; it cannot be combined with `--markdown`.

**Sharing a report.** Run `omarchy-game-mode doctor --markdown`, read it once, and paste
it into the "Optional redacted evidence" field of the
[hardware-validation issue form](https://github.com/brianbaldock/omarchy-game-mode/issues/new?template=hardware-validation.yml).

The doctor's output is English only by design, so reports read the same for everyone who
helps with them; it adds no strings to the translated interface.

### Hardware button test

`omarchy-game-mode doctor buttons` asks you to press each control once and shows the code
that really arrives for it: the Xbox button, View, Menu, A, B, X, Y, the bumpers and
triggers, both stick clicks, the D-pad, both sticks, the back paddles and the two buttons
under the screen. Run it in a terminal; Enter skips a control and `q` stops the test.

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

Nothing about your controller is assumed. At the end the test lists where it differs
from an Xbox-style pad on Linux, for example an Xbox button that sends the View code (so
Steam does not open its menu), a right-stick click that never arrives, paddles that
arrive as keyboard keys, or two buttons that send the same code. It also prints an SDL
mapping line built only from the presses it saw and names the controls it left out.

`--raw` additionally reads the controller's raw HID report, when your user is allowed
to, and tells you when a press changes bits the controller never declared. That is how a
button that exists in the hardware but is invisible to the kernel shows up. Together with
`--device`, only a raw interface of that same device (the same USB device in sysfs) is
read; when there is none the test does not start, rather than mix up two controllers.

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
- Virtual controllers are currently skipped. HHD and InputPlumber configurations may hide or exclusively own the physical controller, leaving Game Mode without native navigation. Compatibility with those input stacks is unverified; do not disable hardware protections to work around it.
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
  gyro and TDP support. Not yet validated together: Game Mode currently skips virtual controllers, so some HHD configurations may not provide navigation to this app.
- [omarchy-rog-ally-codex-plugin](https://github.com/richelle-belle/omarchy-rog-ally-codex-plugin):
  ROG Ally setup automation.
- [omarchy-launcher](https://github.com/headband42/omarchy-launcher): Quickshell
  launcher for Omarchy.

## License

MIT. See [LICENSE](LICENSE).
