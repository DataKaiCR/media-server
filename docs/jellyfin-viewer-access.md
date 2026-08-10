# Jellyfin viewer access

## Purpose

Jellyfin administration, household playback, restricted playback, and request
submission are separate identities. Personal viewer accounts preserve distinct
watch history and recommendation state; shared household or administrator
credentials are not onboarding shortcuts.

Account names, user identifiers, API keys, device identifiers, access history,
and policy backups are private runtime state. They must not appear in commits,
issues, CI output, or public logs.

## Least-privilege contract

The private policy configuration must enumerate every non-administrator account
and assign exactly one role. The tool fails closed if a configured account is
missing, an unknown non-admin appears, the server does not have exactly one
administrator, or required library types are absent.

| Capability | Household viewer | Restricted viewer |
| --- | --- | --- |
| Library access | Explicit current movie, series, music, and book folders | Explicit current movie and series folders |
| Parental rating | Unrestricted | Jellyfin score 10 |
| Unrated movies, trailers, and series | Allowed | Blocked |
| Playback, remux, audio/video transcoding | Allowed | Allowed |
| Delete media | Denied | Denied |
| Download, sync transcode, or media conversion | Denied | Denied |
| Public sharing | Denied | Denied |
| Live TV and channel access | Denied | Denied |
| Collection, subtitle, or lyric management | Denied | Denied |
| Shared-device control | Denied | Denied |
| Remote access | Denied until the reviewed MS-CP-4 entry point exists | Denied |

Explicit folders prevent a future private library from becoming visible merely
because it was added to Jellyfin. The restricted role's rating and unrated-item
rules are defense in depth; its narrower folder allowlist remains authoritative.
Device playback stays enabled because official television and mobile clients
need remux or bounded server transcoding when direct play is unavailable.

The administrator policy is never generated or updated by this tool.

## Private configuration

Copy [`config/jellyfin-policy.example.toml`](../config/jellyfin-policy.example.toml)
outside the repository. Replace every placeholder account with the exact private
Jellyfin identity and protect both the configuration and API key:

```bash
chmod 600 /srv/private-state/jellyfin/config.toml
chmod 600 /srv/private-state/jellyfin/policy-api-key
chmod 700 /srv/private-state/jellyfin
```

Use a dedicated Jellyfin API key when available. The URL must be a loopback
HTTP(S) origin with no credentials, path, query, or fragment. The backup
directory must also remain outside the repository.

Audit without mutation:

```bash
PYTHONPATH=scripts python3 scripts/jellyfin-policy.py \
  --config /srv/private-state/jellyfin/config.toml
```

Output is aggregate-only. Exit status `0` means compliant, `2` means bounded
policy drift, and `1` means configuration, API, or state validation failed. No
account name, user identifier, API key, folder name, or backup path is printed.

Apply only after reviewing the aggregate drift:

```bash
PYTHONPATH=scripts python3 scripts/jellyfin-policy.py \
  --config /srv/private-state/jellyfin/config.toml \
  --apply
```

Application publishes a mode-`0600` private policy snapshot under a mode-`0700`
directory before the first API mutation. Every changed account is verified by a
fresh API read. A failure restores all policies already changed; an incomplete
rollback is a hard failure that requires the independent Jellyfin database
backup.

## Adding a viewer

1. Confirm the viewer's personal identity and intended role out of band. Never
   invent a generic account or place a password in Git history or shell logs.
2. Create the account through the loopback/LAN administrator interface with a
   unique temporary password delivered privately.
3. Add the exact account name and role to the private TOML before running the
   policy tool. Until then, the tool intentionally fails because an unknown
   non-admin exists.
4. Review the aggregate dry run, apply, and rerun the audit until it is clean.
5. Authenticate as that viewer, confirm only intended libraries are visible,
   verify deletion/download controls are absent, and test direct play plus one
   bounded transcode if the client's codec support requires it.
6. Retain the policy snapshot until login and playback verification pass.

Do not enable remote access during account creation. MS-CP-4 must establish the
entry point first, and MS-CP-5 must validate one restricted remote viewer before
other relatives are onboarded.

## Rollback

For an ordinary policy error, reapply the last private JSON snapshot through the
administrator API and rerun the aggregate audit. If API rollback cannot be
verified, stop Jellyfin, preserve the failed database separately, restore the
pre-change SQLite backup, start Jellyfin, and confirm `/health`, account count,
administrator access, library visibility, and viewer playback. Policy rollback
does not require media restoration because policy updates never modify media.
