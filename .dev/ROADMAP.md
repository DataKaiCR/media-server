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

### [x] MS-2 — English to Latin American Spanish translation

Deployed a validated translation pipeline through the existing Ollama service
using `translategemma:12b`. It preserves cue identifiers and timestamp lines,
uses bounded chunks with neighboring context, protects outer formatting and
proper names, and instructs the model to produce neutral Latin American Spanish
without Castilian second-person plural wording.

Strict input/output checks reject malformed UTF-8 SRT, duplicate or regressing
cues, overlaps, runtime violations, non-English sources, cue drift, missing text,
truncation, suspiciously untranslated output, changed formatting, and protected
name loss. Deterministic retries have a fixed budget.

The wrapper serializes translation against Bazarr and Whisper GPU work. Output is
staged and validated beside its destination, marked with source/model/prompt
provenance, and atomically published with a private hash manifest. Repair of an
existing generated translation is backup-first and rolls back publication
failures. Human and unmarked subtitles are never replacement targets. Generated
Spanish remains visibly labeled in Jellyfin while Bazarr continues searching for
a human `ea` subtitle.

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
