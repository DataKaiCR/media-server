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

### [~] MS-4 — Digital Librarian

Build one report-first Librarian for audiovisual media, photos, books, and
personal documents. Collection inventories, filenames, metadata, and reports are
private runtime state. Originals remain authoritative; repair execution stays
separate from analysis and always requires explicit approval.

#### [x] MS-4A — Librarian core

Deployed a read-only filesystem catalog with strict private TOML configuration,
non-overlapping collection roots, symlink refusal, a single-job lock, shallow
format checks, exact duplicate hashing, case-collision detection, photo-sidecar
checks, and atomic mode-0600 JSON reports. Reports contain no proposed actions.
The initial private baseline covered the primary photo and digital-book roots and
verified that source size, timestamps, and inode metadata did not change.

#### [x] MS-4B — Audiovisual collections

Deployed bounded local container/stream evidence, configurable size/bitrate
review signals, conservative movie/series layout and redundant-encode groups,
orphaned artwork/NFO and unmatched subtitle checks, subtitle timing/runtime
analysis, and generated-subtitle provenance validation. Subtitle dialogue, raw
ffprobe output, media frames, and arbitrary container tags are never persisted;
no transcode, tag, rename, replacement, or deletion path exists.

#### [x] MS-4C — Photo collections

Deployed local deep decoding, bounded EXIF capture-time/time-zone evidence,
coordinate-free GPS presence controls, RAW/rendered and still/motion pairing,
quality-review signals, dHash perceptual duplicate groups, bounded burst groups,
and private numeric visual descriptors. Pixels are never persisted, originals
remain authoritative, and every grouping or review order is suggestion-only.
Semantic event/album embeddings remain part of MS-4E/MS-5.

#### [x] MS-4D — Books and documents

Deployed bounded PDF page, metadata, encryption, and sampled text-layer analysis
with evidence-only OCR recommendations that never persist extracted text. Added
EPUB package, cover, language, ISBN, series, and volume checks; bounded MOBI/AZW3
metadata; conservative edition/work/series groups that preserve every format;
and a hash-stable, no-import phone intake role. No online metadata query or DRM
circumvention exists. Physical catalog records and optional book-server adapters
remain candidates for MS-4E.

#### [ ] MS-4E — Curation intelligence

Generate unified private tags, collections, event/album suggestions, reading
queues, and evidence-backed recommendations without exporting collection data.

#### [ ] MS-4F — Repair executor

Execute only an approved, hash-bound plan. Every repair is backup-first, atomic
where possible, post-change verified, audited, and rolled back on failure.

### Remote family access and Cine Pelencho identities

- [x] MS-CP-1 — Audit Jellyfin identity roles read-only and confirm that administration, household viewing, restricted viewing, and request automation remain separate concerns.
- [ ] MS-CP-2 — Add the remaining household viewer and harden every non-admin policy, including parental limits, unrated content, library access, deletion, download, and remote-access permissions.
- [ ] MS-CP-3 — Inventory relatives' television platforms, ISP/CGNAT conditions, measured upload capacity, and official Jellyfin client availability before selecting an exposure model.
- [ ] MS-CP-4 — Implement one reviewed remote entry point: private VPN where client support permits, otherwise HTTPS on port 443 through a hardened reverse proxy; keep administration and Servarr surfaces LAN/VPN-only.
- [ ] MS-CP-5 — Onboard one restricted remote test viewer, validate direct play and bounded hardware transcoding, then add other relatives only after monitoring and rollback checks pass.
- [ ] MS-CP-6 — Keep external viewers on official Jellyfin clients by default; treat Cine Pelencho sideloading or a future Android/Google TV port as optional client work.

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

- **Seeding automation:** retain the agreed 3-day common, 3x/14-day standard,
  5x/30-day contributor, and protected 90-day stewardship tiers in report-only
  mode until aggregate evidence and manual tagging are reviewed. Any later
  cleanup must preserve hardlinked library files and obey tracker rules.
- **Whisper model size:** keep `medium` unless measured GPU contention or latency
  justifies another model.
- **Generic Spanish fallback:** continue preferring no subtitle over an unwanted
  Castilian subtitle until the validated translation stage exists.
- **Parallel language copies:** retain one Radarr file per title unless a real
  requirement justifies a second Radarr and separate library.
