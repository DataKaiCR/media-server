# External viewer onboarding

## Public and private boundary

The onboarding procedure is safe to document publicly. Its architecture and
least-privilege checks are not credentials. Public documentation may describe
loopback maintenance, hidden external roles, explicit Movies/TV access,
request-only import, aggregate verification, and rollback.

Keep these values only in protected runtime state:

- personal account names and Tailscale login identities;
- passwords, API keys, session tokens, and secret-manager item identifiers;
- Jellyfin/Jellyseerr user IDs and device IDs;
- live Tailnet addresses, share-recipient inventories, and access history;
- policy snapshots, database backups, receipts containing private IDs, and
  request history.

The tool therefore reads identities and credentials only from mode-`0600`
files outside Git. Standard output contains counts and booleans, never names,
IDs, paths, or credentials.

## Scope

[`scripts/external-viewer-onboard.py`](../scripts/external-viewer-onboard.py)
creates one password-protected viewer in an already accepted isolated external
application plane. It:

1. proves the existing Jellyfin policy and Jellyseerr identity boundary are
   clean;
2. creates one non-administrator Jellyfin account;
3. backup-first applies the hidden `external` role;
4. atomically appends the account to the private policy authority;
5. verifies password authentication and exactly Movies/TV effective views;
6. imports the exact Jellyfin ID into Jellyseerr with permission `32`
   (request-only); and
7. verifies Jellyseerr authentication and writes an aggregate private receipt.

It does **not** create or share a Tailscale machine, change Tailnet policy,
create/revoke API keys, write to a password manager, approve a request, or test
real client playback. Those remain explicit operator lifecycle steps.

## Prerequisites

Before onboarding:

- the separate external Jellyfin and Jellyseerr instances are healthy;
- Jellyfin exposes only Movies and TV to this application plane;
- the recipient has accepted the individually issued private-network share;
- the existing Tailnet rule already allows accepted shared identities only to
  the gateway HTTPS port, so routine onboarding requires no policy writer;
- every current external Jellyfin user is enumerated as role `external` in the
  private policy and the aggregate audit is clean;
- Jellyseerr contains exactly one administrator plus the same request-only
  external identities, with new-user auto-provisioning disabled; and
- a unique credential has already been generated and stored in an approved
  secret manager.

Take the normal stopped-service/database backup when the operational change
window requires full application-state rollback. The tool also creates a
private pre-change policy copy and the policy engine publishes its own
backup before changing any Jellyfin policy.

## Loopback maintenance paths

Neither external application publishes a routine host port. Establish temporary
loopback-only relays in separate terminals, using ports that are not already in
use.

Jellyfin relay:

```bash
jellyfin_address=$(
  sudo podman inspect jellyfin-external \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
)

socat \
  TCP-LISTEN:18096,bind=127.0.0.1,reuseaddr,fork \
  "TCP:${jellyfin_address}:8096"
```

Jellyseerr relay:

```bash
jellyseerr_address=$(
  sudo podman inspect jellyseerr-external \
    --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
)

socat \
  TCP-LISTEN:15055,bind=127.0.0.1,reuseaddr,fork \
  "TCP:${jellyseerr_address}:5055"
```

Stop both relays with `Ctrl-C` after verification. Never bind these maintenance
paths to a LAN or Tailnet address.

## Private input

Create a fresh mode-`0700` state directory for each attempt. Copy
[`config/external-viewer-onboarding.example.toml`](../config/external-viewer-onboarding.example.toml)
outside Git, update its private absolute paths, and protect it with mode `0600`.
The referenced Jellyfin policy is the same private authority used by
[`scripts/jellyfin-policy.py`](../scripts/jellyfin-policy.py).

The private credential file is strict JSON:

```json
{
  "username": "<private-exact-viewer-name>",
  "password": "<unique-secret-manager-generated-password>"
}
```

It must be mode `0600`, outside Git, and contain no other keys. Do not construct
it with a password on a command line. Export it from the approved secret manager
or paste it through a private editor that does not retain history.

The Jellyfin policy's API key file and the onboarding configuration's
Jellyseerr API key file must likewise be mode `0600`. Prefer a temporary,
dedicated Jellyfin API key and revoke it after the run. Jellyseerr's
administrative API key is presented only to the loopback relay and must remain
protected as application administrative state.

## Preflight

Preflight performs API reads but creates no account and writes no receipt:

```bash
PYTHONPATH=scripts python3 scripts/external-viewer-onboard.py \
  --config /srv/private-state/jellyfin-external/onboarding.toml
```

A successful result reports `ready: true`. The tool fails closed when the target
already exists, a current account is missing from either application, a public
Jellyfin profile exists, an existing policy has drift, Jellyseerr defaults are
unsafe, a private file is too permissive, a URL is not loopback-only, or private
state is inside a Git worktree.

## Apply

After reviewing the aggregate preflight, apply once:

```bash
PYTHONPATH=scripts python3 scripts/external-viewer-onboard.py \
  --config /srv/private-state/jellyfin-external/onboarding.toml \
  --apply
```

Success reports one policy account updated, Jellyfin authentication passing,
Movies/TV as the only effective collection types, zero public profiles, and
Jellyseerr permission `32`. The private state directory receives:

- `jellyfin-policy.pre.toml`, the mode-`0600` pre-change policy authority; and
- `external-viewer-onboarding-receipt.json`, a mode-`0600` aggregate receipt.

The receipt deliberately contains no account name, ID, password, token, private
path, media title, or request.

## Failure and rollback

The preflight requires all existing viewers to be compliant, so the generated
Jellyfin plan may change only the new account. If a later operation fails, the
tool uses only API-acknowledged IDs to:

1. remove an acknowledged Jellyseerr import;
2. remove the acknowledged new Jellyfin account;
3. restore the exact private policy bytes; and
4. revalidate both original application identity boundaries and Jellyfin policy.

A verified rollback is reported as a bounded failure. The tool never guesses an
ID from a username after an ambiguous transport failure because that could
remove a concurrently created account. An ambiguous commit, incomplete
deletion, configuration restore, or policy verification is therefore a hard
failure: preserve the private state directory, stop onboarding, and use the
independent application backup. Never rerun blindly after an incomplete
rollback.

## Acceptance and cleanup

After a successful apply:

1. authenticate from the recipient's shared private-network client;
2. confirm the hidden profile requires explicit username/password entry;
3. confirm only Movies and TV are visible;
4. confirm deletion, download, Live TV, sharing, and management controls are
   absent;
5. test direct play and one bounded transcode when required by the client;
6. authenticate to Jellyseerr, submit a test request, and prove it remains
   pending rather than auto-approved;
7. revoke the temporary Jellyfin API key and remove its local file;
8. stop both loopback relays; and
9. retain the protected receipt and backup until acceptance passes.

Because accepted shared identities are covered by the existing gateway-only
rule, these routine steps do not require a Tailscale policy write. Revoke a
person by revoking their machine share and disabling/deleting their application
accounts; do not broaden the gateway rule.
