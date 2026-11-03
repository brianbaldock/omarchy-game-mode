# Security policy

Omarchy Game Mode is pre-release software. Security fixes target the current `main` branch; older previews may not receive fixes.

## Reporting a vulnerability

Do not publish vulnerabilities, credentials, or personal data in a public issue. Use [GitHub private vulnerability reporting](https://github.com/brianbaldock/omarchy-game-mode/security/advisories/new) when it is enabled. If that option is unavailable, ask the maintainer for a private reporting channel without posting exploit details.

Include the affected revision, a minimal reproduction, the expected and actual behavior, and the security impact. Redact account details and unrelated desktop information from logs and captures.

## Trust boundaries

The launcher reads controller events without exclusively grabbing devices or creating virtual input. It does not change firmware, kernel drivers, or performance limits. Platform installation is a deliberate action that opens a trusted Omarchy installer in a visible terminal.

GitHub workflows use immutable action pins and minimal token permissions. CodeQL scans Python without executing application build scripts. Fork pull requests must never receive repository secrets or deployment permissions.
