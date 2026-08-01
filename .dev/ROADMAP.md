# Media Stack Roadmap

Status: `[ ]` planned · `[~]` in progress · `[x]` complete · `[!]` blocked

## Now

### [x] MS-1 — Whisper transcription fallback

Deployed `whisper-asr-webservice` v1.9.1 with NVIDIA CDI, `faster_whisper`, the
`medium` model, and float16 CUDA inference. The API is internal-only, persists
its model cache, and unloads model memory after an idle interval.

Bazarr's `whisperai` provider is last in the provider list and enabled only as an
automated fallback when human providers do not reach the configured minimum
score. Single-series fallback remains disabled to avoid accidental interactive
bulk work. Whisper output is excluded from `ffsubsync`, remains eligible for
Bazarr's human-subtitle upgrade search, and is identified by Bazarr history,
file xattrs, and a private hash manifest. Human-provider replacement clears the
generated xattrs.

Validation covered a short English transcription, Spanish language detection,
and one full-length Spanish-to-English subtitle. Output was UTF-8 SRT with
positive, ordered, non-overlapping cues inside the media runtime. Whisper's
translation mode only outputs English; English-to-Spanish remains MS-2.

### [ ] MS-2 — English to Latin American Spanish translation

Translate a validated timed English SRT while preserving cue boundaries and
timing.

- Parse and validate SRT before translation.
- Chunk with overlap/context suitable for feature-length content.
- Instruct the model to use neutral Latin American Spanish and avoid Castilian
  vocabulary and `vosotros`.
- Validate cue count, timestamps, encoding, runtime bounds, and text completeness.
- Run translation and transcription sequentially to avoid GPU contention.
- Back up any destination, publish atomically, and roll back failed validation.

### [x] MS-3 — OpenSubtitles.com coverage

Bazarr 1.6.0 is configured with hash matching and Latin American Spanish
language mapping. AI- and machine-translated provider results remain disabled.
Free-tier quota exhaustion is treated as retryable rather than as a reason to
change language policy.

## Next

### [ ] MS-4 — Librarian agent

Build a recurring report-first library audit covering:

- redundant encodes and oversized-for-value files;
- malformed or unsupported media layouts;
- orphaned artwork and unmatched entries;
- malformed, mislabeled, duplicated, out-of-runtime, wrong-cut, or probably
  unsynchronized subtitles.

Repairs require explicit approval, a backup, post-change verification, and
rollback on failure.

### [ ] MS-5 — Semantic library search

Embed library metadata and support natural-language title discovery without
sending the private library inventory to an external service.

### [ ] MS-6 — Per-user recommendations

Use separate Jellyfin watch histories to produce recommendations based on viewer
preference rather than licensing or promotion economics.

### [ ] MS-7 — Governed media events

Publish import failures, disk pressure, integrity reports, and scrub results to
the internal event plane without exposing media titles or user identities in
public logs.

## Blocked

### [!] MS-8 — Challenge-gated public indexers

The deployed challenge solver does not currently handle the target sites'
Turnstile flow reliably. Do not weaken network isolation or spend repeated
effort on the same solver version. Prefer a supported indexer or a legitimate
private source with better retention.

## Deferred decisions

- **Seeding policy:** choose a ratio/time ceiling and remove torrent entries
  without deleting hardlinked library files.
- **Whisper model size:** keep `medium` unless measured GPU contention or latency
  justifies another model.
- **Generic Spanish fallback:** continue preferring no subtitle over an unwanted
  Castilian subtitle until the validated translation stage exists.
- **Parallel language copies:** retain one Radarr file per title unless a real
  requirement justifies a second Radarr and separate library.
