# Security policy

Omarchy Game Mode is pre-release software. Security fixes target the current `main` branch; older previews may not receive fixes.

## Reporting a vulnerability

Do not publish vulnerabilities, credentials, or personal data in a public issue. Use [GitHub private vulnerability reporting](https://github.com/brianbaldock/omarchy-game-mode/security/advisories/new) when it is enabled. If that option is unavailable, ask the maintainer for a private reporting channel without posting exploit details.

Include the affected revision, a minimal reproduction, the expected and actual behavior, and the security impact. Redact account details and unrelated desktop information from logs and captures.

## Trust boundaries

The launcher reads controller events without exclusively grabbing devices or creating virtual input. It does not change firmware, kernel drivers, or performance limits. Platform installation is a deliberate action that opens a trusted Omarchy installer in a visible terminal.

Bluetooth is changed only when the user asks for it in the Gaming Home window. Gaming Home can then switch Bluetooth on or off for the whole machine (with Omarchy's `omarchy-bluetooth-power`, which blocks or unblocks every radio through rfkill and keeps the choice across reboots, or by writing the adapter's `Powered`), pair with a device the user picked, trust it so that it may reconnect by itself, connect and disconnect remembered devices, and forget them. It never makes the machine discoverable or pairable, sets no discovery filter and never asks to be BlueZ's default agent. For a pairing it registers a pairing agent on its own D-Bus connection, for that attempt only. The agent sees what BlueZ asks about that one device (a number to compare, a code to type on the device, a request for a PIN or a passkey) and refuses every other caller, every other device and every service authorisation. BlueZ's D-Bus policy lets only root, in practice bluetoothd, call an agent; the agent also checks that each call comes from the current owner of `org.bluez`. On Omarchy, `bt-agent` stays BlueZ's default agent, so pairings started by other devices and service authorisations still go to it and never to Gaming Home. Codes, PINs and passkeys are shown to the user and passed to BlueZ, and are never logged or kept. BlueZ's error texts and device addresses never reach the window or a log.

GitHub workflows use immutable action pins and minimal token permissions. CodeQL scans Python without executing application build scripts. Fork pull requests must never receive repository secrets or deployment permissions.
