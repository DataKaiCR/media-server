# Digital Librarian

## Purpose

The Digital Librarian provides one governed, report-first inventory for private
audiovisual, photo, book, and document collections. MS-4A supplies the read-only
catalog and MS-4D adds bounded book/document evidence. Neither release has a
move, rename, delete, metadata-write, import, OCR-write, or repair implementation.

Collection roots, filenames, metadata, hashes, and reports are private runtime
state. They must not appear in commits, issues, CI output, or public logs.
Extracted PDF text is analyzed in bounded temporary output and is never persisted
in a report or log.

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
- No external metadata or document-content network request is implemented.

## Configuration

Copy [`config/librarian.example.toml`](../config/librarian.example.toml) to a
private location outside the repository. Define public-safe collection IDs,
collection roles, roots, exclusions, parser bounds, and a private report
directory:

```toml
version = 1
report_dir = "/srv/private-state/librarian/reports"

[book_analysis]
pdf_text_layer = true
pdf_sample_pages = 12
parser_timeout_seconds = 30
max_parser_output_bytes = 1048576
max_parser_memory_bytes = 1073741824

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
settings have enforced minimum and maximum values; unknown settings are rejected.
Protect the configuration even though it contains no credentials because
collection paths are private inventory information.

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
- a PDF parser failure may require recovery from its source, not rewriting;
- an encrypted file may be legitimate and must not trigger circumvention;
- an OCR recommendation may reflect intentionally sparse pages;
- an unsupported extension means the analyzer lacks policy, not that the file
  has no value;
- a clean shallow photo report does not prove visual quality or complete EXIF.

The private report is evidence for future approval plans. MS-4F will require a
plan bound to source hashes before any mutation is possible.

## Optional tools and network boundary

`pdfinfo` and `pdftotext` from Poppler are optional and their availability is
recorded in each report. Without them, shallow PDF evidence remains available,
but page metadata and text-layer quality are absent. The implementation has no
online bibliography client. Any future metadata adapter must be opt-in, disclose
identifiers only, never upload document content, and record its provenance.

## Planned modules

- **Audiovisual:** container/stream integrity, duplicate encodes, subtitle timing
  and provenance, orphaned artwork, and application adapters.
- **Photos:** deep decoding, EXIF/time-zone checks, RAW/JPEG and Live Photo
  pairing, perceptual duplicates, burst selection, events, and local embeddings.
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
