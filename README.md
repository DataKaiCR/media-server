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
| Bazarr | 6767 | Subtitles |
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

The Latino request portal includes optional Traefik labels. Set
`LATINO_REQUEST_HOST` and `TRAEFIK_NETWORK` for the local routing environment,
or use the published port directly.

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

## Subtitles

Bazarr is pinned to 1.6.0 and requests English plus `ea` (`Spanish Latino`). Do
not substitute generic `es`, which providers commonly map to Spain's variant.
Hash matching is enabled and provider results marked AI- or machine-translated
are rejected.

Future low-confidence downloads may be synchronized with `ffsubsync` against
the title's original-language audio. Existing subtitles are not modified
retroactively. Any manual repair should back up the sidecar, verify the result,
and roll back failures.

The roadmap adds local Whisper transcription as a fallback below human
providers, followed by a separate English-to-Latin-American-Spanish translation
stage. Generated files must remain identifiable and must never silently replace
human subtitles.

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
