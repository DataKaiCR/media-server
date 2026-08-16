# Post-import language verification

The language audit verifies the labels of audio streams in imported requested
movies using bounded local `ffprobe` evidence. It does not trust release names or
Radarr's release parser, and it never changes or replaces a media file.

This is stream-metadata verification, not speech recognition. `ffprobe` can
prove which language and regional labels are embedded in the imported
container; it cannot prove what language is actually spoken. A suspicious or
unlabeled stream remains a review finding rather than being guessed from a
release title.

## Policy

- Standard Radarr profiles require at least one audio stream tagged as the
  title's Radarr original language.
- Configured Latino profiles require an audio stream with explicit Latin
  American Spanish stream metadata such as `es-419` or an explicit Latino marker
  in the imported stream title.
- Generic `spa` or `es` proves only generic Spanish and remains
  `generic_spanish_unverified` for a Latino request.
- Castilian markers do not satisfy the Latino requirement.
- Existing playable files are retained regardless of a finding. The audit has no
  remediation or mutation path.

## Safety boundary

The audit reads approved movie requests from configured loopback Jellyseerr
instances and a bounded Radarr movie snapshot. Only imported requested movies
are mapped to host files. Every mapping has a fixed Radarr prefix and existing
host root; mapped files must resolve inside that root and be regular,
non-symlink files.

`ffprobe` runs with configured time, output, address-space, and stream-count
limits. The source device, inode, size, and modification time are checked before
and after probing. A changed source invalidates the evidence.

Reports are atomically published at mode `0600` in a mode-`0700` directory and
hash-chain to the previous report. They may contain private movie titles and
sanitized stream language evidence, but never contain:

- media paths;
- raw stream titles or handler names;
- requester identities or tokens;
- dialogue or subtitle text;
- release titles or download identifiers.

Console output is aggregate-only.

## Categories

| Category | Meaning |
| --- | --- |
| `original_verified` | A standard-profile file contains its tagged original language. |
| `original_unverified` | One or more audio streams are untagged, so original-language presence cannot be decided. |
| `original_missing` | All classified audio streams differ from the expected original language. |
| `unsupported_original_language` | The Radarr language cannot be mapped conservatively. |
| `latino_verified` | Explicit Latino stream metadata is present. |
| `generic_spanish_unverified` | Spanish exists, but no regional Latino evidence exists. |
| `latino_unverified` | Untagged audio prevents a reliable Latino decision. |
| `latino_missing` | All classified audio streams lack qualifying Spanish/Latino evidence. |
| `file_unavailable` | The Radarr path cannot be safely mapped to a regular host file. |
| `source_changed` | File identity or metadata changed while it was being probed. |
| `probe_*` | The bounded parser timed out, failed, exceeded a limit, or returned invalid evidence. |

## Private configuration

Copy `config/language-verification.example.toml` outside the repository. The
configuration and each API-key file must be regular non-symlink files with mode
`0600` or more restrictive. API-key files contain exactly:

```toml
api_key = "private-runtime-value"
```

Run:

```bash
python scripts/language-verification-audit.py \
  --config /srv/private-state/language-verification/config.toml
```

Review the private report before any manual search, replacement, profile change,
or file operation. A positive label is evidence for the imported container; a
negative or unknown result is not authorization to delete a playable file.
