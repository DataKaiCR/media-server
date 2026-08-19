# Seeding policy and private evidence

## Purpose

The seeding evidence audit records aggregate qBittorrent contribution evidence
without persisting torrent names, infohashes, tracker domains, announce URLs,
passkeys, or per-torrent rows. It is report-only: it cannot tag, pause, limit, or
delete a torrent.

A local client report is useful for operating the policy and preserving a daily
history, but it is not authoritative proof for a private tracker. The tracker's
own profile, account age, ratio, seed size, warnings, and hit-and-run state remain
authoritative.

Use torrent distribution only for material you are authorized to acquire and
share. A private tracker does not change the underlying rights.

## Policy tiers

| Tier | qBittorrent tag | Minimum seed time | Ratio target | Review point |
| --- | --- | ---: | ---: | ---: |
| Common | `seed-common-3d` | 3 days | none | 3 days |
| Standard | `seed-standard-3x-14d` | 14 days | 3.0 | 30 days |
| Contributor | `seed-contributor-5x-30d` | 30 days | 5.0 | 90 days |
| Stewardship | `seed-stewardship-90d` | 90 days | informational | 365 days |

Time is a floor. Standard and contributor torrents meet their policy threshold
only when both time and ratio are satisfied. A torrent that reaches its review
point without ratio is evidence for human review, not permission to delete.
Stewardship torrents are protected regardless of ratio. Use that tier for rare,
Latin American, obscure, or low-swarm material.

Tracker-specific minimums always override these defaults. The three-day tier is
for highly available public material and must not be used to evade a private
tracker's seed-time or hit-and-run rules.

The initial backfill is manually reviewed. A separate, narrowly scoped default
classifier can keep later acquisitions from becoming unclassified: once torrent
metadata is available, it assigns Standard to non-private metainfo and
Contributor to private metainfo. Those are retention floors, not tracker-rule
proof. It never assigns Common, promotes to Stewardship, changes an existing
policy tag, or infers language, rarity, or tracker rules. Conflicting rows are
left unchanged and produce an aggregate attention result.

The evidence audit remains strictly report-only and has no mutation endpoint.
The classifier uses a separate client that allowlists only tag creation,
addition, and rollback; it cannot pause, limit, or delete torrents.

## Private configuration

Copy [`config/seeding-evidence.example.toml`](../config/seeding-evidence.example.toml)
to private state outside the repository. The configuration and credential file
must be mode `0600`; the report directory is created as mode `0700`.

The credential file contains exactly:

```toml
username = "<private-qbittorrent-username>"
password = "<private-qbittorrent-password>"
```

The qBittorrent URL is restricted to a loopback HTTP(S) origin with a valid port
and no credentials, path, query, or fragment, so credentials cannot be sent to
an external or ambiguous endpoint by configuration mistake. Authentication is
optional only when qBittorrent already permits the local request.

An optional `forwarded_port_file` lets the report compare qBittorrent's listener
with the VPN's current forwarded-port state. A match is configuration evidence,
not proof that an outside peer can connect. Tracker-side connectability remains
the stronger signal.

## Running

```bash
python scripts/seeding-audit.py \
  --config /srv/private-state/seeding-evidence/config.toml
```

Standard output includes only the report basename, digest, aggregate torrent
count, aggregate ratio, and unclassified count. Full reports are atomically
published mode `0600`. A non-blocking lock prevents concurrent runs.

Each report includes:

- current and all-time uploaded/downloaded byte totals and aggregate ratios;
- complete payload bytes and aggregate qBittorrent state counts;
- ratio and cumulative seed-time buckets;
- counts for each policy tier and threshold;
- unclassified and conflicting-policy-tag counts;
- bounded low-swarm evidence based on qBittorrent's reported seeder count;
- upload-limit and forwarded-port alignment evidence;
- a hash pointer to the previous regular, generated-name report; symlinks and
  untrusted filenames are ignored;
- explicit privacy, authority, and no-mutation declarations.

API responses have a fixed byte ceiling and strict nested shapes. Version,
connection, and torrent-state values are allowlisted or reduced to `unknown`,
so malformed local responses cannot inject arbitrary strings into a report.
Non-finite numeric values reduce to bounded aggregate defaults rather than
crashing the audit.

It never includes enough information to reconstruct a torrent inventory. Daily
reports therefore support internal policy review but do not replace tracker
profile links or unedited tracker-side evidence when requesting access.

## Default classification

Preview aggregate assignments without mutation:

```bash
python scripts/seeding-classify.py \
  --config /srv/private-state/seeding-evidence/config.toml
```

Apply the approved defaults explicitly:

```bash
python scripts/seeding-classify.py \
  --config /srv/private-state/seeding-evidence/config.toml \
  --public-default-tier standard \
  --private-default-tier contributor \
  --apply
```

The classifier waits when metadata size or the private-metainfo flag is not yet
usable, and a later timer run retries. It preserves non-policy tags and any
existing single policy tier. Output and service logs contain aggregate counts
only; torrent names, hashes, tracker data, and per-torrent rows are neither
printed nor persisted. If post-mutation verification finds a race or conflict,
the classifier removes only the tag it just attempted and reports attention.

Common and Stewardship remain review decisions. In particular, low-swarm or
Latin American releases should be promoted to Stewardship rather than relying
indefinitely on the Standard default. Tracker-specific rules still override a
Contributor default and must be checked at removal review.

## Scheduling

Run the evidence report once daily after qBittorrent has been available long
enough to refresh its state. Run the approved classifier every five minutes so
metadata-pending additions are retried promptly. Example hardened units are
[`media-seeding-classification.service`](../config/systemd/media-seeding-classification.service)
and
[`media-seeding-classification.timer`](../config/systemd/media-seeding-classification.timer).
They use `UMask=0077`, the private configuration path, and a tag-only mutation
surface. Keep cleanup in a separately reviewed workflow; enabling classification
does not authorize automatic removal.

Do not implement cleanup as qBittorrent's simple ratio-or-time share limit. The
policy requires tier-aware logic, tracker-rule precedence, protected torrents,
and explicit review when a ratio target cannot be reached.

## Tracker evidence package

When an official application asks for evidence, use the tracker profile as the
source of truth. Depending on its rules, a redacted package may include:

- account age;
- tracker-reported uploaded and downloaded totals and ratio;
- current seed count and seed size;
- bonus-point or average-seed-time evidence;
- zero warning and hit-and-run status;
- connectability status;
- a profile URL when requested through an official recruiter.

Never reveal passkeys, announce URLs, cookies, API keys, private torrent files,
or unrequested IP and account details. Do not buy or trade invitations, alter
statistics, or present client-generated totals as tracker-generated proof.

## Relationship to media retention

A lower-quality but usable library file remains authoritative until a replacement
has downloaded, imported, and passed validation. Quality findings do not permit
quarantine. Likewise, high torrent ratio alone does not authorize payload
removal: hardlinked payload can remain available to peers without consuming a
second full copy, and scarce sources may deserve indefinite stewardship.

## Rollback

The audit mutates no qBittorrent or payload state. Disable its timer and remove
its private reports/configuration to roll it back. Reports can be retained as
immutable evidence or deleted after their hashes are independently recorded.

To stop future default tagging, disable
`media-seeding-classification.timer`. Existing reviewed policy tags should
normally remain because disabling automation does not invalidate their retention
classification. The classifier never changes payload, pause state, share limits,
or tracker configuration.
