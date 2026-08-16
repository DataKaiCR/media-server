import contextlib
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import acquisition_reconciliation.client as client_module  # noqa: E402
from acquisition_reconciliation.client import (  # noqa: E402
    ClientError,
    JellyseerrClient,
    RadarrClient,
)
from acquisition_reconciliation.cli import main  # noqa: E402
from acquisition_reconciliation.config import ConfigError, load_config  # noqa: E402
from acquisition_reconciliation.report import (  # noqa: E402
    build_report,
    previous_report,
    publish_report,
)


RADARR_KEY = "PRIVATE-RADARR-KEY"
STANDARD_KEY = "PRIVATE-STANDARD-KEY"
LATINO_KEY = "PRIVATE-LATINO-KEY"
PRIVATE_USER = "PRIVATE-REQUESTER-NAME"
PRIVATE_EMAIL = "private-requester@example.invalid"
PRIVATE_TOKEN = "PRIVATE-JELLYFIN-TOKEN"
PRIVATE_RELEASE = "PRIVATE-RELEASE-TITLE"
PRIVATE_DOWNLOAD_ID = "PRIVATE-DOWNLOAD-ID"
PRIVATE_PATH = "/private/media/path"


def media(radarr_id, tmdb_id, status=3):
    return {
        "mediaType": "movie",
        "externalServiceId": radarr_id,
        "tmdbId": tmdb_id,
        "status": status,
    }


def request(request_id, radarr_id, tmdb_id, created, status=2, request_type="movie"):
    return {
        "id": request_id,
        "status": status,
        "type": request_type,
        "createdAt": created,
        "media": media(radarr_id, tmdb_id),
        "requestedBy": {
            "username": PRIVATE_USER,
            "email": PRIVATE_EMAIL,
            "jellyfinAuthToken": PRIVATE_TOKEN,
        },
    }


def movie(movie_id, title, *, has_file=False, monitored=True, available=True, last_search=None):
    return {
        "id": movie_id,
        "title": title,
        "year": 2000 + movie_id,
        "tmdbId": 1000 + movie_id,
        "hasFile": has_file,
        "monitored": monitored,
        "isAvailable": available,
        "lastSearchTime": last_search,
        "qualityProfileId": 7 if movie_id == 6 else 4,
        "originalLanguage": {"id": 1, "name": "English"},
        "path": PRIVATE_PATH,
    }


class FakeAcquisitionHandler(BaseHTTPRequestHandler):
    requests = []
    standard_requests = [
        request(1, 1, 1001, "2025-12-01T00:00:00Z"),
        request(2, 2, 1002, "2025-12-02T00:00:00Z"),
        request(3, 3, 1003, "2025-12-03T00:00:00Z"),
        request(40, 4, 1004, "2025-12-04T00:00:00Z", status=3),
        request(41, 4, 1004, "2025-12-04T00:00:00Z", request_type="tv"),
        {
            "id": 42,
            "status": 2,
            "type": "movie",
            "createdAt": "2025-12-04T00:00:00Z",
            "media": {"mediaType": "movie"},
            "requestedBy": {"username": PRIVATE_USER, "jellyfinAuthToken": PRIVATE_TOKEN},
        },
    ]
    latino_requests = [
        request(101, 1, 1001, "2025-12-01T00:00:00Z"),
        request(104, 4, 1004, "2026-01-09T00:00:00Z"),
        request(105, 5, 1005, "2025-12-05T00:00:00Z"),
        request(106, 6, 1006, "2025-12-06T00:00:00Z"),
        request(107, 7, 1007, "2025-12-07T00:00:00Z"),
        request(108, 8, 1008, "2025-12-08T00:00:00Z"),
        request(109, 9, 1009, "2025-12-09T00:00:00Z"),
        request(110, 10, 1010, "2026-01-09T12:00:00Z"),
        request(111, 11, 1011, "PRIVATE-DATE"),
        request(199, 99, 1099, "2025-12-09T00:00:00Z"),
    ]
    movies = [
        movie(1, "Stale Movie", last_search="2026-01-01T00:00:00Z"),
        movie(2, "Active Movie", last_search="2026-01-01T00:00:00Z"),
        movie(3, "Available Movie", has_file=True),
        movie(4, "Recent Movie", last_search="2026-01-09T00:00:00Z"),
        movie(5, "Abandoned Grab"),
        movie(6, "Unmonitored Movie", monitored=False),
        movie(7, "Missing Imported File"),
        movie(8, "Future Movie", available=False),
        movie(9, "Never Searched"),
        movie(10, "New Unsearched"),
        movie(11, "Unknown Timing"),
    ]
    queue = [
        {
            "id": 900,
            "movieId": 2,
            "status": "downloading",
            "title": PRIVATE_RELEASE,
            "downloadId": PRIVATE_DOWNLOAD_ID,
        }
    ]
    history = [
        {
            "id": 800,
            "movieId": 5,
            "eventType": "grabbed",
            "date": "2025-12-10T00:00:00Z",
            "sourceTitle": PRIVATE_RELEASE,
            "downloadId": PRIVATE_DOWNLOAD_ID,
        },
        {
            "id": 801,
            "movieId": 7,
            "eventType": "downloadFolderImported",
            "date": "2025-12-11T00:00:00Z",
            "sourceTitle": PRIVATE_RELEASE,
            "data": {"droppedPath": PRIVATE_PATH},
        },
    ]

    def log_message(self, format, *args):
        pass

    def _send_json(self, document):
        body = json.dumps(document).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _paged(self, rows, query):
        page = int(query.get("page", ["1"])[0])
        size = int(query.get("pageSize", ["100"])[0])
        start = (page - 1) * size
        return {
            "page": page,
            "pageSize": size,
            "totalRecords": len(rows),
            "records": rows[start : start + size],
        }

    def do_GET(self):
        parsed = urlsplit(self.path)
        self.__class__.requests.append(("GET", parsed.path))
        query = parse_qs(parsed.query)
        key = self.headers.get("X-Api-Key")
        if parsed.path == "/api/v1/request":
            rows = self.standard_requests if key == STANDARD_KEY else self.latino_requests
            if key not in {STANDARD_KEY, LATINO_KEY}:
                self.send_error(401)
                return
            take = int(query.get("take", ["100"])[0])
            skip = int(query.get("skip", ["0"])[0])
            self._send_json(
                {
                    "pageInfo": {
                        "pages": (len(rows) + take - 1) // take,
                        "pageSize": take,
                        "results": len(rows),
                        "page": skip // take + 1,
                    },
                    "results": rows[skip : skip + take],
                }
            )
        elif parsed.path == "/api/v3/movie" and key == RADARR_KEY:
            self._send_json(self.movies)
        elif parsed.path == "/api/v3/queue" and key == RADARR_KEY:
            self._send_json(self._paged(self.queue, query))
        elif parsed.path == "/api/v3/history" and key == RADARR_KEY:
            self._send_json(self._paged(self.history, query))
        else:
            self.send_error(404)

    def do_POST(self):
        self.__class__.requests.append(("POST", urlsplit(self.path).path))
        self.send_error(405)


class FakeServer:
    def __enter__(self):
        FakeAcquisitionHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAcquisitionHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def __exit__(self, exc_type, exc, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class AcquisitionReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reports = self.root / "reports"
        self.keys = {}
        for name, key in (
            ("radarr", RADARR_KEY),
            ("standard", STANDARD_KEY),
            ("latino", LATINO_KEY),
        ):
            path = self.root / f"{name}.toml"
            path.write_text(f"api_key = {json.dumps(key)}\n", encoding="utf-8")
            path.chmod(0o600)
            self.keys[name] = path

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, port, *, url=None, extra=""):
        origin = url or f"http://127.0.0.1:{port}"
        path = self.root / "config.toml"
        path.write_text(
            f'''version = 1
report_dir = {json.dumps(str(self.reports))}
recent_search_hours = 72
page_size = 2
max_requests_per_instance = 100
max_radarr_movies = 100
max_queue_records = 100
max_history_records = 100

[radarr]
url = {json.dumps(origin)}
api_key_file = {json.dumps(str(self.keys["radarr"]))}
timeout_seconds = 5

[[jellyseerr]]
id = "standard"
url = {json.dumps(origin)}
api_key_file = {json.dumps(str(self.keys["standard"]))}
timeout_seconds = 5

[[jellyseerr]]
id = "latino"
url = {json.dumps(origin)}
api_key_file = {json.dumps(str(self.keys["latino"]))}
timeout_seconds = 5
{extra}''',
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def snapshot_and_requests(self, config):
        requests = {
            service.service_id: JellyseerrClient(service).approved_movie_requests(
                config.max_requests_per_instance, config.page_size
            )
            for service in config.jellyseerr
        }
        return requests, RadarrClient(config.radarr).snapshot(config)

    def test_end_to_end_private_report_is_bounded_chained_and_get_only(self):
        with FakeServer() as port:
            config_path = self.write_config(port)
            stdout = io.StringIO()
            with patch("sys.argv", ["acquisition-audit", "--config", str(config_path)]):
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(main(), 0)
            first_path = next(self.reports.glob("acquisition-reconciliation-*.json"))
            first_bytes = first_path.read_bytes()
            first = json.loads(first_bytes)

            self.assertEqual(stat.S_IMODE(self.reports.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(first_path.stat().st_mode), 0o600)
            self.assertEqual(first["mode"], "report-only")
            self.assertEqual(first["proposed_actions"], [])
            self.assertFalse(first["mutation"]["mutation_endpoints_available"])
            self.assertEqual(first["summary"]["malformed_request_count"], 1)
            self.assertGreater(first["summary"]["actionable_finding_count"], 0)
            self.assertNotIn("Stale Movie", stdout.getvalue())

            rendered = first_bytes.decode("utf-8")
            for secret in (
                RADARR_KEY,
                STANDARD_KEY,
                LATINO_KEY,
                PRIVATE_USER,
                PRIVATE_EMAIL,
                PRIVATE_TOKEN,
                PRIVATE_RELEASE,
                PRIVATE_DOWNLOAD_ID,
                PRIVATE_PATH,
            ):
                self.assertNotIn(secret, rendered)

            with patch("sys.argv", ["acquisition-audit", "--config", str(config_path)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 0)
            paths = sorted(self.reports.glob("acquisition-reconciliation-*.json"))
            second = json.loads(paths[-1].read_bytes())
            self.assertEqual(second["previous_report"]["name"], paths[0].name)

        self.assertTrue(FakeAcquisitionHandler.requests)
        self.assertTrue(all(method == "GET" for method, _ in FakeAcquisitionHandler.requests))
        self.assertEqual(
            {path for _, path in FakeAcquisitionHandler.requests},
            {"/api/v1/request", "/api/v3/movie", "/api/v3/queue", "/api/v3/history"},
        )

    def test_exact_reconciliation_categories_and_duplicate_sources(self):
        with FakeServer() as port:
            config = load_config(self.write_config(port))
            requests, snapshot = self.snapshot_and_requests(config)
        report = build_report(
            config,
            requests,
            snapshot,
            generated_at=dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc),
        )
        by_title = {row["title"]: row for row in report["records"] if row["title"]}
        self.assertEqual(by_title["Stale Movie"]["category"], "stale_no_grab")
        self.assertEqual(by_title["Active Movie"]["category"], "active_transfer")
        self.assertEqual(by_title["Available Movie"]["category"], "available")
        self.assertEqual(by_title["Recent Movie"]["category"], "recent_search_no_grab")
        self.assertEqual(by_title["Abandoned Grab"]["category"], "grabbed_without_file_or_queue")
        self.assertEqual(by_title["Unmonitored Movie"]["category"], "unmonitored_missing")
        self.assertEqual(by_title["Missing Imported File"]["category"], "file_missing_after_import")
        self.assertEqual(by_title["Future Movie"]["category"], "not_yet_available")
        self.assertEqual(by_title["Never Searched"]["category"], "never_searched")
        self.assertEqual(by_title["New Unsearched"]["category"], "recent_request_never_searched")
        self.assertEqual(by_title["Unknown Timing"]["category"], "unknown_timing_no_grab")
        self.assertEqual(by_title["Stale Movie"]["source_instances"], ["latino", "standard"])
        self.assertEqual(by_title["Stale Movie"]["source_request_count"], 2)
        self.assertEqual(report["summary"]["duplicate_request_count"], 1)
        missing = next(row for row in report["records"] if row["radarr_id"] == 99)
        self.assertEqual(missing["category"], "radarr_missing")

    def test_config_rejects_non_loopback_broad_secret_and_unknown_settings(self):
        with FakeServer() as port:
            with self.assertRaisesRegex(ConfigError, "loopback"):
                load_config(self.write_config(port, url="https://example.invalid:8080"))

            self.keys["radarr"].chmod(0o640)
            with self.assertRaisesRegex(ConfigError, "mode 0600"):
                load_config(self.write_config(port))
            self.keys["radarr"].chmod(0o600)

            self.keys["radarr"].write_text(
                f'api_key = {json.dumps(RADARR_KEY)}\nextra = "private"\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ConfigError, "only api_key"):
                load_config(self.write_config(port))
            self.keys["radarr"].write_text(
                f"api_key = {json.dumps(RADARR_KEY)}\n", encoding="utf-8"
            )

            path = self.write_config(port, extra="\nunknown = true\n")
            with self.assertRaises(ConfigError):
                load_config(path)

            path = self.write_config(port)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    str(self.reports), str(ROOT / ".private-acquisition-reports")
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "outside Git worktrees"):
                load_config(path)

    def test_config_rejects_duplicate_instances_and_unsafe_origins(self):
        with FakeServer() as port:
            duplicate = f'''\n[[jellyseerr]]
id = "standard"
url = "http://127.0.0.1:{port}"
api_key_file = {json.dumps(str(self.keys["standard"]))}
'''
            with self.assertRaisesRegex(ConfigError, "ids must be unique"):
                load_config(self.write_config(port, extra=duplicate))

        for url in (
            "http://127.0.0.1:bad",
            "http://127.0.0.1:5055/private",
            "http://127.0.0.1:5055?token=private",
            "http://user:password@127.0.0.1:5055",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ConfigError, "URL|port|origin"):
                load_config(self.write_config(1, url=url))

    def test_clients_reject_mutation_endpoints_without_network_request(self):
        config = load_config(self.write_config(1))
        clients = [RadarrClient(config.radarr), JellyseerrClient(config.jellyseerr[0])]
        for client, endpoint in ((clients[0], "command"), (clients[1], "request/1")):
            with self.subTest(endpoint=endpoint), patch.object(client.opener, "open") as opened:
                with self.assertRaisesRegex(ClientError, "read-only allowlist"):
                    client.get_json(endpoint)
                opened.assert_not_called()

    def test_clients_bound_response_bytes_json_shapes_and_record_counts(self):
        config = load_config(self.write_config(1))
        client = RadarrClient(config.radarr)

        class OversizedResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, maximum):
                return b"x" * maximum

        with patch.object(client_module, "MAX_RESPONSE_BYTES", 8), patch.object(
            client.opener, "open", return_value=OversizedResponse()
        ):
            with self.assertRaisesRegex(ClientError, "safety limit"):
                client.get_json("movie")
        with patch.object(client, "get_json", return_value={"not": "movies"}):
            with self.assertRaisesRegex(ClientError, "movie response shape"):
                client.movies(100)
        with patch.object(client, "get_json", return_value=[{}, {}]):
            with self.assertRaisesRegex(ClientError, "movie limit"):
                client.movies(1)

        jellyseerr = JellyseerrClient(config.jellyseerr[0])
        payload = {"pageInfo": {"results": 2}, "results": [{}, {}]}
        with patch.object(jellyseerr, "get_json", return_value=payload):
            with self.assertRaisesRegex(ClientError, "record limit"):
                jellyseerr.approved_movie_requests(1, 1)

    def test_threshold_is_recent_below_boundary_and_stale_at_boundary(self):
        config = load_config(self.write_config(1))
        requests = {
            "standard": [request(1, 1, 1001, "2026-01-01T00:00:00Z")],
            "latino": [],
        }
        base = movie(1, "Boundary Movie", last_search="2026-01-07T00:00:00Z")
        snapshot = {"movies": [base], "queue": [], "history": []}
        exact = build_report(
            config,
            requests,
            snapshot,
            generated_at=dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(exact["records"][0]["category"], "stale_no_grab")
        base["lastSearchTime"] = "2026-01-07T00:00:01Z"
        below = build_report(
            config,
            requests,
            snapshot,
            generated_at=dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(below["records"][0]["category"], "recent_search_no_grab")

    def test_previous_report_ignores_symlinks_and_untrusted_names(self):
        self.reports.mkdir(mode=0o700)
        legitimate = self.reports / "acquisition-reconciliation-20260101T000000.000000Z.json"
        legitimate.write_bytes(b"legitimate\n")
        secret = self.root / "private-target"
        secret.write_bytes(b"private")
        link = self.reports / "acquisition-reconciliation-20990101T000000.000000Z.json"
        link.symlink_to(secret)
        (self.reports / "acquisition-reconciliation-PRIVATE.json").write_bytes(b"private")
        evidence = previous_report(self.reports)
        self.assertEqual(evidence["name"], legitimate.name)

    def test_atomic_publication_cleans_stage_when_replace_fails(self):
        document = {"schema": "private-test"}
        with patch("acquisition_reconciliation.report.os.replace", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                publish_report(self.reports, document)
        self.assertEqual(list(self.reports.glob(".acquisition-reconciliation-*.tmp")), [])
        self.assertEqual(list(self.reports.glob("acquisition-reconciliation-*.json")), [])


if __name__ == "__main__":
    unittest.main()
