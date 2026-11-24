# Security policy

Omarchy Game Mode is pre-release software. Security fixes land on the current `main` branch and are released as a new version (see the [changelog](CHANGELOG.md)); older versions may not receive fixes.

## Reporting a vulnerability

Do not publish vulnerabilities, credentials, or personal data in a public issue. Use [GitHub private vulnerability reporting](https://github.com/brianbaldock/omarchy-game-mode/security/advisories/new) when it is enabled. If that option is unavailable, ask the maintainer for a private reporting channel without posting exploit details.

Include the affected revision, a minimal reproduction, the expected and actual behavior, and the security impact. Redact account details and unrelated desktop information from logs and captures.

## Trust boundaries

The launcher reads controller events without exclusively grabbing devices or creating virtual input. It does not change firmware, kernel drivers, or performance limits itself. Platform installation is a deliberate action that opens a trusted Omarchy installer in a visible terminal.

The one setting of the machine it can change is the power profile (power-saver, balanced or performance), and only when the user asks for it in Gaming Home or runs `omarchy-game-mode power set` or `power cycle`. The change is a request to power-profiles-daemon on the system bus, which asks polkit whether the session may make it; a refusal is reported, never worked around. Gaming Home never starts that daemon, never takes or releases a profile hold, never offers or asks for authentication, and never writes sysfs or firmware settings itself. To let Omarchy keep the choice for the current power source, it runs Omarchy's own `omarchy-powerprofiles-set` when that is installed: without a shell, with a minimal environment and a 10 second limit.

GitHub workflows use immutable action pins and minimal token permissions. CodeQL scans Python without executing application build scripts. Fork pull requests must never receive repository secrets or deployment permissions.
