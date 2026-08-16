import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from acquisition_reconciliation.language_cli import main  # noqa: E402
from acquisition_reconciliation.language_config import (  # noqa: E402
    ConfigError,
    load_language_config,
)
from acquisition_reconciliation.language_probe import (  # noqa: E402
    canonical_language,
    map_media_path,
    probe_audio,
)
from acquisition_reconciliation.language_report import (  # noqa: E402
    build_language_report,
    publish_language_report,
)
from digital_librarian.bounded import BoundedProcessResult  # noqa: E402


RADARR_KEY = "PRIVATE-LANGUAGE-RADARR-KEY"
JELLYSEERR_KEY = "PRIVATE-LANGUAGE-JELLYSEERR-KEY"
PRIVATE_STREAM_TITLE = "PRIVATE-AUDIO-STREAM-TITLE"
PRIVATE_MEDIA_PATH = "/data/movies/private-test.mkv"


def request(movie_id, tmdb_id):
    return {
        "id": movie_id,
        "status": 2,
        "type": "movie",
        "createdAt": "2026-01-01T00:00:00Z",
        "media": {
            "mediaType": "movie",
            "externalServiceId": movie_id,
            "tmdbId": tmdb_id,
        },
        "requestedBy": {
            "username": "PRIVATE-REQUESTER",
            "email": "private@example.invalid",
            "jellyfinAuthToken": "PRIVATE-USER-TOKEN",
        },
    }


def movie(movie_id, tmdb_id, path, *, profile=4, original="English"):
    return {
        "id": movie_id,
        "tmdbId": tmdb_id,
        "title": f"Private Movie {movie_id}",
        "year": 2000 + movie_id,
        "hasFile": True,
        "qualityProfileId": profile,
        "originalLanguage": {"id": 1, "name": original},
        "movieFile": {"id": 100 + movie_id, "path": path, "size": 1000},
    }


def audio_probe(*streams):
    return {
        "status": "ok",
        "audio_stream_count": len(streams),
        "audio_streams": list(streams),
    }


def stream(language, *, latino=False, castilian=False, index=1):
    return {
        "index": index,
        "language_tag": language,
        "canonical_language": canonical_language(language),
        "channels": 2,
        "default": index == 1,
        "explicit_latino_marker": latino,
        "explicit_castilian_marker": castilian,
    }


class FakeLanguageHandler(BaseHTTPRequestHandler):
    requests = []
    host_path = None

    def log_message(self, format, *args):
        pass

    def _json(self, document):
        body = json.dumps(document).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        self.__class__.requests.append(("GET", parsed.path))
        key = self.headers.get("X-Api-Key")
        if parsed.path == "/api/v1/request" and key == JELLYSEERR_KEY:
            rows = [request(1, 1001)]
            query = parse_qs(parsed.query)
            take = int(query.get("take", ["100"])[0])
            skip = int(query.get("skip", ["0"])[0])
            self._json(
                {
                    "pageInfo": {"pages": 1, "pageSize": take, "results": 1, "page": 1},
                    "results": rows[skip : skip + take],
                }
            )
        elif parsed.path == "/api/v3/movie" and key == RADARR_KEY:
            self._json([movie(1, 1001, PRIVATE_MEDIA_PATH)])
        else:
            self.send_error(404)

    def do_POST(self):
        self.__class__.requests.append(("POST", urlsplit(self.path).path))
        self.send_error(405)


class FakeServer:
    def __enter__(self):
        FakeLanguageHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLanguageHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def __exit__(self, exc_type, exc, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class PostImportLanguageVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media_root = self.root / "media"
        self.movies = self.media_root / "movies"
        self.movies.mkdir(parents=True)
        self.reports = self.root / "reports"
        self.radarr_key = self.root / "radarr.toml"
        self.jellyseerr_key = self.root / "jellyseerr.toml"
        self.radarr_key.write_text(f"api_key = {json.dumps(RADARR_KEY)}\n", encoding="utf-8")
        self.jellyseerr_key.write_text(
            f"api_key = {json.dumps(JELLYSEERR_KEY)}\n", encoding="utf-8"
        )
        self.radarr_key.chmod(0o600)
        self.jellyseerr_key.chmod(0o600)
        self.sample = self.movies / "private-test.mkv"
        self.sample.write_bytes(b"synthetic-media")

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, port=1, *, extra="", mapping_extra=""):
        path = self.root / "config.toml"
        path.write_text(
            f'''version = 1
report_dir = {json.dumps(str(self.reports))}
page_size = 2
max_requests_per_instance = 100
max_radarr_movies = 100
max_files = 100
parser_timeout_seconds = 5
max_parser_output_bytes = 65536
max_parser_memory_bytes = 536870912
ffprobe_command = {json.dumps(shutil.which("ffprobe") or "/usr/bin/ffprobe")}
latino_profile_ids = [7]

[radarr]
url = "http://127.0.0.1:{port}"
api_key_file = {json.dumps(str(self.radarr_key))}
timeout_seconds = 5

[[jellyseerr]]
id = "standard"
url = "http://127.0.0.1:{port}"
api_key_file = {json.dumps(str(self.jellyseerr_key))}
timeout_seconds = 5

[[path_mapping]]
radarr_prefix = "/data"
host_root = {json.dumps(str(self.media_root))}
{mapping_extra}
{extra}''',
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def test_config_is_private_bounded_and_rejects_ambiguous_mappings(self):
        config = load_language_config(self.write_config())
        self.assertEqual(config.latino_profile_ids, frozenset({7}))
        self.assertEqual(config.path_mappings[0].host_root, self.media_root.resolve())

        self.radarr_key.chmod(0o640)
        with self.assertRaisesRegex(ConfigError, "mode 0600"):
            load_language_config(self.write_config())
        self.radarr_key.chmod(0o600)

        overlap = f'''\n[[path_mapping]]
radarr_prefix = "/data/movies"
host_root = {json.dumps(str(self.movies))}
'''
        with self.assertRaisesRegex(ConfigError, "must not overlap"):
            load_language_config(self.write_config(mapping_extra=overlap))

        invalid_profiles = self.write_config()
        invalid_profiles.write_text(
            invalid_profiles.read_text(encoding="utf-8").replace(
                "latino_profile_ids = [7]", "latino_profile_ids = [0]"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "positive bounded"):
            load_language_config(invalid_profiles)

    def test_path_mapping_refuses_missing_symlink_and_escape(self):
        config = load_language_config(self.write_config())
        mapped = map_media_path(PRIVATE_MEDIA_PATH, config.path_mappings)
        self.assertEqual(mapped, self.sample.resolve())
        with self.assertRaisesRegex(ValueError, "no configured"):
            map_media_path("/other/private.mkv", config.path_mappings)
        with self.assertRaisesRegex(ValueError, "regular non-symlink"):
            map_media_path("/data/movies/missing.mkv", config.path_mappings)

        target = self.movies / "target.mkv"
        target.write_bytes(b"target")
        link = self.movies / "link.mkv"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular non-symlink"):
            map_media_path("/data/movies/link.mkv", config.path_mappings)

        outside = self.root / "outside"
        outside.mkdir()
        (outside / "escaped.mkv").write_bytes(b"outside")
        parent_link = self.media_root / "escape"
        parent_link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            map_media_path("/data/escape/escaped.mkv", config.path_mappings)

    def test_probe_derives_latino_without_persisting_raw_stream_titles(self):
        config = load_language_config(self.write_config())
        payload = {
            "streams": [
                {
                    "index": 1,
                    "codec_type": "audio",
                    "channels": 6,
                    "tags": {"language": "spa", "title": f"Español Latino {PRIVATE_STREAM_TITLE}"},
                    "disposition": {"default": 1},
                },
                {
                    "index": 2,
                    "codec_type": "audio",
                    "channels": 2,
                    "tags": {"language": "spa", "title": "Castellano"},
                    "disposition": {"default": 0},
                },
            ]
        }
        result = BoundedProcessResult(0, json.dumps(payload).encode(), b"")
        with patch(
            "acquisition_reconciliation.language_probe.run_bounded", return_value=result
        ):
            evidence = probe_audio(self.sample, config)
        self.assertEqual(evidence["status"], "ok")
        self.assertTrue(evidence["audio_streams"][0]["explicit_latino_marker"])
        self.assertTrue(evidence["audio_streams"][1]["explicit_castilian_marker"])
        self.assertNotIn(PRIVATE_STREAM_TITLE, json.dumps(evidence))

    def test_probe_failure_and_stream_limits_fail_closed(self):
        config = load_language_config(self.write_config())
        cases = (
            (BoundedProcessResult(None, b"", b"", timed_out=True), "timeout"),
            (BoundedProcessResult(1, b"", b"", output_limited=True), "output_limit"),
            (BoundedProcessResult(None, b"", b"", unavailable=True), "unavailable"),
            (BoundedProcessResult(1, b"", b"private parser error"), "invalid_media"),
            (BoundedProcessResult(0, b"not-json", b""), "invalid_output"),
            (BoundedProcessResult(0, b"[]", b""), "invalid_output"),
            (
                BoundedProcessResult(
                    0,
                    json.dumps({"streams": [{"codec_type": "audio"}] * 129}).encode(),
                    b"",
                ),
                "stream_limit",
            ),
            (
                BoundedProcessResult(
                    0, json.dumps({"streams": [{"codec_type": "video"}]}).encode(), b""
                ),
                "no_audio",
            ),
        )
        for result, expected in cases:
            with self.subTest(expected=expected), patch(
                "acquisition_reconciliation.language_probe.run_bounded", return_value=result
            ):
                evidence = probe_audio(self.sample, config)
                self.assertEqual(evidence["status"], expected)
                self.assertEqual(evidence["audio_streams"], [])

    def test_regional_tag_is_latino_but_latin_language_code_is_not(self):
        config = load_language_config(self.write_config())
        payload = {
            "streams": [
                {"index": 1, "codec_type": "audio", "tags": {"language": "es-MX"}},
                {"index": 2, "codec_type": "audio", "tags": {"language": "lat"}},
            ]
        }
        result = BoundedProcessResult(0, json.dumps(payload).encode(), b"")
        with patch(
            "acquisition_reconciliation.language_probe.run_bounded", return_value=result
        ):
            evidence = probe_audio(self.sample, config)
        self.assertTrue(evidence["audio_streams"][0]["explicit_latino_marker"])
        self.assertFalse(evidence["audio_streams"][1]["explicit_latino_marker"])
        self.assertIsNone(evidence["audio_streams"][1]["canonical_language"])

    def test_language_categories_are_conservative_and_profile_aware(self):
        config = load_language_config(self.write_config())
        paths = {}
        for movie_id in range(1, 8):
            path = self.movies / f"movie-{movie_id}.mkv"
            path.write_bytes(f"movie-{movie_id}".encode())
            paths[movie_id] = f"/data/movies/movie-{movie_id}.mkv"
        movies = [
            movie(1, 1001, paths[1], original="English"),
            movie(2, 1002, paths[2], profile=7, original="English"),
            movie(3, 1003, paths[3], profile=7, original="English"),
            movie(4, 1004, paths[4], original="Italian"),
            movie(5, 1005, paths[5], original="PRIVATE-LANGUAGE"),
            movie(6, 1006, paths[6], original="English"),
            movie(7, 1007, paths[7], profile=7, original="English"),
        ]
        requests = {"standard": [request(i, 1000 + i) for i in range(1, 8)]}
        probes = [
            audio_probe(stream("eng")),
            audio_probe(stream("spa")),
            audio_probe(stream("spa", latino=True)),
            audio_probe(stream("eng")),
            audio_probe(stream("eng")),
            audio_probe(stream(None)),
            audio_probe(stream(None)),
        ]
        with patch(
            "acquisition_reconciliation.language_report.probe_audio", side_effect=probes
        ):
            report = build_language_report(config, requests, movies)
        by_id = {row["radarr_id"]: row for row in report["records"]}
        self.assertEqual(by_id[1]["category"], "original_verified")
        self.assertEqual(by_id[2]["category"], "generic_spanish_unverified")
        self.assertEqual(by_id[3]["category"], "latino_verified")
        self.assertEqual(by_id[4]["category"], "original_missing")
        self.assertEqual(by_id[5]["category"], "unsupported_original_language")
        self.assertEqual(by_id[6]["category"], "original_unverified")
        self.assertEqual(by_id[7]["category"], "latino_unverified")
        self.assertEqual(report["summary"]["verified_file_count"], 2)
        self.assertEqual(report["proposed_actions"], [])
        self.assertFalse(report["mutation"]["mutation_endpoints_available"])

    def test_source_change_invalidates_probe_evidence(self):
        config = load_language_config(self.write_config())
        movies = [movie(1, 1001, PRIVATE_MEDIA_PATH)]
        requests = {"standard": [request(1, 1001)]}

        def mutate(path, _config):
            path.write_bytes(b"changed-during-probe")
            return audio_probe(stream("eng"))

        with patch("acquisition_reconciliation.language_report.probe_audio", side_effect=mutate):
            report = build_language_report(config, requests, movies)
        self.assertEqual(report["records"][0]["category"], "source_changed")
        self.assertFalse(report["records"][0]["verified"])

    def test_real_ffprobe_reads_synthetic_media_without_mutating_it(self):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg and ffprobe are required")
        config = load_language_config(self.write_config())
        output = self.movies / "real.mkv"
        command = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x32:rate=1:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=spa",
            "-metadata:s:a:0",
            "title=Español Latino",
            "-y",
            str(output),
        ]
        subprocess.run(command, check=True, timeout=30)
        before = output.stat()
        evidence = probe_audio(output, config)
        after = output.stat()
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["audio_stream_count"], 1)
        self.assertTrue(evidence["audio_streams"][0]["explicit_latino_marker"])
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_end_to_end_cli_is_private_get_only_and_atomic(self):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            self.skipTest("ffmpeg and ffprobe are required")
        command = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=32x32:rate=1:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=eng",
            "-y",
            str(self.sample),
        ]
        subprocess.run(command, check=True, timeout=30)
        with FakeServer() as port:
            config_path = self.write_config(port)
            stdout = io.StringIO()
            with patch(
                "sys.argv", ["language-verification-audit", "--config", str(config_path)]
            ), contextlib.redirect_stdout(stdout):
                self.assertEqual(main(), 0)
        report_path = next(self.reports.glob("language-verification-*.json"))
        report_bytes = report_path.read_bytes()
        document = json.loads(report_bytes)
        self.assertEqual(stat.S_IMODE(self.reports.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
        self.assertEqual(document["summary"]["verified_file_count"], 1)
        self.assertNotIn(str(self.media_root), report_bytes.decode())
        self.assertNotIn(RADARR_KEY, report_bytes.decode())
        self.assertNotIn(JELLYSEERR_KEY, report_bytes.decode())
        self.assertTrue(all(method == "GET" for method, _ in FakeLanguageHandler.requests))
        self.assertNotIn("Private Movie", stdout.getvalue())

    def test_atomic_report_failure_leaves_no_stage_or_destination(self):
        with patch(
            "acquisition_reconciliation.language_report.os.replace",
            side_effect=OSError("failure"),
        ):
            with self.assertRaises(OSError):
                publish_language_report(self.reports, {"schema": "private-test"})
        self.assertEqual(list(self.reports.glob(".language-verification-*.tmp")), [])
        self.assertEqual(list(self.reports.glob("language-verification-*.json")), [])


if __name__ == "__main__":
    unittest.main()
