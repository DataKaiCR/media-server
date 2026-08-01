# Security Policy

## Reporting

Report vulnerabilities through GitHub's private vulnerability reporting for
this repository. Do not open a public issue containing credentials, private
network information, service tokens, media inventories, or exploit details.

If private reporting is unavailable, contact the repository owner through the
private contact method listed on the DataKaiCR GitHub profile.

## Scope

The supported configuration is the latest revision of `main`. Container images
and upstream applications retain their own security policies; reports about an
upstream vulnerability should also be sent upstream.

## Deployment expectations

- Keep `.env`, application state, databases, backups, and logs outside Git.
- Keep administrative services behind authenticated private access.
- Run qBittorrent only inside Gluetun's network namespace.
- Treat bind-mount permissions, SELinux labels, and the container socket as
  security boundaries.
- Pin images, review upgrades, and back up application state before migrations.
- Never publish real tokens while demonstrating a configuration problem.
