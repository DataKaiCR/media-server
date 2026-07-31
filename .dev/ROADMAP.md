# Media Stack — Roadmap

Host: **gruff** (Bazzite, rootful podman). Companion to `README.md`, which
describes what exists. This file describes what does not exist yet.

Status legend: `[ ]` open · `[~]` in flight · `[x]` done · `[!]` blocked

---

## Now

### [ ] MS-1 — Whisper transcription as a Bazarr provider

Local subtitle generation on the RTX 3080 Ti, so subtitle availability stops
depending on scraping sites that die without warning (`podnapisi.net` did
exactly that on 2026-07-30 — it no longer resolves from anywhere, including
public DNS).

- `whisper-asr-webservice` container, GPU via CDI (`nvidia.com/gpu=all`) — the
  same mechanism already proven by Jellyfin NVENC and Ollama CUDA
- Model `medium` (~5 GB; 11.6 GiB available on the card). `small` is faster but
  noticeably worse on accents and noisy mixes
- Wired into Bazarr as the `whisperai` provider, ranked **below** the scraping
  providers — a human transcript beats a generated one, so this is the fallback
- Scope limit, understood before building: Whisper `transcribe` produces text in
  the audio's own language; Whisper `translate` **only ever outputs English**.
  So this delivers English subtitles for English audio, and Spanish subtitles
  only where the audio is already Spanish. It cannot produce Spanish subtitles
  for an English film — see MS-2.

### [ ] MS-2 — Ollama translation stage (English → Spanish, Latin American)

The half that makes MS-1 useful for this household, and the piece nothing
off-the-shelf provides.

Verified need, 2026-07-31: with the Bazarr profile correctly requesting `ea`
(Spanish Latino) rather than `es` (which resolves to Spain's variant on most
providers), a manual provider search for *Spider-Man: Across the Spider-Verse*
returned **23 candidate subtitles, all English, zero Latin American Spanish**.
The providers cannot serve this. Local translation is not a nicety here.

- Input: Whisper's timed English SRT. Output: same timings, Spanish text
- Ollama (already running, `ollama.dk.internal`, models on `/var/mnt/fast/ollama`)
- The differentiator: an LLM can be *instructed* on dialect — "Latin American
  Spanish, use ustedes never vosotros, no Castilian vocabulary". No subtitle
  provider can promise that. On 2026-07-30 a scraped `es` subtitle for
  Spider-Verse contained "tío" 14 times
- Design work required: SRT parsing, timing preservation across chunk
  boundaries, context-window chunking for feature-length transcripts, and a
  quality gate before anything replaces an existing subtitle
- Runs sequentially after MS-1, not concurrently — one model on the GPU at a time

### [ ] MS-3 — OpenSubtitles.com provider (cheap coverage win)

Bazarr's provider set was deliberately restricted to credential-free providers
during the 2026-07-31 build. That excluded OpenSubtitles.com, which holds by far
the largest Latin American Spanish catalogue. A free account would materially
improve `ea` coverage today, without waiting on MS-1/MS-2.

Decide: free tier (rate-limited) vs VIP. Then add credentials to Bazarr.

---

## Next

### [ ] MS-4 — Librarian agent

Recurring audit of the library, reporting rather than acting. Motivated by what
a single unplanned pass on 2026-07-30 turned up by accident:

- 4.1 GB of duplicate *Mad Max: Fury Road* (a third copy of Black & Chrome)
- A movie split across `CD1.avi` / `CD2.avi` that Radarr structurally cannot
  import (multi-part is unsupported)
- A folder containing artwork and no film at all
- 11 titles whose folder names did not match any metadata entry confidently

Nobody was looking for any of that. Scope: redundant encodes, integrity
failures, orphaned artwork, unmatched entries, oversized-for-value files.

**Weight this higher than it looks.** `media10` is the *only* copy of the media
library — its backup drive (WD-BC04E69J) is defective with 4,440 reallocated
sectors and pending RMA. Knowing what is actually on that disk, and that it is
healthy, matters more here than on a mirrored setup.

### [ ] MS-5 — Semantic library search

"The one where the guy can't form new memories" → *Memento*. Ollama plus
Jellyfin metadata over 309 films, embedded and queryable in natural language.
All components already run on this box.

### [ ] MS-6 — Recommender that learns from watch history

Explicitly *not* the streaming-service model, where ranking is driven by
licensing economics rather than taste.

**Prerequisite already satisfied (2026-07-31):** `bajura` and `fabi` are now
separate Jellyfin users, so watch history is two distinguishable streams rather
than one merged household signal. Every episode watched from this date forward
is training data. Starting that clock was the point of splitting the users.

### [ ] MS-7 — Media events onto ubiweave

Imports, failed grabs, disk pressure and scrub results posted to the bus, so the
media host reports into DKOS like every other node. Precedent exists:
`dk-notify-failure` already posts systemd unit failures to ubiweave.

---

## Blocked

### [!] MS-8 — 1337x and EZTV indexers

FlareSolverr v3.3.21 is deployed, healthy, and wired as a tagged indexer proxy
in Prowlarr. It reaches both sites, detects the CloudFlare challenge ("Just a
moment..."), and fails to solve it within 150 s. `/dev/shm` was raised from
podman's default 64 MB to 512 MB, which is a real fix worth keeping, and it did
not help.

This is a solver capability gap against current CloudFlare Turnstile, not a
configuration error. Routing through ares would likely be *worse* — datacenter
IPs draw harsher Turnstile treatment than residential.

Unblocks by itself if a newer FlareSolverr handles current challenges. Do not
spend more effort here. If TV coverage becomes a priority, a private tracker
invite is the better investment — no CloudFlare gate, better retention.

---

## Deferred decisions

- **Seeding policy.** qBittorrent has `max_ratio_enabled: false` and
  `max_seeding_time_enabled: false` — it seeds indefinitely and never removes
  anything. Fine at 7.9 TB free, but the torrent list grows without bound.
  Suggested: ratio 2.0 or 14 days, whichever first, action = *remove torrent but
  not files*. Safe because imports are hardlinks: removing the torrent drops one
  directory entry, and the library keeps the data.
- **Whisper model size.** `medium` recommended; revisit if GPU contention with
  gaming or Ollama becomes noticeable.
- **`es` as a fallback language.** Currently the profile is `en` + `ea` only, so
  where no Latin American subtitle exists the result is none rather than a
  Castilian one. Reconsider once MS-2 lands.
