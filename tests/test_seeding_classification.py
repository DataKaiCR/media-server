import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from seeding_evidence.classification import (  # noqa: E402
    build_classification_plan,
    enforce_classification,
)
from seeding_evidence.classification_cli import main  # noqa: E402
from seeding_evidence.client import ClientError  # noqa: E402
from seeding_evidence.config import load_config  # noqa: E402
from seeding_evidence.tag_client import QBittorrentTagClient  # noqa: E402


SECRET_PUBLIC_NAME = "PRIVATE CLASSIFICATION TITLE"
SECRET_PUBLIC_HASH = "a" * 40
SECRET_PRIVATE_HASH = "b" * 40
SECRET_CUSTOM_HASH = "c" * 40
SECRET_METADATA_HASH = "d" * 40
SECRET_CLASSIFIED_HASH = "e" * 40
SECRET_CONFLICT_HASH = "f" * 40


class FakeClassificationHandler(BaseHTTPRequestHandler):
    initial_torrents = [
        {
            "name": SECRET_PUBLIC_NAME,
            "hash": SECRET_PUBLIC_HASH,
            "tags": "",
            "total_size": 100,
            "state": "downloading",
            "private": False,
        },
        {
            "name": "private-source",
            "hash": SECRET_PRIVATE_HASH,
            "tags": "",
            "total_size": 100,
            "state": "downloading",
            "private": True,
        },
        {
            "name": "custom-tag",
            "hash": SECRET_CUSTOM_HASH,
            "tags": "operator-custom",
            "total_size": 100,
            "state": "stalledUP",
            "private": False,
        },
        {
            "name": "metadata-pending",
            "hash": SECRET_METADATA_HASH,
            "tags": "",
            "total_size": 0,
            "state": "metaDL",
            "private": False,
        },
        {
            "name": "already-classified",
            "hash": SECRET_CLASSIFIED_HASH,
            "tags": "seed-stewardship-90d",
            "total_size": 100,
            "state": "stalledUP",
            "private": False,
        },
        {
            "name": "conflicting",
            "hash": SECRET_CONFLICT_HASH,
            "tags": "seed-common-3d, seed-standard-3x-14d",
            "total_size": 100,
            "state": "stalledUP",
            "private": False,
        },
    ]
    torrents = []
    tags = set()
    requests = []

    @classmethod
    def reset(cls):
        cls.torrents = copy.deepcopy(cls.initial_torrents)
        cls.tags = {
            "seed-common-3d",
            "seed-standard-3x-14d",
            "seed-stewardship-90d",
        }
        cls.requests = []

    def log_message(self, format, *args):
        pass

    def _send(self, body=b"", content_type="text/plain", headers=None):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.__class__.requests.append(("GET", self.path))
        if self.path == "/api/v2/torrents/info":
            self._send(json.dumps(self.__class__.torrents), "application/json")
        elif self.path == "/api/v2/torrents/tags":
            self._send(json.dumps(sorted(self.__class__.tags)), "application/json")
        else:
            self.send_error(404)

    def do_POST(self):
        self.__class__.requests.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        fields = parse_qs(self.rfile.read(length).decode("ascii"))
        if self.path == "/api/v2/auth/login":
            self._send("Ok.", headers={"Set-Cookie": "SID=fake; Path=/"})
            return
        if self.path == "/api/v2/torrents/createTags":
            self.__class__.tags.update(fields.get("tags", [""])[0].split(","))
            self._send()
            return
        if self.path in {"/api/v2/torrents/addTags", "/api/v2/torrents/removeTags"}:
            hashes = set(fields.get("hashes", [""])[0].split("|"))
            tags = set(fields.get("tags", [""])[0].split(","))
            for torrent in self.__class__.torrents:
                if torrent["hash"] not in hashes:
                    continue
                existing = {
                    tag.strip() for tag in torrent["tags"].split(",") if tag.strip()
                }
                if self.path.endswith("addTags"):
                    existing.update(tags)
                else:
                    existing.difference_update(tags)
                torrent["tags"] = ", ".join(sorted(existing))
            self._send()
            return
        self.send_error(405)


class FakeServer:
    def __enter__(self):
        FakeClassificationHandler.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClassificationHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def __exit__(self, exc_type, exc, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RaceClient:
    def __init__(self, initial, verification):
        self.responses = [initial, [], verification, verification]
        self.created = []
        self.added = []
        self.removed = []

    def authenticate(self):
        pass

    def get_json(self, endpoint):
        return self.responses.pop(0)

    def create_tags(self, tags):
        self.created.append(tags)

    def add_tag(self, hashes, tag):
        self.added.append((hashes, tag))

    def remove_tag(self, hashes, tag):
        self.removed.append((hashes, tag))


class CommitThenFailClient(RaceClient):
    def __init__(self, initial):
        super().__init__(initial, [])
        self.responses = [initial, []]

    def add_tag(self, hashes, tag):
        self.added.append((hashes, tag))
        raise ClientError("simulated ambiguous transport failure")


class SeedingClassificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.credentials = self.root / "credentials.toml"
        self.credentials.write_text('username = "private-user"\npassword = "private-password"\n')
        self.credentials.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, port):
        path = self.root / "config.toml"
        path.write_text(
            f'''version = 1
report_dir = {json.dumps(str(self.root / "reports"))}

[qbittorrent]
url = "http://127.0.0.1:{port}"
credential_file = {json.dumps(str(self.credentials))}
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
''',
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def test_plan_is_conservative_and_aggregate_only(self):
        with FakeServer() as port:
            config = load_config(self.write_config(port))
            plan = build_classification_plan(
                config, copy.deepcopy(FakeClassificationHandler.torrents)
            )

        self.assertEqual(plan.torrent_count, 6)
        self.assertEqual(plan.already_classified_count, 1)
        self.assertEqual(plan.conflicting_policy_tag_count, 1)
        self.assertEqual(plan.unclassified_count, 4)
        self.assertEqual(plan.deferred_metadata_count, 1)
        self.assertEqual(plan.invalid_record_count, 0)
        self.assertEqual(plan.assignment_counts, {"standard": 2, "contributor": 1})
        rendered = json.dumps(plan.aggregate()) + repr(plan.assignments)
        self.assertNotIn(SECRET_PUBLIC_NAME, rendered)
        self.assertNotIn(SECRET_PUBLIC_HASH, rendered)
        self.assertNotIn(SECRET_PRIVATE_HASH, rendered)

    def test_report_only_does_not_call_tag_endpoints(self):
        with FakeServer() as port:
            config = load_config(self.write_config(port))
            outcome = enforce_classification(config, apply=False)

        self.assertEqual(outcome["mode"], "report-only")
        self.assertFalse(outcome["mutation_performed"])
        self.assertEqual(outcome["eligible_assignment_count"], 3)
        self.assertEqual(
            FakeClassificationHandler.requests,
            [
                ("POST", "/api/v2/auth/login"),
                ("GET", "/api/v2/torrents/info"),
            ],
        )

    def test_apply_creates_missing_tag_preserves_other_tags_and_is_idempotent(self):
        with FakeServer() as port:
            config = load_config(self.write_config(port))
            first = enforce_classification(config, apply=True)
            second = enforce_classification(config, apply=True)

        self.assertEqual(first["applied_count"], 3)
        self.assertEqual(first["applied_counts"], {"contributor": 1, "standard": 2})
        self.assertEqual(first["created_policy_tag_count"], 1)
        self.assertTrue(first["attention_required"])
        by_hash = {row["hash"]: row for row in FakeClassificationHandler.torrents}
        self.assertEqual(by_hash[SECRET_PUBLIC_HASH]["tags"], "seed-standard-3x-14d")
        self.assertEqual(by_hash[SECRET_PRIVATE_HASH]["tags"], "seed-contributor-5x-30d")
        self.assertEqual(
            by_hash[SECRET_CUSTOM_HASH]["tags"],
            "operator-custom, seed-standard-3x-14d",
        )
        self.assertEqual(by_hash[SECRET_METADATA_HASH]["tags"], "")
        self.assertIn("seed-stewardship-90d", by_hash[SECRET_CLASSIFIED_HASH]["tags"])
        self.assertEqual(second["applied_count"], 0)
        self.assertEqual(second["eligible_assignment_count"], 0)
        self.assertFalse(
            any(
                path.endswith(("delete", "pause", "setShareLimits"))
                for _, path in FakeClassificationHandler.requests
            )
        )

    def test_cli_output_never_contains_per_torrent_secrets(self):
        with FakeServer() as port:
            config_path = self.write_config(port)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--config", str(config_path), "--apply"])

        self.assertEqual(result, 2)
        rendered = stdout.getvalue() + stderr.getvalue()
        for secret in (
            SECRET_PUBLIC_NAME,
            SECRET_PUBLIC_HASH,
            SECRET_PRIVATE_HASH,
            "private-user",
            "private-password",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn('"applied_count": 3', rendered)

    def test_verification_conflict_rolls_back_only_attempted_tag(self):
        config = load_config(self.write_config(1))
        initial = [
            {
                "hash": SECRET_PUBLIC_HASH,
                "tags": "",
                "total_size": 100,
                "state": "stalledUP",
                "private": False,
            }
        ]
        verification = [
            {
                **initial[0],
                "tags": "seed-common-3d, seed-standard-3x-14d",
            }
        ]
        client = RaceClient(initial, verification)

        outcome = enforce_classification(config, apply=True, client=client)

        self.assertEqual(outcome["applied_count"], 0)
        self.assertEqual(outcome["verification_failure_count"], 1)
        self.assertTrue(outcome["attention_required"])
        self.assertEqual(
            client.removed,
            [((SECRET_PUBLIC_HASH,), "seed-standard-3x-14d")],
        )

    def test_ambiguous_transport_failure_rolls_back_attempted_batch(self):
        config = load_config(self.write_config(1))
        initial = [
            {
                "hash": SECRET_PUBLIC_HASH,
                "tags": "",
                "total_size": 100,
                "state": "stalledUP",
                "private": False,
            }
        ]
        client = CommitThenFailClient(initial)

        with self.assertRaisesRegex(ClientError, "rolled back"):
            enforce_classification(config, apply=True, client=client)

        self.assertEqual(
            client.removed,
            [((SECRET_PUBLIC_HASH,), "seed-standard-3x-14d")],
        )

    def test_unknown_metadata_and_invalid_records_are_never_assumed_public(self):
        config = load_config(self.write_config(1))
        plan = build_classification_plan(
            config,
            [
                {
                    "hash": SECRET_PUBLIC_HASH,
                    "tags": "",
                    "total_size": 100,
                    "state": "stalledUP",
                },
                {
                    "hash": "not-an-infohash",
                    "tags": "",
                    "total_size": 100,
                    "state": "stalledUP",
                    "private": False,
                },
                {"tags": None},
            ],
        )
        self.assertEqual(plan.deferred_metadata_count, 1)
        self.assertEqual(plan.invalid_record_count, 2)
        self.assertEqual(plan.assignments, ())

    def test_tag_client_rejects_non_tag_mutation_endpoint(self):
        config = load_config(self.write_config(1))
        client = QBittorrentTagClient(config.qbittorrent)
        with self.assertRaisesRegex(ClientError, "tag-mutation allowlist"):
            client._post_form("torrents/delete", {"hashes": SECRET_PUBLIC_HASH})


if __name__ == "__main__":
    unittest.main()
