from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"


def _service_block(name: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9][a-zA-Z0-9_-]*:\n|\Z)",
        text,
    )
    if match is None:
        raise AssertionError(f"missing Compose service {name}")
    return match.group(0)


def _volume_rows(block: str) -> list[str]:
    match = re.search(r"(?m)^    volumes:\n(?P<rows>(?:      - [^\n]+\n)+)", block)
    if match is None:
        return []
    return [line.strip()[2:] for line in match.group("rows").splitlines()]


class ExternalMediaComposeTests(unittest.TestCase):
    def test_external_stateful_services_are_inert_and_have_no_host_ports(self) -> None:
        for name, expected_port in (
            ("jellyfin-external", "8096"),
            ("jellyseerr-external", "5055"),
        ):
            block = _service_block(name)
            self.assertIn('profiles: ["external"]', block)
            self.assertNotRegex(block, r"(?m)^    ports:")
            self.assertIn(f'expose: ["{expected_port}"]', block)
            self.assertIn("networks: [media]", block)
            self.assertIn('traefik.enable: "false"', block)

    def test_external_jellyfin_mounts_only_movies_and_tv_read_only(self) -> None:
        volumes = _volume_rows(_service_block("jellyfin-external"))
        media = [row for row in volumes if ":/data/media/" in row]

        self.assertEqual(
            set(media),
            {
                "${DATA}/media/movies:/data/media/movies:ro",
                "${DATA}/media/tv:/data/media/tv:ro",
            },
        )
        self.assertFalse(any(":/data:" in row or ":/data/media:" in row for row in volumes))
        self.assertFalse(any(f"/media/{name}:" in row for row in volumes for name in ("music", "books", "documentaries")))

    def test_external_application_state_and_transcodes_are_separate(self) -> None:
        jellyfin = _volume_rows(_service_block("jellyfin-external"))
        jellyseerr = _volume_rows(_service_block("jellyseerr-external"))

        self.assertIn("${APPDATA}/jellyfin-external:/config:Z", jellyfin)
        self.assertIn("${EXTERNAL_TRANSCODE}:/config/transcodes:Z", jellyfin)
        self.assertEqual(jellyseerr, ["${APPDATA}/jellyseerr-external:/app/config:Z"])
        self.assertNotIn("${APPDATA}/jellyfin:/config", jellyfin)
        self.assertNotIn("${APPDATA}/jellyseerr:/app/config", jellyseerr)

    def test_external_jellyfin_uses_pinned_image_and_gpu_without_discovery_ports(self) -> None:
        block = _service_block("jellyfin-external")

        self.assertIn("image: lscr.io/linuxserver/jellyfin:10.11.11", block)
        self.assertIn("- nvidia.com/gpu=all", block)
        self.assertIn('NVIDIA_VISIBLE_DEVICES: "all"', block)
        self.assertIn('NVIDIA_DRIVER_CAPABILITIES: "compute,video,utility"', block)
        self.assertNotIn("7359", block)
        self.assertNotIn("8920", block)

    def test_external_jellyseerr_is_pinned_without_direct_arr_mount(self) -> None:
        block = _service_block("jellyseerr-external")

        self.assertIn("image: docker.io/fallenbagel/jellyseerr:2.1.0", block)
        self.assertNotIn("depends_on:", block)
        self.assertNotIn("devices:", block)
        self.assertEqual(len(_volume_rows(block)), 1)

    def test_cine_pelencho_web_is_shared_digest_pinned_and_runtime_hardened(self) -> None:
        block = _service_block("cine-pelencho-web")

        self.assertNotIn("profiles:", block)
        self.assertNotRegex(block, r"(?m)^    ports:")
        self.assertIn('expose: ["8080"]', block)
        self.assertIn("networks: [media]", block)
        self.assertIn('traefik.enable: "false"', block)
        self.assertRegex(
            block,
            r"image: localhost/cine-pelencho-web@sha256:[0-9a-f]{64}",
        )
        self.assertIn("read_only: true", block)
        self.assertIn("cap_drop: [ALL]", block)
        self.assertIn("no-new-privileges:true", block)
        self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=32m", block)
        self.assertIn("http://127.0.0.1:8080/healthz", block)
        self.assertNotIn("volumes:", block)

    def test_external_transcode_path_is_explicit_in_environment_template(self) -> None:
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        assignments = dict(
            line.split("=", 1)
            for line in lines
            if line and not line.startswith("#") and "=" in line
        )

        self.assertEqual(assignments["EXTERNAL_TRANSCODE"], "/srv/media-transcodes/external")
        self.assertNotEqual(assignments["EXTERNAL_TRANSCODE"], assignments["TRANSCODE"])


if __name__ == "__main__":
    unittest.main()
