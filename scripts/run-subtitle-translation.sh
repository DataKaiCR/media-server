#!/usr/bin/env bash
# Serialize an Ollama translation against Bazarr's GPU Whisper fallback.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
lock_file=${SUBTITLE_GPU_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/media-subtitle-gpu.lock}
whisper_was_running=false
bazarr_was_running=false
whisper_stopped=false
bazarr_stopped=false

for command in flock python3 sudo; do
  command -v "$command" >/dev/null || {
    echo "required command not found: $command" >&2
    exit 2
  }
done
sudo -n true 2>/dev/null || {
  echo "passwordless sudo is required for rootful container orchestration" >&2
  exit 2
}

exec 9>"$lock_file"
flock -n 9 || {
  echo "another subtitle GPU job holds the serialization lock" >&2
  exit 3
}

container_running() {
  sudo -n podman inspect "$1" --format '{{.State.Running}}' 2>/dev/null | grep -qx true
}

restore_services() {
  status=$?
  trap - EXIT INT TERM
  set +e
  if $whisper_was_running && $whisper_stopped; then
    sudo -n podman start whisper >/dev/null
    for _ in $(seq 1 90); do
      health=$(sudo -n podman inspect whisper --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null)
      [[ $health == healthy || $health == running ]] && break
      sleep 2
    done
    if [[ $health != healthy && $health != running ]]; then
      echo "Whisper did not recover after translation" >&2
      [[ $status -ne 0 ]] || status=5
    fi
  fi
  if $bazarr_was_running && $bazarr_stopped; then
    sudo -n podman start bazarr >/dev/null || {
      echo "Bazarr did not restart after translation" >&2
      [[ $status -ne 0 ]] || status=5
    }
  fi
  exit "$status"
}
trap restore_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if container_running whisper; then
  whisper_was_running=true
  # A non-loopback established client means an ASR request is in flight. Abort
  # rather than interrupting generated-subtitle publication.
  if sudo -n podman exec whisper awk \
    '$2 ~ /:2328$/ && $4 == "01" && $3 !~ /^0100007F:/ {found=1} END {exit !found}' \
    /proc/net/tcp 2>/dev/null; then
    echo "Whisper is processing a request; translation was not started" >&2
    exit 4
  fi
fi
if container_running bazarr; then
  bazarr_was_running=true
  sudo -n podman stop -t 60 bazarr >/dev/null
  bazarr_stopped=true
fi
if $whisper_was_running; then
  sudo -n podman stop -t 60 whisper >/dev/null
  whisper_stopped=true
fi

python3 "$script_dir/translate-subtitle.py" "$@"
