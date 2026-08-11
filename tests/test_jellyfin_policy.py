"""Iron-Grade tests for private least-privilege Jellyfin policy enforcement."""

from __future__ import annotations

import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from jellyfin_policy.client import ClientError, JellyfinClient, MAX_RESPONSE_BYTES
from jellyfin_policy.config import ConfigError, PolicyConfig, UserRule, load_config
from jellyfin_policy.policy import PolicyError, build_plan, public_summary
from jellyfin_policy.service import ApplyError, apply_plan


CLI_SPEC = importlib.util.spec_from_file_location(
    "jellyfin_policy_cli", ROOT / "scripts" / "jellyfin-policy.py"
)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
CLI = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(CLI)


ADMIN_ID = "a" * 32
HOUSEHOLD_ID = "b" * 32
RESTRICTED_ID = "c" * 32
GUEST_ID = "5" * 32
FOLDER_IDS = {
    "movies": "d" * 32,
    "tvshows": "e" * 32,
    "music": "f" * 32,
    "books": "1" * 32,
}


def unsafe_policy(*, administrator: bool = False, restricted: bool = False) -> dict[str, object]:
    policy: dict[str, object] = {
        "IsAdministrator": administrator,
        "IsHidden": True,
        "EnableAllFolders": True,
        "EnabledFolders": [],
        "EnableContentDeletion": administrator,
        "EnableContentDeletionFromFolders": [],
        "EnableContentDownloading": True,
        "EnableSyncTranscoding": True,
        "EnableMediaConversion": True,
        "EnablePublicSharing": True,
        "EnableRemoteAccess": True,
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": True,
        "EnableLiveTvAccess": True,
        "EnableLiveTvManagement": True,
        "EnableAllChannels": True,
        "EnabledChannels": [],
        "EnableCollectionManagement": False,
        "EnableSubtitleManagement": False,
        "EnableLyricManagement": False,
        "EnableMediaPlayback": True,
        "EnablePlaybackRemuxing": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
        "BlockUnratedItems": ["Movie", "Series", "Trailer"] if restricted else [],
    }
    if restricted:
        policy["MaxParentalRating"] = 10
    return policy


def users() -> list[object]:
    return [
        {"Id": ADMIN_ID, "Name": "private-admin", "Policy": unsafe_policy(administrator=True)},
        {"Id": HOUSEHOLD_ID, "Name": "private-adult", "Policy": unsafe_policy()},
        {
            "Id": RESTRICTED_ID,
            "Name": "private-child",
            "Policy": unsafe_policy(restricted=True),
        },
    ]


def folders() -> list[object]:
    return [
        {"CollectionType": kind, "ItemId": item_id}
        for kind, item_id in FOLDER_IDS.items()
    ]


def policy_config(root: Path) -> PolicyConfig:
    token = root / "api-key"
    token.write_text("a" * 32, encoding="ascii")
    token.chmod(0o600)
    return PolicyConfig(
        "http://127.0.0.1:8096",
        token,
        root / "backups",
        (
            UserRule("private-adult", "household"),
            UserRule("private-child", "restricted"),
        ),
    )


class TemporaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()


class ConfigTest(TemporaryTest):
    def write_config(self, extra: str = "", mode: int = 0o600) -> Path:
        token = self.root / "token"
        token.write_text("a" * 32, encoding="ascii")
        token.chmod(0o600)
        path = self.root / "policy.toml"
        path.write_text(
            "version = 1\n"
            'base_url = "http://127.0.0.1:8096"\n'
            f'api_key_file = "{token}"\n'
            f'backup_dir = "{self.root / "backups"}"\n'
            "[[users]]\n"
            'name = "private-adult"\n'
            'role = "household"\n'
            "[[users]]\n"
            'name = "private-child"\n'
            'role = "restricted"\n'
            + extra,
            encoding="utf-8",
        )
        path.chmod(mode)
        return path

    def test_loads_private_loopback_configuration(self) -> None:
        config = load_config(self.write_config())
        self.assertEqual(config.base_url, "http://127.0.0.1:8096")
        self.assertEqual([row.role for row in config.users], ["household", "restricted"])
        guest = self.write_config()
        guest.write_text(
            guest.read_text(encoding="utf-8").replace(
                'role = "household"', 'role = "guest"', 1
            ),
            encoding="utf-8",
        )
        self.assertEqual(load_config(guest).users[0].role, "guest")

    def test_rejects_public_symlink_external_and_unknown_configuration(self) -> None:
        public = self.write_config(mode=0o644)
        with self.assertRaisesRegex(ConfigError, "0600"):
            load_config(public)
        target = self.write_config()
        link = self.root / "linked.toml"
        link.symlink_to(target)
        with self.assertRaisesRegex(ConfigError, "non-symlink"):
            load_config(link)
        external = self.write_config()
        external.write_text(
            external.read_text(encoding="utf-8").replace(
                "http://127.0.0.1:8096", "https://example.com"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "loopback"):
            load_config(external)
        unknown = self.write_config(extra='unexpected = "value"\n')
        with self.assertRaisesRegex(ConfigError, "only name and role"):
            load_config(unknown)

    def test_rejects_private_state_inside_a_git_worktree(self) -> None:
        worktree = self.root / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        token = worktree / "token"
        token.write_text("a" * 32, encoding="ascii")
        token.chmod(0o600)
        config = worktree / "policy.toml"
        config.write_text(
            "version = 1\n"
            'base_url = "http://127.0.0.1:8096"\n'
            f'api_key_file = "{token}"\n'
            f'backup_dir = "{worktree / "backups"}"\n'
            "[[users]]\nname = \"viewer\"\nrole = \"household\"\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        with self.assertRaisesRegex(ConfigError, "outside Git"):
            load_config(config)

    def test_rejects_insecure_token_duplicate_users_and_bad_roles(self) -> None:
        insecure = self.write_config()
        config_text = insecure.read_text(encoding="utf-8")
        token_path = Path(next(
            line.split('"')[1] for line in config_text.splitlines()
            if line.startswith("api_key_file")
        ))
        token_path.chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "0600"):
            load_config(insecure)
        duplicate = self.write_config(
            '[[users]]\nname = "PRIVATE-ADULT"\nrole = "household"\n'
        )
        with self.assertRaisesRegex(ConfigError, "unique"):
            load_config(duplicate)
        bad_role = self.write_config()
        bad_role.write_text(
            bad_role.read_text(encoding="utf-8").replace(
                'role = "restricted"', 'role = "administrator"'
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "guest, household, or restricted"):
            load_config(bad_role)


class PolicyTest(TemporaryTest):
    def test_builds_exact_household_and_restricted_contracts(self) -> None:
        plan = build_plan(policy_config(self.root), users(), folders())
        by_role = {row.role: row for row in plan}
        adult = by_role["household"].desired
        child = by_role["restricted"].desired
        for policy in (adult, child):
            for field in (
                "EnableAllFolders", "EnableContentDeletion",
                "EnableContentDownloading", "EnableSyncTranscoding",
                "EnableMediaConversion", "EnablePublicSharing",
                "EnableRemoteAccess", "EnableSharedDeviceControl",
                "EnableLiveTvAccess", "EnableLiveTvManagement",
                "EnableAllChannels",
            ):
                self.assertFalse(policy[field], field)
            self.assertTrue(policy["EnableMediaPlayback"])
            self.assertTrue(policy["EnablePlaybackRemuxing"])
            self.assertFalse(policy["IsAdministrator"])
            self.assertFalse(policy["IsHidden"])
        self.assertEqual(set(adult["EnabledFolders"]), set(FOLDER_IDS.values()))
        self.assertEqual(
            set(child["EnabledFolders"]),
            {FOLDER_IDS["movies"], FOLDER_IDS["tvshows"]},
        )
        self.assertIsNone(adult["MaxParentalRating"])
        self.assertEqual(adult["BlockUnratedItems"], [])
        self.assertEqual(child["MaxParentalRating"], 10)
        self.assertEqual(child["BlockUnratedItems"], ["Movie", "Trailer", "Series"])

    def test_guest_can_use_music_but_not_books_without_parental_ceiling(self) -> None:
        config = policy_config(self.root)
        config = PolicyConfig(
            config.base_url,
            config.api_key_file,
            config.backup_dir,
            (*config.users, UserRule("private-guest", "guest")),
        )
        rows = users() + [
            {"Id": GUEST_ID, "Name": "private-guest", "Policy": unsafe_policy()}
        ]
        plan = build_plan(config, rows, folders())
        guest = next(row for row in plan if row.role == "guest")
        self.assertEqual(
            set(guest.desired["EnabledFolders"]),
            {FOLDER_IDS["movies"], FOLDER_IDS["tvshows"], FOLDER_IDS["music"]},
        )
        self.assertNotIn(FOLDER_IDS["books"], guest.desired["EnabledFolders"])
        self.assertIsNone(guest.desired["MaxParentalRating"])
        self.assertEqual(guest.desired["BlockUnratedItems"], [])
        self.assertEqual(public_summary(plan)["role_counts"]["guest"], 1)

    def test_allows_multiple_folders_per_type_but_rejects_duplicate_ids(self) -> None:
        rows = folders() + [{"CollectionType": "movies", "ItemId": "4" * 32}]
        plan = build_plan(policy_config(self.root), users(), rows)
        adult = next(row for row in plan if row.role == "household")
        child = next(row for row in plan if row.role == "restricted")
        self.assertIn("4" * 32, adult.desired["EnabledFolders"])
        self.assertIn("4" * 32, child.desired["EnabledFolders"])
        duplicate = folders() + [
            {"CollectionType": "movies", "ItemId": FOLDER_IDS["movies"]}
        ]
        with self.assertRaisesRegex(PolicyError, "folder identifier"):
            build_plan(policy_config(self.root), users(), duplicate)
        malformed = folders()
        malformed[0] = {"CollectionType": "movies", "ItemId": "-" * 32}
        with self.assertRaisesRegex(PolicyError, "folder identifier"):
            build_plan(policy_config(self.root), users(), malformed)

    def test_fails_closed_on_account_or_library_drift(self) -> None:
        config = policy_config(self.root)
        with self.assertRaisesRegex(PolicyError, "enumerate every"):
            build_plan(config, users()[:-1], folders())
        extra = users() + [{"Id": "2" * 32, "Name": "unknown", "Policy": unsafe_policy()}]
        with self.assertRaisesRegex(PolicyError, "enumerate every"):
            build_plan(config, extra, folders())
        with self.assertRaisesRegex(PolicyError, "collection types"):
            build_plan(config, users(), folders()[:-1])
        malformed = users()
        malformed[1] = {"Id": HOUSEHOLD_ID, "Name": "private-adult", "Policy": []}
        with self.assertRaisesRegex(PolicyError, "incomplete user"):
            build_plan(config, malformed, folders())

    def test_requires_exactly_one_administrator(self) -> None:
        rows = users()
        rows[0]["Policy"]["IsAdministrator"] = False
        with self.assertRaisesRegex(PolicyError, "exactly one"):
            build_plan(policy_config(self.root), rows, folders())
        rows = users() + [
            {"Id": "3" * 32, "Name": "second-admin", "Policy": unsafe_policy(administrator=True)}
        ]
        with self.assertRaisesRegex(PolicyError, "exactly one"):
            build_plan(policy_config(self.root), rows, folders())

    def test_summary_is_aggregate_only(self) -> None:
        plan = build_plan(policy_config(self.root), users(), folders())
        rendered = json.dumps(public_summary(plan), sort_keys=True)
        self.assertNotIn("private-adult", rendered)
        self.assertNotIn("private-child", rendered)
        self.assertNotIn(HOUSEHOLD_ID, rendered)
        self.assertEqual(public_summary(plan)["accounts_requiring_change"], 2)


class FakeClient:
    def __init__(
        self,
        user_rows: list[object],
        folder_rows: list[object],
        fail_calls: set[int] | None = None,
        commit_then_fail_calls: set[int] | None = None,
    ):
        self.user_rows = copy.deepcopy(user_rows)
        self.folder_rows = copy.deepcopy(folder_rows)
        self.fail_calls = fail_calls or set()
        self.commit_then_fail_calls = commit_then_fail_calls or set()
        self.calls = 0

    def users(self) -> list[object]:
        return copy.deepcopy(self.user_rows)

    def virtual_folders(self) -> list[object]:
        return copy.deepcopy(self.folder_rows)

    def update_policy(self, user_id: object, policy: dict[str, object]) -> None:
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RuntimeError("injected update failure")
        for row in self.user_rows:
            if row["Id"] == user_id:
                row["Policy"] = copy.deepcopy(policy)
                if self.calls in self.commit_then_fail_calls:
                    raise RuntimeError("injected post-commit transport failure")
                return
        raise RuntimeError("unknown user")


class CLITest(TemporaryTest):
    def invoke(self, client: FakeClient, *extra: str) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(CLI, "load_config", return_value=policy_config(self.root)), patch.object(
            CLI, "JellyfinClient", return_value=client
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = CLI.main(["--config", str(self.root / "private.toml"), *extra])
        return status, output.getvalue(), errors.getvalue()

    def test_audit_exit_codes_and_output_are_aggregate_only(self) -> None:
        client = FakeClient(users(), folders())
        status, output, errors = self.invoke(client)
        self.assertEqual(status, 2)
        self.assertEqual(errors, "")
        self.assertFalse(json.loads(output)["compliant"])
        self.assertNotIn("private-adult", output)
        apply_plan(client, policy_config(self.root), client.users(), client.virtual_folders())
        status, output, errors = self.invoke(client)
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output)["compliant"])
        self.assertEqual(errors, "")

    def test_apply_mode_and_sanitized_configuration_failure(self) -> None:
        client = FakeClient(users(), folders())
        status, output, errors = self.invoke(client, "--apply")
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output)["applied"])
        self.assertEqual(errors, "")
        output_buffer = io.StringIO()
        error_buffer = io.StringIO()
        with patch.object(
            CLI, "load_config", side_effect=ConfigError("private state rejected")
        ), contextlib.redirect_stdout(output_buffer), contextlib.redirect_stderr(error_buffer):
            status = CLI.main(["--config", str(self.root / "private-name.toml")])
        self.assertEqual(status, 1)
        self.assertEqual(output_buffer.getvalue(), "")
        self.assertNotIn("private-name", error_buffer.getvalue())


class ServiceTest(TemporaryTest):
    def test_apply_is_backup_first_private_and_verified(self) -> None:
        config = policy_config(self.root)
        client = FakeClient(users(), folders())
        result = apply_plan(client, config, client.users(), client.virtual_folders())
        backups = list(config.backup_dir.glob("jellyfin-policies-*.json"))
        self.assertTrue(result["applied"])
        self.assertEqual(result["updated_account_count"], 2)
        self.assertEqual(len(backups), 1)
        self.assertEqual(config.backup_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertIn("private-adult", backups[0].read_text(encoding="utf-8"))
        self.assertFalse(any(row.changed_fields for row in build_plan(
            config, client.users(), client.virtual_folders()
        )))

    def test_failure_rolls_back_every_applied_policy(self) -> None:
        config = policy_config(self.root)
        original = users()
        client = FakeClient(original, folders(), fail_calls={2})
        with self.assertRaisesRegex(ApplyError, "rolled back"):
            apply_plan(client, config, client.users(), client.virtual_folders())
        self.assertEqual(client.user_rows, original)
        self.assertEqual(len(list(config.backup_dir.glob("*.json"))), 1)

    def test_transport_failure_after_remote_commit_is_rolled_back(self) -> None:
        config = policy_config(self.root)
        original = users()
        client = FakeClient(
            original, folders(), commit_then_fail_calls={1}
        )
        with self.assertRaisesRegex(ApplyError, "rolled back"):
            apply_plan(client, config, client.users(), client.virtual_folders())
        self.assertEqual(client.user_rows, original)

    def test_incomplete_rollback_is_a_hard_failure(self) -> None:
        config = policy_config(self.root)
        client = FakeClient(users(), folders(), fail_calls={2, 3})
        with self.assertRaisesRegex(ApplyError, "rollback was incomplete"):
            apply_plan(client, config, client.users(), client.virtual_folders())
        self.assertGreaterEqual(client.calls, 3)

    def test_symlink_backup_directory_is_refused_before_api_mutation(self) -> None:
        base = policy_config(self.root)
        target = self.root / "target"
        target.mkdir()
        linked = self.root / "linked-backups"
        linked.symlink_to(target, target_is_directory=True)
        config = PolicyConfig(
            base.base_url, base.api_key_file, linked, base.users
        )
        client = FakeClient(users(), folders())
        with self.assertRaisesRegex(ApplyError, "regular directory"):
            apply_plan(client, config, client.users(), client.virtual_folders())
        self.assertEqual(client.calls, 0)
        self.assertEqual(list(target.iterdir()), [])

    def test_backup_publication_failure_prevents_api_mutation_and_cleans_stage(self) -> None:
        config = policy_config(self.root)
        client = FakeClient(users(), folders())
        with patch("jellyfin_policy.service.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(ApplyError, "backup"):
                apply_plan(client, config, client.users(), client.virtual_folders())
        self.assertEqual(client.calls, 0)
        self.assertEqual(list(config.backup_dir.glob("*.tmp")), [])

    def test_compliant_state_is_a_noop_without_backup(self) -> None:
        config = policy_config(self.root)
        client = FakeClient(users(), folders())
        apply_plan(client, config, client.users(), client.virtual_folders())
        compliant_client = FakeClient(client.users(), folders())
        backup_count = len(list(config.backup_dir.glob("*.json")))
        result = apply_plan(
            compliant_client, config,
            compliant_client.users(), compliant_client.virtual_folders(),
        )
        self.assertFalse(result["applied"])
        self.assertFalse(result["backup_created"])
        self.assertEqual(compliant_client.calls, 0)
        self.assertEqual(len(list(config.backup_dir.glob("*.json"))), backup_count)


class APIHandler(BaseHTTPRequestHandler):
    users_payload: object = users()
    folders_payload: object = folders()
    posts: list[tuple[str, object]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        payload = self.users_payload if self.path == "/Users" else self.folders_payload
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.posts.append((self.path, payload))
        self.send_response(204)
        self.end_headers()


class ClientTest(TemporaryTest):
    def setUp(self) -> None:
        super().setUp()
        APIHandler.users_payload = users()
        APIHandler.folders_payload = folders()
        APIHandler.posts = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.root / "token"
        self.token.write_text("a" * 32, encoding="ascii")
        self.token.chmod(0o600)
        self.client = JellyfinClient(
            f"http://127.0.0.1:{self.server.server_port}", self.token
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def test_real_loopback_get_and_policy_post(self) -> None:
        self.assertEqual(len(self.client.users()), 3)
        self.assertEqual(len(self.client.virtual_folders()), 4)
        self.client.update_policy(HOUSEHOLD_ID, {"EnableRemoteAccess": False})
        self.assertEqual(len(APIHandler.posts), 1)
        self.assertEqual(APIHandler.posts[0][0], f"/Users/{HOUSEHOLD_ID}/Policy")
        self.assertNotIn("a" * 32, json.dumps(APIHandler.posts[0][1]))

    def test_rejects_external_origins_unsafe_tokens_and_invalid_timeouts(self) -> None:
        with self.assertRaisesRegex(ClientError, "loopback"):
            JellyfinClient("https://example.com", self.token)
        linked = self.root / "linked-token"
        linked.symlink_to(self.token)
        with self.assertRaisesRegex(ClientError, "cannot read"):
            JellyfinClient("http://127.0.0.1:8096", linked)
        self.token.chmod(0o644)
        with self.assertRaisesRegex(ClientError, "private regular"):
            JellyfinClient("http://127.0.0.1:8096", self.token)
        self.token.chmod(0o600)
        for timeout in (True, 0, float("nan"), 121):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                ClientError, "timeout"
            ):
                JellyfinClient("http://127.0.0.1:8096", self.token, timeout)

    def test_rejects_malformed_shapes_invalid_ids_and_oversized_responses(self) -> None:
        APIHandler.users_payload = {"Users": []}
        with self.assertRaisesRegex(ClientError, "invalid shape"):
            self.client.users()
        for identifier in ("../admin", "-" * 32):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(
                ClientError, "identifier"
            ):
                self.client.update_policy(identifier, {})
        APIHandler.users_payload = "X" * (MAX_RESPONSE_BYTES + 1)
        with self.assertRaisesRegex(ClientError, "safety limit"):
            self.client.users()


if __name__ == "__main__":
    unittest.main()
