# Ollama Subtitle Translation

## Purpose and authority

This pipeline translates a validated English SRT into generated neutral Latin
American Spanish (`es-419`) while preserving cue identifiers and timestamp lines.
It never edits the English source and refuses to replace an unmarked or human
subtitle.

Generated output uses a `.es-MX.generated.srt` suffix. Jellyfin exposes it as a
clearly titled generated Spanish stream. Bazarr intentionally does not count
this filename as its human `ea` target, so searches and upgrades for an
acceptable human Latin American Spanish subtitle continue.

## Model selection

Runtime evaluation compared TranslateGemma 4B, Qwen 3 8B, and TranslateGemma
12B using structured subtitle batches. `translategemma:12b` was selected for its
better semantic accuracy, natural phrasing, structured-output compliance, and
speed. The deployed model is Q4_K_M and occupies about 7.6 GiB on disk.

A representative full pilot reached about 10.9 GiB of GPU memory on a 12 GiB
card. Whisper and translation therefore must not run concurrently. Configure
Ollama to admit one loaded model and one parallel inference request:

```text
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_QUEUE=4
```

Install the model in the existing trusted Ollama service after reviewing its
license terms:

```bash
ollama pull translategemma:12b
```

Do not expose an unauthenticated Ollama API to an untrusted network. The
translation client accepts only a loopback HTTP(S) origin without credentials,
path, query, or fragment, and enforces finite request timeouts and an 8 MiB
response ceiling.

## Translation workflow

Use the serialization wrapper rather than invoking the Python client directly:

```bash
scripts/run-subtitle-translation.sh \
  /srv/media/movie.en.srt \
  /srv/media/movie.es-MX.generated.srt \
  --runtime-seconds 5400 \
  --manifest /srv/private-state/generated-translations.jsonl
```

The wrapper:

1. takes an exclusive local GPU-job lock;
2. refuses to interrupt an active non-loopback Whisper request;
3. stops Bazarr before stopping Whisper, preventing a new fallback request;
4. runs the translation;
5. unloads the Ollama model;
6. restores Whisper to health before restarting Bazarr, even after failure.

Rootful container orchestration requires non-interactive `sudo`. For an
explicit repair of an existing generated destination, add both options:

```text
--replace --backup-dir /srv/private-backups/generated-subtitle-repairs
```

Replacement still fails unless the destination xattrs identify it as an Ollama
generated translation. Human and unmarked files are never accepted as repair
targets.

## Input validation

The parser reads at most 16 MiB, caps cue count and per-cue text, requires strict
UTF-8 SRT, and rejects:

- empty or oversized files, NUL bytes, malformed blocks, and invalid or
  pathologically large cue identifiers;
- duplicated or regressing identifiers and timestamp pairs;
- invalid timestamp components, zero/negative durations, and overlaps;
- cues beyond a finite, positive supplied media runtime;
- feature-length sources that end before the media midpoint;
- sources that do not appear to be English.

Derive `--runtime-seconds` from the exact media file with `ffprobe`; do not guess.
The source SHA-256 is captured before inference and rechecked immediately before
publication, preventing a concurrent source change from producing a mismatched
translation.

## Chunking and model contract

The default batch contains at most 24 cues or 3,500 source characters. Two cues
on either side are supplied as read-only context. Context is never included in
the returned cue set.

Ollama receives a JSON schema requiring one ordered `{id, text}` row for every
cue. Model inventories and response envelopes are size-bounded and strictly
validated before any values are consumed or persisted. Temperature is zero and
retries use a fixed, incrementing seed. A failed
attempt supplies only its validation reason to the next deterministic attempt.
After three failures, the complete operation stops without publication. Retry
counts and delays are validated before any model request.

Fully outer formatting tags are removed before inference and restored exactly.
Detected proper names are replaced with stable placeholders and restored after
translation. This avoids model-generated tag corruption and unintended
translation or misspelling of names.

## Output validation

Before publication, the completed translation must have:

- exactly the source cue count, order, identifiers, and timestamp lines;
- non-empty text for every cue and no model-generated timestamps;
- matching formatting tags, music notes, and effect delimiters;
- no `vosotros`, `vosotras`, or common Castilian second-person plural forms;
- plausible total text length and no suspicious truncation or expansion;
- a low unchanged-English ratio and strong Spanish lexical evidence;
- every protected proper name restored exactly;
- a final timestamp inside the supplied runtime.

Heuristics are fail-closed safety checks, not proof of literary quality. Review a
representative sample before treating a newly selected model or prompt as
production-ready.

## Atomic publication and provenance

The complete UTF-8 output is written and synchronized to a temporary file in the
destination directory, reparsed, revalidated, and given provenance xattrs before
an atomic rename. The private JSONL manifest is then synchronized. If manifest
publication fails, the destination is removed or restored from its protected
backup.

Recorded provenance includes source and output hashes, model tag and digest,
prompt version and hash, cue count, runtime bound, chunk metrics, and any backup
path. File xattrs record generated state, source/output hashes, model, provider,
and `es-419` target language. The manifest must remain private because it
contains media paths.

## Rollback

1. Verify the destination hash against the private manifest.
2. Remove the generated destination, or restore the recorded backup for an
   authorized generated-file repair.
3. Refresh Jellyfin metadata.
4. Leave the English source and any human subtitle untouched.
5. Keep Bazarr's human `ea` search enabled.
