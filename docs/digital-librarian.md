# Digital Librarian

## Purpose

The Digital Librarian provides one governed, report-first inventory for private
audiovisual, photo, book, and document collections. The current MS-4A release is
strictly read-only: it opens collection files for inspection and hashing but has
no move, rename, delete, metadata-write, or repair implementation.

Collection roots, filenames, metadata, hashes, and reports are private runtime
state. They must not appear in commits, issues, CI output, or public logs.

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

## Configuration

Copy [`config/librarian.example.toml`](../config/librarian.example.toml) to a
private location outside the repository. Define public-safe collection IDs,
collection kinds, roots, exclusions, and a private report directory:

```toml
version = 1
report_dir = "/srv/private-state/librarian/reports"

[[collections]]
id = "primary-photos"
kind = "photos"
root = "/srv/collections/photos"
exclude_globs = [".thumbnails/**"]

[[collections]]
id = "digital-books"
kind = "books"
root = "/srv/collections/books"
exclude_globs = []
```

Protect the configuration even though it contains no credentials: collection
paths are private inventory information.

## Running an audit

```bash
python scripts/librarian-audit.py \
  --config /srv/private-state/librarian/config.toml
```

Standard output contains collection IDs and aggregate counts, not individual
filenames. The complete JSON report remains under the configured private report
directory. An audit records its configuration hash and available optional
analyzer capabilities.

Before the first audit of a new root, take an independent metadata snapshot or
backup. Compare file count, bytes, timestamps, and inode metadata after the run
to verify read-only behavior.

## Current evidence

MS-4A inventories every regular file and records relative path, extension, size,
nanosecond modification time, detected format, shallow metadata, and a SHA-256
only when a same-size candidate needs exact duplicate comparison.

Current findings include:

- unreadable, empty, or unsupported files;
- extension/format mismatches;
- malformed archive and EPUB containers;
- missing PDF EOF markers and independent `pdfinfo` parser failures;
- password/encryption evidence without attempting circumvention;
- unreadable image dimensions in supported headers;
- exact duplicate groups;
- filenames that collide under case-insensitive filesystems;
- orphaned XMP/AAE photo sidecars;
- skipped symbolic links and hashing races.

`pdfinfo` from Poppler is optional. When present it supplies an independent PDF
parse; the report records whether that capability was available.

## Report interpretation

A finding is not permission to repair. In particular:

- identical hashes do not establish which copy is authoritative;
- a PDF parser failure may require recovery from its source, not rewriting;
- an encrypted file may be legitimate and must not trigger DRM circumvention;
- an unsupported extension means the analyzer lacks policy, not that the file
  has no value;
- a clean shallow photo report does not prove visual quality or complete EXIF.

The private report is the evidence input for future approval plans. MS-4F will
require a plan bound to source hashes before any mutation is possible.

## Planned collection modules

### Audiovisual

Container/stream integrity, duplicate encodes, subtitle timing and provenance,
orphaned artwork, and application adapters.

### Photos

Deep decoding, EXIF and time-zone checks, RAW/JPEG and Live Photo pairing,
perceptual duplicates, burst selection, quality signals, events, and local visual
embeddings. Originals remain untouched; metadata changes should prefer sidecars.

### Books and documents

ISBN and bibliographic reconciliation, edition/format grouping, covers, series,
language, PDF text/OCR quality, EPUB internals, comics, and audiobooks. Online
metadata lookups must be optional and disclose identifiers only, never document
content. Books held only on a phone should first be exported into an explicit
private intake collection rather than silently scraped from a changing device.

## Rollback

MS-4A does not modify collection files, so collection rollback is unnecessary.
A report can be deleted after verifying its hash or restored from private backup.
If a configuration is wrong, retain the report as evidence, correct the private
configuration, and run a new audit; never edit a published report in place.
