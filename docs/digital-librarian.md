# Digital Librarian

## Purpose

The Digital Librarian provides one governed, report-first inventory for private
audiovisual, photo, book, and document collections. MS-4A supplies the read-only
catalog, MS-4B adds bounded audiovisual evidence, MS-4C adds private photo
evidence, and MS-4D adds bounded book/document evidence. None has a move,
rename, delete, metadata-write, import, OCR-write, transcode, or repair
implementation.

Collection roots, filenames, metadata, hashes, and reports are private runtime
state. They must not appear in commits, issues, CI output, or public logs.
Extracted PDF text is analyzed in bounded temporary output and is never persisted
in a report or log. Decoded photo pixels are reduced locally to numeric quality
signals and visual fingerprints; pixel buffers are never written to reports.
Subtitle dialogue, raw ffprobe output, and individual packet rows are likewise
never persisted: only selected stream, aggregate packet-order, timing,
relationship, and provenance evidence is retained.

## Safety invariants

- Collection roots must be existing absolute directories and cannot overlap.
- The private report directory must be outside every collection.
- Root and report paths cannot be symbolic links.
- Traversal never follows symbolic links.
- One non-blocking file lock prevents concurrent audits per report directory.
- Reports contain an empty `proposed_actions` list.
- Reports are staged, synchronized, atomically renamed, and mode `0600`.
- The report directory and runtime configuration are mode `0700` and `0600`.
- Exact duplicates are evidence only; distinct formats and editions are not
  treated as interchangeable.
- Encrypted documents are reported without password or DRM circumvention.
- Audiovisual analysis does not decode frames, transcode media, or persist
  subtitle dialogue, arbitrary container tags, raw parser output, packet
  timestamps, or packet byte positions.
- Packet-order analysis reads only a configured leading packet count under the
  same parser time, memory, and output limits as other audiovisual evidence.
- Size, bitrate, duplicate-encode, and subtitle findings are review evidence and
  never authorize deletion or replacement.
- No external metadata or document-content network request is implemented.

## Configuration

Copy [`config/librarian.example.toml`](../config/librarian.example.toml) to a
private location outside the repository. Define public-safe collection IDs,
collection roles, roots, exclusions, parser bounds, and a private report
directory:

```toml
version = 1
report_dir = "/srv/private-state/librarian/reports"

[audiovisual_analysis]
enabled = true
parser_timeout_seconds = 30
max_parser_output_bytes = 4194304
max_parser_memory_bytes = 1073741824
packet_order_sampling = true
packet_sample_packets = 50000
interleave_skew_threshold_seconds = 30
subtitle_max_bytes = 8388608
large_file_bytes = 21474836480
high_bitrate_bits_per_second = 40000000
subtitle_runtime_tolerance_seconds = 30

[book_analysis]
pdf_text_layer = true
pdf_sample_pages = 12
parser_timeout_seconds = 30
max_parser_output_bytes = 1048576
max_parser_memory_bytes = 1073741824

[photo_analysis]
enabled = true
location_detail = "presence"
quality_signals = true
perceptual_duplicates = true
near_duplicate_distance = 4
burst_window_seconds = 2
burst_max_span_seconds = 15
parser_timeout_seconds = 20
max_parser_memory_bytes = 1073741824
max_image_pixels = 50000000

[[collections]]
id = "movie-library"
kind = "audiovisual"
role = "library"
media_layout = "movies"
root = "/srv/collections/media/movies"
exclude_globs = ["@eaDir/**"]

[[collections]]
id = "series-library"
kind = "audiovisual"
role = "library"
media_layout = "series"
root = "/srv/collections/media/series"
exclude_globs = ["@eaDir/**"]

[[collections]]
id = "primary-photos"
kind = "photos"
role = "library"
root = "/srv/collections/photos"
exclude_globs = [".thumbnails/**"]

[[collections]]
id = "digital-books"
kind = "books"
role = "library"
root = "/srv/collections/books"
exclude_globs = []
```

`role` defaults to `library`. The `intake` role is limited to book collections,
requires its root to be mode `0700` or stricter, and hashes every regular file,
not only same-size duplicate candidates. Parser
settings have enforced minimum and maximum values; unknown top-level, analysis,
and collection settings are rejected. The loader requires an absolute, regular,
non-symlink configuration file with mode `0600` or stricter. Audiovisual
collections accept `movies`, `series`, or conservative `mixed`
layout evidence. Prefer separate movie and series roots when the filesystem
already provides that boundary. Protect the configuration even though it
contains no credentials because collection paths are private inventory
information.

## Running an audit

```bash
python scripts/librarian-audit.py \
  --config /srv/private-state/librarian/config.toml
```

Standard output contains collection IDs and aggregate counts, not individual
filenames. The complete JSON report remains under the configured private report
directory. Reports record their configuration hash, analyzer settings, optional
tool availability, collection role, and schema version.

Before the first audit of a new root, take an independent metadata snapshot or
backup. Compare file count, bytes, modification times, and inode metadata after
the run to verify read-only behavior.

## Core evidence

The catalog records every regular file's relative path, extension, size,
nanosecond modification time, detected format, and bounded metadata. SHA-256 is
normally calculated only when a same-size candidate needs exact duplicate
comparison; every intake file receives a stable SHA-256.

Core findings include:

- unreadable, empty, or unsupported files;
- extension/format mismatches;
- malformed archives;
- exact duplicate groups;
- filenames that collide under case-insensitive filesystems;
- orphaned XMP/AAE photo sidecars;
- skipped symbolic links and files that race inspection or hashing.

## Audiovisual evidence

### Containers, streams, and bounded review thresholds

For recognized video and audio containers, local `ffprobe` records only selected
container formats, runtime, bitrate, chapter count, and bounded stream summaries.
Video summaries include codec, dimensions, pixel format, and frame rate; audio
summaries include codec, channels, layout, and valid language tags; subtitle
summaries include codec, language, and default/forced disposition when exposed.
Arbitrary title/comment tags, parser stderr, input filenames, and raw ffprobe JSON
are discarded. The parser runs with wall-clock, address-space, regular-file
output, and core-dump limits. Failure or timeout becomes evidence for that file
instead of aborting the audit.

Configured size and bitrate thresholds create `oversized-media-review` findings.
They are triage thresholds, not quality judgments: a remux, archival master, long
runtime, high frame rate, or unusual content may fully justify the result. The
finding explicitly carries `automatic_action = false` and does not compare
release names or seek a smaller download.

### Bounded physical packet-order sampling

When enabled for a video with audio, a second local `ffprobe` invocation reads at
most `packet_sample_packets` leading packets. It retains only aggregate counts,
stream indexes, position coverage, and maximum audio lead/lag relative to the
primary video timestamp frontier. If byte positions are present for every
sampled packet, they define physical ordering; otherwise the demuxer's bounded
output order is used. Invalid selected rows are counted only in aggregate and
make the sample explicitly incomplete. Individual timestamps, byte positions,
filenames, tags, and raw packet rows are discarded.

A stream crossing `interleave_skew_threshold_seconds` creates
`media-packet-interleave-skew`. This identifies severe physical scheduling that
can force a player or transcoder to read far ahead before synchronized audio is
available. It is bounded leading-file evidence rather than proof that every
packet is malformed. Deliberately sparse or late-starting tracks, timestamp
discontinuities, and unusual containers still require review. Timeout,
output-limit, and parser failures retain partial aggregate evidence and an
incomplete-sample finding; no repair is attempted and `automatic_action` remains
false.

The focused Iron-Grade suite falsifies physical ordering, whole-sample fallback,
exact skew boundaries, regressing timestamps, DTS/PTS selection, malformed and
oversized rows, parser failure recovery, stream-selection traps, privacy, and
read-only real-tool integration.

### Layout, unmatched sidecars, and redundant encodes

Each audiovisual collection declares a `media_layout`. Movie layout expects a
scope directory around primary video. Series layout conservatively recognizes
`SxxEyy` filename tokens and supports show/season nesting. Mixed layout is
available when a cleaner root boundary does not exist, but produces weaker
evidence. Findings cover root-level or unexpectedly deep primary media, series
files without a recognized episode token, and scopes containing sidecars but no
video.

Multiple primary movie videos in one scope, or multiple series videos with the
same recognized episode token, form `possible-redundant-encode-group` evidence.
Known extras/trailer/sample directories are excluded from primary candidates.
Groups retain paths, aggregate bytes, codecs, and resolutions only for manual
review; `automatic_delete` is always false. Explicit alternate-cut/edition
markers instead produce a distinct-edition group with automatic collapse false.
Exact hashes remain separate core evidence and do not establish which encode is
authoritative.

Artwork and NFO files are checked against their configured movie or show scope.
Episode-specific artwork receives a conservative same-stem check. Orphaned or
unmatched sidecars are findings, never deletion candidates. The current module
does not call Radarr, Sonarr, Jellyfin, or an online metadata service; filesystem
evidence cannot by itself prove an application entry is unmatched.

### Subtitle integrity, matching, and provenance

External SRT, WebVTT, ASS/SSA, and VobSub index files receive bounded local timing
analysis. Reports retain cue counts; counts of malformed, nonpositive,
regressing, empty, or overlapping cues; and first/last timing bounds—never
dialogue. Overlap is informational because simultaneous cues can be intentional. Binary VobSub
payload analysis is explicitly limited rather than pretending to validate text.

A subtitle is matched to an unambiguous same-directory video stem or, for a
movie scope with exactly one primary video, to a conventional subtitle
subdirectory/sidecar location. The Librarian reports unmatched/ambiguous
subtitles, language evidence inferred from bounded provenance or conventional
filename suffixes, timings beyond media runtime after a configurable tolerance,
and possible long-form truncation before the media midpoint. These are review
signals; credits, alternate cuts, signs-only tracks, and intentional partial
subtitles can explain them.

Generated-subtitle provenance recognizes the local Whisper and Ollama xattrs. It
records marker presence and validity, compares a marked subtitle hash with the
current bounded file, and reports incomplete, stale, or mismatched markers. It
does not persist marker hashes, model strings, source dialogue, translated text,
or manifest content. Unmarked files are classified only as `human-or-unmarked`;
that is not proof of human authorship. Embedded subtitle streams have stream
metadata evidence but no external-file provenance claim.

## Photo evidence

### Decode and embedded metadata

Supported raster images are decoded locally to a 64-by-64 grayscale working
buffer. Pillow is preferred and uses JPEG decoder downscaling, a wall-clock
alarm, a configurable pixel ceiling, `O_NOFOLLOW`, and first-frame-only handling.
When Pillow is unavailable, ImageMagick runs with wall-clock, address-space,
output, and core-dump limits. Decode failures become evidence without aborting
the collection audit.

The built-in bounded EXIF reader records camera/lens presence, orientation,
original capture time, UTC-offset evidence, and whether GPS metadata exists.
`location_detail = "presence"` reports only a boolean and never coordinates;
`"none"` suppresses even that boolean. Missing or malformed capture times and
UTC offsets are grouped for review. The Librarian does not guess a time zone or
rewrite metadata.

### Quality and local visual evidence

Decoded pixels are reduced to luminance, contrast, entropy, edge strength, and
extreme-dark/extreme-bright fractions. A `photo-quality-review` finding means
only that severe bounded signals merit human inspection. Flat artwork, night
photos, scans, or intentionally blank images can be legitimate. No image is
classified for deletion.

Each decoded image receives a local 64-bit difference hash and a 16-value spatial
luminance descriptor. The report stores those numeric fingerprints, not decoded
pixels. Candidate fingerprints within the configured Hamming distance and a
compatible aspect ratio form conservative perceptual-duplicate groups. Exact
content duplicates remain separate hash evidence, and automatic deletion is
always false.

### Relationships and bursts

Same-directory, same-stem RAW/rendered and still/motion assets are reported as
pairs while preserving every component as authoritative. RAW files without a
same-stem rendered companion are informational, not errors.

Capture-time runs with at least three images can form possible burst groups. Both
the maximum consecutive gap and total span are bounded. Related paths are ordered
for review using edge-strength and entropy signals, but automatic representative
selection is false.

## Book and document evidence

### PDF

When Poppler is installed, `pdfinfo` provides an independent parse, page count,
encryption status, PDF version, and title/author/date presence. `pdftotext`
analyzes only a bounded leading-page sample. The Librarian reports counts and
statistics such as sparse sampled pages, median alphanumeric characters per
sampled page, and an OCR recommendation. It never includes extracted words or
text excerpts.

Each parser has wall-clock, address-space, regular-file output, and core-dump
limits. A process that times out is killed as its own process group. Output-limit, timeout, parser,
and malformed-document failures become evidence rather than aborting the whole
audit. A recommendation does not run OCR or rewrite a PDF.

PDF findings include missing EOF markers, parser rejection, encryption,
incomplete bounded analysis, sparse text layers, and likely OCR candidates.
Leading-page sampling is evidence, not proof that every page has the same text
quality.

### EPUB

EPUB analysis checks:

- the required uncompressed first `mimetype` member;
- safe, unique archive member names;
- bounded `container.xml` and OPF parsing without custom XML entities;
- manifest member existence and spine references;
- navigation-document and declared-cover evidence;
- title, creator, language, identifier, series, and volume metadata;
- validated ISBN-10/ISBN-13 values normalized to ISBN-13.

The analyzer does not parse or persist chapter text. Missing metadata and covers
are reported separately from broken package structure.

### MOBI and AZW3

The bounded Palm/MOBI header and EXTH reader extracts title, author, ISBN, and
ASIN evidence where available. Unsupported or malformed metadata is reported
without attempting conversion or DRM circumvention.

### Covers and conservative grouping

A book record can carry embedded EPUB cover evidence and sibling image
candidates with a matching stem or conventional `cover`/`folder` name. Cover
selection is not automatic.

Files sharing a validated ISBN form a high-confidence probable edition group.
Files with matching normalized embedded title and creator form a medium-confidence
possible work group. Embedded series and volume metadata can form a series group.
Every group records format distinctions and explicitly sets automatic collapse
or reorder to false. Exact hashes still do not determine the authoritative copy.

## Private phone intake

Do not point the Librarian at a changing mounted phone or silently scrape an app.
Use an explicit private intake directory:

```toml
[[collections]]
id = "phone-book-intake"
kind = "books"
role = "intake"
root = "/srv/private-intake/books-from-phone"
exclude_globs = []
```

Workflow:

1. Export or copy selected files from the phone into the private intake root.
2. Disconnect or finish synchronization so the source is stable.
3. Run the Librarian and retain the mode-0600 report.
4. Review format, integrity, bibliography, and SHA-256 evidence.
5. Keep, back up, or reject files manually until a future approval-bound import
   executor exists.

An intake audit marks each file `awaiting-review`. It does not copy into the
library, rename a title, modify phone state, or delete the exported source.

## Report interpretation

A finding is not permission to repair. In particular:

- identical hashes do not establish which copy is authoritative;
- matching title/author metadata does not prove a matching edition;
- multiple video candidates do not prove that one is redundant or inferior;
- an oversized signal can describe a legitimate remux or archival master;
- packet-order skew can reflect an unusual intentional timeline and requires
  review rather than automatic remuxing;
- overlapping or short subtitle timing can be intentional;
- a same-stem subtitle match does not prove language, edition, or cut alignment;
- a PDF parser failure may require recovery from its source, not rewriting;
- an encrypted file may be legitimate and must not trigger circumvention;
- an OCR recommendation may reflect intentionally sparse pages;
- an unsupported extension means the analyzer lacks policy, not that the file
  has no value;
- a perceptual group is a review candidate, not proof of interchangeable images;
- a quality signal can reflect an intentional artistic or documentary choice;
- missing UTC offsets cannot be repaired safely without external context.

The private report is evidence for future approval plans. MS-4F will require a
plan bound to source hashes before any mutation is possible.

## Optional tools and network boundary

`ffprobe`, `pdfinfo` and `pdftotext` from Poppler, Pillow, and ImageMagick are
optional and their availability is recorded in each report. `ffprobe` is the
bounded local audiovisual parser, including optional leading packet-order
sampling; without it, shallow signatures, layout, and sidecar relationships
remain available. Pillow is the preferred photo decoder
and ImageMagick is the bounded fallback. Without a local image decoder, shallow
dimensions and EXIF evidence remain available but quality and visual fingerprints
are absent. The implementation has no online bibliography or media metadata
client. Any future metadata adapter must be opt-in, disclose identifiers only,
never upload document or subtitle content, and record its provenance.

## Planned modules

- **Audiovisual:** optional private application reconciliation and approved
  review-plan generation remain separate from the deployed filesystem evidence.
- **Photos:** event/album suggestions and optional semantic embeddings beyond
  the deployed local visual fingerprints.
- **Curation:** physical-book catalog records, optional private application
  adapters, reading queues, event/album suggestions, and evidence-backed tags.
- **Repair executor:** backup-first, hash-bound, explicitly approved, verified,
  audited, reversible changes only.

## Rollback

The current Librarian does not modify collection files, so collection rollback
is unnecessary. A report can be deleted after verifying its hash or restored
from private backup. If configuration is wrong, retain the report as evidence,
correct the private configuration, and run a new audit; never edit a published
report in place.
