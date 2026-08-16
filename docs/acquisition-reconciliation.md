# Acquisition reconciliation

The acquisition audit detects approved movie requests that can remain in
Jellyseerr's processing state without an active Radarr acquisition. It is a
private, report-only control: it cannot approve requests, search indexers, grab
releases, alter monitoring or profiles, remove queue entries, or modify files.

## Evidence flow

The audit reads only loopback API endpoints:

1. Every configured Jellyseerr instance supplies bounded, paginated approved
   movie requests.
2. Radarr supplies bounded movie, queue, and history snapshots.
3. Requests are deduplicated by Radarr service ID, with TMDb ID as a fallback.
4. Each movie receives one evidence category based on current file, queue,
   monitoring, availability, search, grab, and import state.
5. A private JSON report is staged, flushed, set to mode `0600`, and atomically
   published in a mode-`0700` directory. Each report links to the prior report's
   filename and SHA-256 digest.

The console emits aggregate counts, the private report filename, and its digest.
It never emits movie titles, requesters, account identifiers, tokens, release
titles, download IDs, or media paths.

## Categories

| Category | Meaning |
| --- | --- |
| `available` | Radarr reports an imported file. |
| `active_transfer` | Radarr has one or more current queue records. |
| `not_yet_available` | Radarr says the monitored title is not yet available. |
| `recent_search_no_grab` | Radarr searched within the configured interval but has no grab. |
| `recent_request_never_searched` | A new request has no search timestamp yet. |
| `stale_no_grab` | The last search is older than the interval, with no file, queue, or grab. |
| `never_searched` | An older approved request has no search timestamp. |
| `grabbed_without_file_or_queue` | Grab history exists, but no file or current queue record remains. |
| `file_missing_after_import` | Import history exists, but Radarr no longer reports a file. |
| `unmonitored_missing` | The requested movie exists in Radarr but is not monitored and has no file. |
| `radarr_missing` | No Radarr movie matches the request's service or TMDb ID. |
| `unknown_timing_no_grab` | Malformed or missing timestamps prevent a stale decision. |

Only stale, missing, unmonitored, and abandoned-grab/import categories count as
actionable findings. A finding is evidence for review, never authorization to
search or mutate.

## Private configuration

Copy `config/acquisition-reconciliation.example.toml` outside the repository.
The configuration and each API-key file must be regular non-symlink files with
mode `0600` or more restrictive. API-key files contain exactly:

```toml
api_key = "private-runtime-value"
```

Service URLs must be credential-free loopback HTTP(S) origins. Record limits,
response-byte limits, timeouts, and pagination bounds fail closed rather than
publishing incomplete evidence.

Run the audit with:

```bash
python scripts/acquisition-audit.py \
  --config /srv/private-state/acquisition-reconciliation/config.toml
```

## Privacy and interpretation

Private reports include movie titles, years, service IDs, source-instance IDs,
and sanitized state evidence because an operator must be able to investigate a
finding. They deliberately omit requester objects, usernames, email addresses,
authentication tokens, download IDs, release titles, and filesystem paths.
Never copy reports into the repository, tickets, or public logs.

The audit does not query Radarr's interactive release endpoint because doing so
contacts indexers and changes `lastSearchTime`. Therefore `stale_no_grab` does
not mean an acceptable release currently exists. It only means the approved
request has no file, queue, prior grab, or recent search evidence.

Post-import audio validation is a separate MS-ACQ-2 control. Radarr and release
title language classifications are not proof of actual audio streams.
