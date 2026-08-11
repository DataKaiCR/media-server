# Media Server

## Project Name

**Media Server**

## Description

A rootful Podman media stack built around Jellyfin, the Servarr applications,
and a VPN-gated download path. It supports NVIDIA hardware transcoding,
language-aware movie requests, and English/Latin American Spanish subtitles.

## Architecture

| Service | Port | Purpose |
|---|---:|---|
| Jellyfin | 8096 | Playback and library management |
| Jellyseerr | 5055 | Standard movie and TV requests |
| Jellyseerr Latino | 5056 | Movie requests requiring Latin American Spanish audio |
| Radarr | 7878 | Movies |
| Sonarr | 8989 | TV |
| Bazarr | 6767 | Subtitle search, fallback policy, and history |
| Whisper ASR | internal | GPU transcription and speech translation fallback |
| Ollama | external | Private English-to-Latin-American-Spanish translation API |
| Digital Librarian | local CLI | Report-only audits for media, photos, and books |
| Prowlarr | 9696 | Indexer management |
| FlareSolverr | internal | Supported challenge solving for Prowlarr |
| Gluetun | profile only | VPN namespace and firewall |
| qBittorrent | profile only | Download client inside Gluetun's namespace |

Every downloader and Servarr application mounts the same `/data` tree. Keeping
`media/` and `torrents/` on one filesystem lets Radarr and Sonarr import using
hardlinks instead of storing a second copy.

qBittorrent has no independent network namespace: it joins Gluetun with
`network_mode: service:gluetun`. If the VPN stops, qBittorrent loses network
access. Both services are opt-in through the Compose `download` profile.

## Installation

### Requirements

- Rootful Podman and a Compose implementation
- SELinux labels that permit containers to access the configured bind mounts
- NVIDIA Container Toolkit with a generated CDI specification for GPU playback
  and transcription
- Fast local storage for persisted Whisper and Ollama model caches
- A VPN provider with WireGuard and port-forwarding support for the download
  profile

### SELinux

On enforcing Fedora-family systems, label the configured storage and application
state paths as `container_file_t`, then persist those labels with
`semanage fcontext`. Large media mounts intentionally omit `:z`/`:Z` because a
container-start relabel can walk the entire library. Small private configuration
mounts may use `:Z`; Bazarr uses it to prevent cache files retaining an obsolete
private MCS label after container replacement.

### NVIDIA CDI

Generate and verify the CDI device before starting Jellyfin:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo podman run --rm --device nvidia.com/gpu=all \
  docker.io/nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## Configuration

Copy the example and replace every environment-specific value:

```bash
cp .env.example .env
chmod 600 .env
```

`DATA`, `APPDATA`, and `TRANSCODE` are host paths. VPN credentials belong only
in `.env` or an external secret manager; never place them in Compose, logs,
documentation, issues, or commits.

## Usage

Validate before applying changes:

```bash
sudo /path/to/podman-compose config
sudo /path/to/podman-compose up -d
```

Start the VPN-gated download path explicitly:

```bash
sudo /path/to/podman-compose --profile download up -d gluetun
sudo /path/to/podman-compose --profile download up -d qbittorrent
```

On a host where the opted-in download path must survive reboots, install the
ordered systemd unit. It waits for Gluetun to become healthy before starting
qBittorrent, preserving the VPN kill switch. The generic Podman restart unit is
a soft, ordered dependency so an unrelated container startup failure cannot
block the VPN-gated download path:

```bash
sudo install -D -m 0644 \
  config/systemd/media-download-stack.service \
  /etc/systemd/system/media-download-stack.service
sudo systemctl daemon-reload
sudo systemctl enable --now media-download-stack.service
```

The Latino request portal includes optional Traefik labels. Set
`LATINO_REQUEST_HOST` and `TRAEFIK_NETWORK` for the local routing environment,
or use the published port directly.

## Jellyfin customization

Jellyfin can use built-in branding, local artwork, and reviewed Custom CSS
without adding Fanart.tv or another metadata provider. Dynamic web features such
as Media Bar and Home Screen Sections are optional, version-sensitive layers and
must be introduced one at a time with a stopped-service backup and client
validation. See [docs/jellyfin-customization.md](docs/jellyfin-customization.md).

## Jellyfin viewer access

Jellyfin uses separate administrator, guest, household-viewer,
restricted-viewer, and request identities. A private, loopback-only policy tool
audits every non-admin against explicit current-library access and denies
deletion, downloads, public
sharing, Live TV management, shared-device control, and remote access by
default. The restricted role also limits library types, rating score, and
unrated video. Application is backup-first, aggregate-only, verified, and rolls
back failures; account names and identifiers never enter repository state or
logs. See [docs/jellyfin-viewer-access.md](docs/jellyfin-viewer-access.md).

## Language-aware requests

The standard Radarr profile requires English audio. Latin American Spanish
audio receives a positive custom-format score, so English-plus-Latino releases
win when available while English-only releases remain acceptable.

The movie-only Latino request portal uses a separate default profile that:

- requires Radarr's `Spanish (Latino)` language classification;
- accepts 720p and 1080p;
- prefers dual-audio releases;
- does not silently fall back to English or Castilian Spanish.

Both portals share one Radarr and one library. Radarr normally manages one file
per title, so an existing English-only movie must be upgraded to dual/Latino
audio rather than added as a parallel copy. Reliable results also require an
indexer with strong Latin American coverage.

## Seeding policy and evidence

Torrent retention uses four explicit tiers: common public material has a
three-day floor; standard material targets ratio 3 after at least 14 days;
contributor material targets ratio 5 after at least 30 days; and scarce,
Latin American, obscure, or low-swarm material remains protected for at least
90 days and may be retained indefinitely. Tracker-specific seed-time and
hit-and-run rules always override these defaults.

The initial seeding auditor is report-only. It reads qBittorrent through a
loopback-only API configuration and publishes mode-0600 aggregate evidence
without torrent names, infohashes, tracker domains, announce URLs, passkeys, or
per-torrent records. It cannot tag, pause, limit, or delete torrents. Tracker
profiles remain the authoritative evidence for tracker applications. See
[docs/seeding-evidence.md](docs/seeding-evidence.md).

## Movie quality policy

The everyday library targets 1080p WEB-DL or Blu-ray encodes. The Latino
profile may accept 720p when Latin American audio is scarce, while remaining
upgradeable to 1080p. CAM, telesync, telecine, workprint, screener, and SD
sources are not acceptable for normal requests. Remux and 2160p releases are
opt-in rather than defaults.

Radarr quality definitions use runtime-scaled MB/min guardrails:

| Quality | Minimum | Preferred | Maximum |
| --- | ---: | ---: | ---: |
| 720p HDTV, WEB, or Blu-ray | 8 | 22 | 45 |
| 1080p HDTV or WEB | 12 | 35 | 70 |
| 1080p Blu-ray encode | 15 | 45 | 90 |

These limits include all streams. They prevent unusually small, visibly
compressed releases and unexpectedly large downloads without treating file
size as proof of quality. Grainy films may need more bitrate, while animation
and efficient HEVC encodes may need less. Prefer WEB-DL over WEBRip, retain
legitimate stereo for older films, and prefer 5.1 or dual audio when available.
Do not replace a Latin American Spanish release with a higher-resolution
English-only or Castilian release.

Apply this policy to new downloads first. Existing files should be audited and
upgraded selectively rather than triggering an unbounded library-wide search.
Keep at least 15–20 percent of the data filesystem free and pause automatic
acquisition before utilization reaches 85 percent.

## Subtitles

Bazarr is pinned to 1.6.0 and requests English plus `ea` (`Spanish Latino`). Do
not substitute generic `es`, which providers commonly map to Spain's variant.
Hash matching is enabled and provider results marked AI- or machine-translated
are rejected.

Future low-confidence downloads may be synchronized with `ffsubsync` against
the title's original-language audio. Existing subtitles are not modified
retroactively. Any manual repair should back up the sidecar, verify the result,
and roll back failures.

Whisper runs internally with the `faster_whisper` engine, the `medium` model,
CUDA float16 inference, and an idle timeout that releases model memory. Bazarr
uses it only as an automated fallback after human providers fail to meet the
minimum score. The service has no published host port and receives 16 kHz mono
audio extracted by Bazarr rather than direct media-library access.

Whisper output is excluded from `ffsubsync` because its timestamps already come
from the audio. Bazarr records the provider in history, and post-processing adds
source/hash xattrs plus an append-only mode-`0600` private manifest without
modifying SRT content. Symlink targets are refused, and a manifest failure
restores the prior xattr state. A later human-provider replacement clears the
generated markers.
Bazarr's upgrade search may therefore replace generated output with a better
human subtitle.

Whisper can transcribe English audio and translate other audio **to English**;
it cannot translate English into Spanish. A separate loopback-only Ollama
pipeline translates a size-bounded, validated English SRT into neutral Latin
American Spanish while preserving cue identifiers and timestamps. Model
responses are locally bounded and strictly shaped. Translation is serialized
against Whisper, validated before an atomic publish, and recorded with
model/prompt provenance. It refuses
to replace human or unmarked subtitles, and its generated filename does not
satisfy Bazarr's human `ea` target.

Generated files must never silently replace human subtitles. See
[docs/whisper-bazarr.md](docs/whisper-bazarr.md) and
[docs/ollama-subtitle-translation.md](docs/ollama-subtitle-translation.md) for
configuration, validation, and rollback details.

## Digital Librarian

The report-only Digital Librarian inventories private audiovisual, photo, book,
and document collections without modifying originals. Its strict private TOML
configuration defines disjoint collection roots and a report directory outside
them. The core performs shallow format validation, exact duplicate hashing,
filename collision and photo-sidecar checks, and atomic private JSON reporting
with no proposed actions.

The audiovisual module adds bounded local container/stream and physical
packet-order evidence, configurable size and bitrate review signals,
conservative movie/series layout and possible redundant-encode groups, orphaned
artwork/NFO and unmatched subtitle checks, external subtitle timing/runtime
evidence, and generated-subtitle provenance validation. It persists neither
subtitle dialogue, raw ffprobe output, nor individual packet rows and cannot
transcode, tag, rename, replace, or delete media.

The books/documents module adds bounded PDF page and text-layer evidence, OCR
recommendations without OCR execution, EPUB package and bibliographic checks,
MOBI metadata, conservative edition/series grouping, cover evidence, and a
hash-stable private phone intake role. Extracted document text is never persisted
or sent over a network.

The photo module adds local deep decoding, EXIF capture-time/time-zone evidence,
coordinate-free GPS controls, quality-review signals, RAW/rendered and
still/motion pairing, local visual fingerprints, perceptual duplicate candidates,
and bounded burst review groups. Decoded pixels are never persisted, and no
quality or similarity signal authorizes deletion.

Application adapters, semantic event/album curation, and all repair execution
remain separate roadmap stages. See
[docs/digital-librarian.md](docs/digital-librarian.md) for safety invariants,
configuration, report interpretation, and intake workflow.

## Development

Development workflow, validation requirements, and public-boundary rules are in
[CONTRIBUTING.md](CONTRIBUTING.md). Planned work is tracked in
[.dev/ROADMAP.md](.dev/ROADMAP.md).

## Governance and security

This is a public DataKai project governed by PK metadata, DKOS workspace policy,
fail-closed Git hooks, CI validation, and secret scanning. Runtime state,
credentials, backups, private hostnames, account names, addresses, and library
inventories do not belong in the repository.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[.dev/ROADMAP.md](.dev/ROADMAP.md).

## License

[MIT](LICENSE)
