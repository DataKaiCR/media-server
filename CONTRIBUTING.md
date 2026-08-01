# Contributing

## Public boundary

Do not commit credentials, `.env` files, databases, backups, logs, media
inventories, private hostnames, addresses, account names, or screenshots that
contain them. Use placeholders in examples and report security issues privately.

## Development Setup

Install rootful Podman, a Compose implementation, PK, and DKOS. Copy
`.env.example` to `.env` with placeholders or local private values. Do not start
the download profile without a verified VPN configuration.

## Workflow

1. Create a focused branch from `main`.
2. Make one logical change at a time.
3. Copy `.env.example` to `.env` and validate Compose.
4. Run the checks below.
5. Open a pull request describing behavior, risk, validation, and rollback.

```bash
sudo /path/to/podman-compose config
python -m unittest discover -s tests -v

dkos check-hooks --strict --no-codex-drift
git diff --check
```

Do not start the download profile during validation unless a working VPN is
configured and its egress isolation has been verified.

## Code Style

Keep Compose declarative, pin image versions, use YAML anchors only where they
remain readable, and parameterize deployment-specific values through `.env`.
Documentation and comments must use public placeholders rather than live runtime
details.

## Commit messages

This is a DataKai public project. Use an imperative subject, preferably no more
than 72 characters. Do not use Conventional Commit prefixes, internal ticket
language, AI attribution, or generated-by trailers.

Examples:

- `Add a language-specific request portal`
- `Require VPN isolation for the download client`
- `Document subtitle integrity checks`

## Testing

At minimum, validate every Compose profile with the public example environment,
run the DKOS hooks, execute `git diff --check`, and scan the complete Git history
for secrets. Changes that touch live state require service-specific health and
behavior checks.

## Pull requests

Keep changes reviewable and avoid unrelated formatting. State whether a change
recreates containers, changes persistent state, touches the media library, or
requires manual migration. Runtime repair procedures must be backup-first and
include verification and rollback.
