import contextlib
import hashlib
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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from seeding_evidence.client import ClientError, QBittorrentClient  # noqa: E402
from seeding_evidence.config import ConfigError, load_config  # noqa: E402
from seeding_evidence.cli import main  # noqa: E402


SECRET_NAME = "PRIVATE-TORRENT-TITLE"
SECRET_HASH = "0123456789abcdef-private-infohash"
SECRET_TRACKER = "https://tracker.invalid/announce?passkey=PRIVATE-PASSKEY"


class FakeQBittorrentHandler(BaseHTTPRequestHandler):
    requests = []
    now = 1_800_000_000

    torrents = [
        {
            "name": SECRET_NAME,
            "hash": SECRET_HASH,
            "tracker": SECRET_TRACKER,
            "tags": "seed-common-3d",
            "uploaded": 25_000,
            "downloaded": 1_000,
            "ratio": 25,
            "seeding_time": 4 * 86400,
            "total_size": 1_000,
            "progress": 1,
            "state": "uploading",
            "num_complete": 100,
        },
        {
            "name": "secret-standard",
            "hash": "secret-standard-hash",
            "tags": "seed-standard-3x-14d",
            "uploaded": 2_000,
            "downloaded": 1_000,
            "ratio": 2,
            "seeding_time": 20 * 86400,
            "total_size": 2_000,
            "progress": 1,
            "state": "stalledUP",
            "num_complete": 10,
        },
        {
            "name": "secret-contributor",
            "hash": "secret-contributor-hash",
            "tags": "seed-contributor-5x-30d",
            "uploaded": 6_000,
            "downloaded": 1_000,
            "ratio": 6,
            "seeding_time": 31 * 86400,
            "total_size": 3_000,
            "progress": 1,
            "state": "stalledUP",
            "num_complete": 2,
        },
        {
            "name": "secret-stewardship",
            "hash": "secret-stewardship-hash",
            "tags": "seed-stewardship-90d",
            "uploaded": 200,
            "downloaded": 1_000,
            "ratio": 0.2,
            "seeding_time": 100 * 86400,
            "total_size": 4_000,
            "progress": 1,
            "state": "stalledUP",
            "num_complete": 1,
        },
        {
            "name": "secret-untagged",
            "hash": "secret-untagged-hash",
            "tags": "",
            "uploaded": 1_000,
            "downloaded": 1_000,
            "ratio": 1,
            "seeding_time": 5 * 86400,
            "total_size": 5_000,
            "progress": 1,
            "state": "pausedUP",
            "num_complete": -1,
        },
        {
            "name": "secret-conflict",
            "hash": "secret-conflict-hash",
            "tags": "seed-standard-3x-14d, seed-contributor-5x-30d",
            "uploaded": 3_500,
            "downloaded": 1_000,
            "ratio": 3.5,
            "seeding_time": 40 * 86400,
            "total_size": 6_000,
            "progress": 1,
            "state": "stalledUP",
            "num_complete": 20,
        },
    ]

    def log_message(self, format, *args):
        pass

    def _send(self, body, content_type="application/json", headers=None):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.__class__.requests.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path == "/api/v2/auth/login":
            self._send("Ok.", "text/plain", {"Set-Cookie": "SID=fake; Path=/"})
        else:
            self.send_error(405)

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path))
        if self.path == "/api/v2/app/version":
            self._send("v5.0.4", "text/plain")
        elif self.path == "/api/v2/torrents/info":
            self._send(json.dumps(self.torrents))
        elif self.path == "/api/v2/sync/maindata?rid=0":
            self._send(json.dumps({
                "server_state": {
                    "alltime_ul": 100_000,
                    "alltime_dl": 20_000,
                    "connection_status": "connected",
                    "up_info_speed": 1_024,
                    "use_alt_speed_limits": False,
                }
            }))
        elif self.path == "/api/v2/app/preferences":
            self._send(json.dumps({"listen_port": 12_345, "up_limit": 10_000, "alt_up_limit": 5_000}))
        else:
            self.send_error(404)


class FakeServer:
    def __enter__(self):
        FakeQBittorrentHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeQBittorrentHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class SeedingEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reports = self.root / "reports"
        self.credentials = self.root / "credentials.toml"
        self.credentials.write_text('username = "private-user"\npassword = "private-password"\n')
        self.credentials.chmod(0o600)
        self.forwarded_port = self.root / "forwarded_port"
        self.forwarded_port.write_text("12345\n")

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, port, url=None, extra=""):
        config = self.root / "config.toml"
        config.write_text(
            f'''version = 1
report_dir = {json.dumps(str(self.reports))}

[qbittorrent]
url = {json.dumps(url or f"http://127.0.0.1:{port}")}
credential_file = {json.dumps(str(self.credentials))}
forwarded_port_file = {json.dumps(str(self.forwarded_port))}
timeout_seconds = 5

[[tiers]]
id = "common"
tag = "seed-common-3d"
minimum_days = 3
review_after_days = 3
protected = false

[[tiers]]
id = "standard"
tag = "seed-standard-3x-14d"
minimum_days = 14
target_ratio = 3.0
review_after_days = 30
protected = false

[[tiers]]
id = "contributor"
tag = "seed-contributor-5x-30d"
minimum_days = 30
target_ratio = 5.0
review_after_days = 90
protected = false

[[tiers]]
id = "stewardship"
tag = "seed-stewardship-90d"
minimum_days = 90
review_after_days = 365
protected = true
{extra}''',
            encoding="utf-8",
        )
        config.chmod(0o600)
        return config

    def test_end_to_end_report_is_private_aggregate_chained_and_read_only(self):
        with FakeServer() as port:
            config_path = self.write_config(port)
            stdout = io.StringIO()
            with patch("sys.argv", ["seeding-audit", "--config", str(config_path)]):
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(main(), 0)
            first_path = next(self.reports.glob("seeding-evidence-*.json"))
            first_bytes = first_path.read_bytes()
            first = json.loads(first_bytes)

            self.assertEqual(stat.S_IMODE(self.reports.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(first_path.stat().st_mode), 0o600)
            self.assertEqual(first["mode"], "report-only")
            self.assertEqual(first["proposed_actions"], [])
            self.assertEqual(first["summary"]["torrent_count"], 6)
            self.assertEqual(first["summary"]["unclassified_torrent_count"], 1)
            self.assertEqual(first["summary"]["conflicting_policy_tag_count"], 1)
            self.assertEqual(first["summary"]["low_swarm_evidence_count"], 2)
            self.assertEqual(first["summary"]["ratio_buckets"]["20-or-more"], 1)
            self.assertTrue(first["client"]["port_forwarding"]["client_port_matches"])
            tiers = {tier["id"]: tier for tier in first["tiers"]}
            self.assertEqual(tiers["common"]["policy_threshold_met_count"], 1)
            self.assertEqual(tiers["standard"]["below_ratio_target_count"], 1)
            self.assertEqual(tiers["contributor"]["policy_threshold_met_count"], 1)
            self.assertEqual(tiers["stewardship"]["policy_threshold_met_count"], 0)
            self.assertFalse(first["mutation"]["torrents_paused_or_deleted"])

            rendered = first_bytes.decode("utf-8")
            for secret in (
                SECRET_NAME,
                SECRET_HASH,
                SECRET_TRACKER,
                "private-user",
                "private-password",
                "secret-standard",
                "secret-contributor-hash",
            ):
                self.assertNotIn(secret, rendered)

            with patch("sys.argv", ["seeding-audit", "--config", str(config_path)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 0)
            paths = sorted(self.reports.glob("seeding-evidence-*.json"))
            self.assertEqual(len(paths), 2)
            second = json.loads(paths[-1].read_bytes())
            self.assertEqual(second["previous_report"]["name"], paths[0].name)
            self.assertEqual(
                second["previous_report"]["sha256"], hashlib.sha256(paths[0].read_bytes()).hexdigest()
            )

        self.assertEqual(
            set(FakeQBittorrentHandler.requests),
            {
                ("POST", "/api/v2/auth/login"),
                ("GET", "/api/v2/app/version"),
                ("GET", "/api/v2/torrents/info"),
                ("GET", "/api/v2/sync/maindata?rid=0"),
                ("GET", "/api/v2/app/preferences"),
            },
        )

    def test_rejects_non_loopback_url_and_broad_credentials(self):
        with FakeServer() as port:
            with self.subTest("non-loopback"):
                path = self.write_config(port, url="https://example.invalid:8080")
                with self.assertRaisesRegex(ConfigError, "loopback"):
                    load_config(path)
            with self.subTest("credential-mode"):
                path = self.write_config(port)
                self.credentials.chmod(0o640)
                with self.assertRaisesRegex(ConfigError, "mode 0600"):
                    load_config(path)
            with self.subTest("config-mode"):
                self.credentials.chmod(0o600)
                path = self.write_config(port)
                path.chmod(0o644)
                with self.assertRaisesRegex(ConfigError, "mode 0600"):
                    load_config(path)

    def test_rejects_protected_ratio_and_unknown_configuration(self):
        with FakeServer() as port:
            path = self.write_config(port, extra="target_ratio = 5.0\n")
            with self.assertRaisesRegex(ConfigError, "protected tiers"):
                load_config(path)

            path = self.write_config(port)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\nunknown = true\n")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_client_rejects_mutation_endpoint(self):
        with FakeServer() as port:
            config = load_config(self.write_config(port))
            client = QBittorrentClient(config.qbittorrent)
            with self.assertRaisesRegex(ClientError, "read-only allowlist"):
                client.get_bytes("torrents/delete")


if __name__ == "__main__":
    unittest.main()
