# Bazarr Whisper Fallback

## Purpose

Whisper supplies generated English subtitles only after enabled human providers
fail to reach Bazarr's minimum score. It is not a peer to human providers and is
not an English-to-Spanish translator.

## Container settings

The Compose service uses:

- image `onerahmet/openai-whisper-asr-webservice:v1.9.1-gpu`;
- `faster_whisper` with model `medium`;
- CUDA float16 inference through NVIDIA CDI;
- an internal-only port on the media network;
- a persistent model cache;
- a configurable idle timeout that releases model memory.

The service does not mount the media library. Bazarr extracts mono 16 kHz PCM
and uploads it to the internal API.

## Bazarr settings

Configure the `whisperai` provider with:

| Setting | Value |
|---|---|
| Endpoint | `http://whisper:9000` |
| Connection timeout | `10` seconds |
| Transcription timeout | `7200` seconds |
| Pass video name | disabled |
| Log level | `INFO` |

Place `whisperai` last in the enabled provider list. Under **Subtitles → Whisper
As Fallback**, enable automated fallback. Leave single-series fallback disabled
unless interactive series-wide generation is explicitly desired.

Add `whisperai` to the automatic `ffsubsync` provider exclusion list. Whisper's
timestamps are derived from the same audio and should not be shifted by a
second alignment pass.

## Generated-output identification

Enable Bazarr custom post-processing with no score threshold and use:

```text
python3 /opt/media-scripts/mark-generated-subtitle.py --provider {{provider}} --subtitle {{subtitles}} --score {{score}} --manifest /config/generated-subtitles.jsonl
```

The script never edits subtitle content. For Whisper output it records:

- `user.media_server.generated=true`;
- `user.media_server.subtitle_source=whisperai`;
- a SHA-256 xattr;
- an append-only mode-`0600` JSONL record under mode-`0700` private Bazarr
  state.

The marker refuses subtitle and manifest symlinks, hashes a stable regular file,
and restores the exact prior xattr state if private manifest publication fails.
When a human provider later writes the same subtitle path, the script clears the
generated xattrs and fails loudly if marker removal is not possible. Bazarr
history remains the audit trail for both events.

## Validation

Before enabling automated fallback:

1. Confirm the Whisper container is healthy.
2. Fetch `/openapi.json` from Bazarr across the internal network.
3. Transcribe a short clip without publishing it.
4. Generate one full subtitle for media with no acceptable human result.
5. Validate UTF-8 decoding, cue count, positive durations, chronological order,
   non-overlap, final timestamp within runtime, Bazarr history, xattrs, manifest,
   and Jellyfin discovery.

## Rollback

1. Disable Bazarr's automated Whisper fallback.
2. Remove `whisperai` from enabled providers.
3. Stop the Whisper service.
4. Remove only generated subtitles confirmed by Bazarr history and matching
   manifest hash; never infer provenance from the filename alone.
5. Restore the pre-change Bazarr backup if configuration rollback is required.
