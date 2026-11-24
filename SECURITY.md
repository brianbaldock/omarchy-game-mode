# Security policy

Omarchy Game Mode is pre-release software. Security fixes land on the current `main` branch and are released as a new version (see the [changelog](CHANGELOG.md)); older versions may not receive fixes.

## Reporting a vulnerability

Do not publish vulnerabilities, credentials, or personal data in a public issue. Use [GitHub private vulnerability reporting](https://github.com/brianbaldock/omarchy-game-mode/security/advisories/new) when it is enabled. If that option is unavailable, ask the maintainer for a private reporting channel without posting exploit details.

Include the affected revision, a minimal reproduction, the expected and actual behavior, and the security impact. Redact account details and unrelated desktop information from logs and captures.

## Trust boundaries

The launcher reads controller events without exclusively grabbing devices or creating virtual input. It does not change firmware or kernel drivers, and it changes performance limits only by asking power-profiles-daemon for one of its profiles. Platform installation is a deliberate action that opens a trusted Omarchy installer in a visible terminal.

Gaming Home changes the machine's power profile (power-saver, balanced or performance) only when the user asks for it in Gaming Home or runs `omarchy-game-mode power set` or `power cycle`. The change is a request to power-profiles-daemon on the system bus, which asks polkit whether the session may make it; a refusal is reported, never worked around. Gaming Home's own calls never start that daemon. It never takes a hold, but choosing a different profile ends every hold other programs have on one (the daemon does that for any change), and the window says whose hold a choice ends. It never offers or asks for authentication and never writes sysfs or firmware settings itself. To let Omarchy keep the choice for the current power source, it runs Omarchy's own `omarchy-powerprofiles-set` when that is installed: without a shell, with a minimal environment and a 10 second limit. That script sets the profile again itself, and the tools it uses may start UPower or the daemon.

GitHub workflows use immutable action pins and minimal token permissions. CodeQL scans Python without executing application build scripts. Fork pull requests must never receive repository secrets or deployment permissions.
