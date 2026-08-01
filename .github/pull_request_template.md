## Summary

<!-- What changes and why? -->

## Risk and public boundary

- [ ] No credentials, private endpoints, account names, media inventories, logs, or backups are included.
- [ ] Persistent-state or container-recreation impact is described.
- [ ] Backup and rollback steps are documented where applicable.

## Validation

- [ ] `docker compose config` or `podman-compose config`
- [ ] `git diff --check`
- [ ] Relevant service health and behavior checks

## Rollback

<!-- How can this change be safely reverted? -->
