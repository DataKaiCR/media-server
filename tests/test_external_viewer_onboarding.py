"""Iron-Grade tests for isolated external-viewer onboarding."""

from __future__ import annotations

import contextlib
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from external_viewer import cli as onboarding_cli
from external_viewer.client import (
    ExternalJellyfinClient,
    JellyseerrClient,
    MAX_RESPONSE_BYTES,
    OnboardingClientError,
    _RejectRedirect,
)
from external_viewer.config import (
    OnboardingConfig,
    OnboardingConfigError,
    ViewerCredential,
    create_attempt_config,
    create_onboarding_config,
    load_onboarding_config,
    load_viewer_credential,
)
from external_viewer.service import OnboardingError, apply, preflight
from jellyfin_policy.client import ClientError
from jellyfin_policy.config import PolicyConfig, load_config
from jellyfin_policy.policy import build_plan


ADMIN_ID = "a" * 32
EXISTING_ID = "b" * 32
NEW_ID = "c" * 32
FOLDERS = [
    {"CollectionType": "movies", "ItemId": "d" * 32},
    {"CollectionType": "tvshows", "ItemId": "e" * 32},
]


def unsafe_policy(*, administrator: bool = False) -> dict[str, object]:
    return {
        "IsAdministrator": administrator,
        "IsHidden": administrator,
        "EnableAllFolders": True,
        "EnabledFolders": [],
        "EnableContentDeletion": administrator,
        "EnableContentDeletionFromFolders": [],
        "EnableContentDownloading": True,
        "EnableSyncTranscoding": True,
        "EnableMediaConversion": True,
        "EnablePublicSharing": True,
        "EnableRemoteAccess": False,
        "EnableRemoteControlOfOtherUsers": True,
        "EnableSharedDeviceControl": True,
        "EnableLiveTvAccess": True,
        "EnableLiveTvManagement": True,
        "EnableAllChannels": True,
        "EnabledChannels": [],
        "EnableCollectionManagement": True,
        "EnableSubtitleManagement": True,
        "EnableLyricManagement": True,
        "EnableMediaPlayback": True,
        "EnablePlaybackRemuxing": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
        "MaxParentalRating": None,
        "BlockUnratedItems": [],
    }


class TemporaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.token = self.root / "jellyfin-api-key"
        self.token.write_text("a" * 32, encoding="ascii")
        self.token.chmod(0o600)
        self.policy_path = self.root / "jellyfin-policy.toml"
        self.policy_path.write_text(
            "version = 1\n"
            'base_url = "http://127.0.0.1:18096/jellyfin"\n'
            f'api_key_file = "{self.token}"\n'
            f'backup_dir = "{self.root / "policy-backups"}"\n'
            "[[users]]\n"
            'name = "private-existing"\n'
            'role = "external"\n',
            encoding="utf-8",
        )
        self.policy_path.chmod(0o600)
        self.credential_path = self.root / "credential.json"
        self.credential_path.write_text(
            json.dumps({"username": "private-new", "password": "A" * 24}),
            encoding="utf-8",
        )
        self.credential_path.chmod(0o600)
        self.jellyseerr_key = self.root / "jellyseerr-api-key"
        self.jellyseerr_key.write_text("b" * 67 + "=", encoding="ascii")
        self.jellyseerr_key.chmod(0o600)
        self.state = self.root / "onboarding-state"
        self.state.mkdir(mode=0o700)
        self.onboarding_path = self.root / "onboarding.toml"
        self.onboarding_path.write_text(
            "version = 1\n"
            f'jellyfin_policy_config = "{self.policy_path}"\n'
            f'credential_file = "{self.credential_path}"\n'
            'jellyseerr_base_url = "http://127.0.0.1:15055/api/v1"\n'
            f'jellyseerr_api_key_file = "{self.jellyseerr_key}"\n'
            f'state_dir = "{self.state}"\n',
            encoding="utf-8",
        )
        self.onboarding_path.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def policy(self) -> PolicyConfig:
        return load_config(self.policy_path)

    def onboarding(self) -> OnboardingConfig:
        return load_onboarding_config(self.onboarding_path)

    def credential(self) -> ViewerCredential:
        return load_viewer_credential(self.credential_path)

    def compliant_users(self) -> list[object]:
        rows: list[object] = [
            {
                "Id": ADMIN_ID,
                "Name": "private-admin",
                "HasPassword": True,
                "Policy": unsafe_policy(administrator=True),
            },
            {
                "Id": EXISTING_ID,
                "Name": "private-existing",
                "HasPassword": True,
                "Policy": unsafe_policy(),
            },
        ]
        desired = build_plan(self.policy(), rows, FOLDERS)[0].desired
        rows[1]["Policy"] = desired  # type: ignore[index]
        return rows


class ConfigTest(TemporaryTest):
    def test_loads_private_runtime_configuration_and_credential(self) -> None:
        config = self.onboarding()
        credential = self.credential()
        self.assertEqual(config.jellyseerr_base_url, "http://127.0.0.1:15055/api/v1")
        self.assertEqual(credential.username, "private-new")
        self.assertEqual(len(credential.password), 24)

    def test_rejects_public_unknown_external_and_git_state(self) -> None:
        self.onboarding_path.chmod(0o644)
        with self.assertRaisesRegex(OnboardingConfigError, "0600"):
            self.onboarding()
        self.onboarding_path.chmod(0o600)
        original = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            original.replace("version = 1", 'version = 1\nunexpected = "value"'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(OnboardingConfigError, "exactly"):
            self.onboarding()
        self.onboarding_path.write_text(
            original.replace("http://127.0.0.1:15055", "https://example.com"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(OnboardingConfigError, "loopback"):
            self.onboarding()
        worktree = self.root / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        private = worktree / "credential.json"
        private.write_bytes(self.credential_path.read_bytes())
        private.chmod(0o600)
        self.onboarding_path.write_text(
            original.replace(str(self.credential_path), str(private)),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(OnboardingConfigError, "outside Git"):
            self.onboarding()

    def test_rejects_unsafe_credential_shapes(self) -> None:
        for document in (
            {"username": "private-new"},
            {"username": "bad\nname", "password": "A" * 24},
            {"username": "private-new", "password": "short"},
            {"username": "private-new", "password": "A" * 12, "extra": True},
        ):
            with self.subTest(document=document):
                self.credential_path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(OnboardingConfigError):
                    self.credential()

    def test_stable_config_may_prompt_and_uses_fresh_attempt_directories(self) -> None:
        document = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            "\n".join(
                line for line in document.splitlines()
                if not line.startswith("credential_file =")
            ) + "\n",
            encoding="utf-8",
        )
        config = self.onboarding()
        self.assertIsNone(config.credential_file)
        first = create_attempt_config(config)
        second = create_attempt_config(config)
        self.assertNotEqual(first.state_dir, second.state_dir)
        self.assertEqual(first.state_dir.parent, self.state)
        self.assertEqual(first.state_dir.stat().st_mode & 0o777, 0o700)

    def test_setup_creates_private_stable_configuration(self) -> None:
        config_path = self.root / "operator-config" / "onboarding.toml"
        state_dir = self.root / "operator-state"
        create_onboarding_config(
            config_path,
            policy_config_file=self.policy_path,
            jellyseerr_base_url="http://127.0.0.1:15055/api/v1",
            jellyseerr_api_key_file=self.jellyseerr_key,
            state_dir=state_dir,
        )
        config = load_onboarding_config(config_path)
        self.assertIsNone(config.credential_file)
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(OnboardingConfigError, "already exists"):
            create_onboarding_config(
                config_path,
                policy_config_file=self.policy_path,
                jellyseerr_base_url="http://127.0.0.1:15055/api/v1",
                jellyseerr_api_key_file=self.jellyseerr_key,
                state_dir=state_dir,
            )

    def test_setup_refuses_missing_references_before_writing_config(self) -> None:
        config_path = self.root / "missing-reference" / "onboarding.toml"
        with self.assertRaisesRegex(OnboardingConfigError, "cannot read"):
            create_onboarding_config(
                config_path,
                policy_config_file=self.root / "missing-policy.toml",
                jellyseerr_base_url="http://127.0.0.1:15055/api/v1",
                jellyseerr_api_key_file=self.jellyseerr_key,
                state_dir=self.root / "unused-state",
            )
        self.assertFalse(config_path.exists())

    def test_setup_can_atomically_repair_an_existing_config(self) -> None:
        config_path = self.root / "repair-config" / "onboarding.toml"
        first_state = self.root / "first-state"
        second_state = self.root / "second-state"
        create_onboarding_config(
            config_path,
            policy_config_file=self.policy_path,
            jellyseerr_base_url="http://127.0.0.1:15055/api/v1",
            jellyseerr_api_key_file=self.jellyseerr_key,
            state_dir=first_state,
        )
        create_onboarding_config(
            config_path,
            policy_config_file=self.policy_path,
            jellyseerr_base_url="http://127.0.0.1:15055/api/v1",
            jellyseerr_api_key_file=self.jellyseerr_key,
            state_dir=second_state,
            replace_existing=True,
        )
        self.assertEqual(
            load_onboarding_config(config_path).state_dir, second_state
        )
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(config_path.parent.glob(".onboarding.toml.*")), [])

    def test_setup_does_not_change_an_existing_parent_directory_mode(self) -> None:
        shared_parent = self.root / "shared-config"
        shared_parent.mkdir(mode=0o755)
        state_dir = self.root / "separate-private-state"
        create_onboarding_config(
            shared_parent / "onboarding.toml",
            policy_config_file=self.policy_path,
            jellyseerr_base_url="http://127.0.0.1:15055/api/v1",
            jellyseerr_api_key_file=self.jellyseerr_key,
            state_dir=state_dir,
        )
        self.assertEqual(shared_parent.stat().st_mode & 0o777, 0o755)


class FakeResponse:
    def __init__(self, value: object | None, status: int = 200) -> None:
        self.status = status
        self.raw = b"" if value is None else json.dumps(value).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return

    def read(self, _limit: int = -1) -> bytes:
        return self.raw


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    def open(self, request: object, timeout: float) -> FakeResponse:
        del timeout
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected fake HTTP request")
        return self.responses.pop(0)


class ClientTest(TemporaryTest):
    def test_jellyfin_account_lifecycle_is_bounded_and_base_path_aware(self) -> None:
        client = ExternalJellyfinClient(
            "http://127.0.0.1:18096/jellyfin", self.token
        )
        responses = [
            FakeResponse({
                "Id": NEW_ID,
                "Policy": {"IsAdministrator": False},
            }),
            FakeResponse({
                "AccessToken": "t" * 32,
                "User": {"Id": NEW_ID},
            }),
            FakeResponse({"Items": copy.deepcopy(FOLDERS)}),
            FakeResponse(None, 204),
            FakeResponse([]),
            FakeResponse(None, 204),
        ]
        opener = FakeOpener(responses)
        with patch(
            "external_viewer.client.urllib.request.build_opener",
            return_value=opener,
        ):
            self.assertEqual(
                client.create_user("private-new", "A" * 24), NEW_ID
            )
            identifier, token = client.authenticate_user(
                "private-new", "A" * 24
            )
            self.assertEqual(identifier, NEW_ID)
            self.assertEqual(len(client.user_views(identifier, token)), 2)
            client.logout(token)
            self.assertEqual(client.public_users(), [])
            client.delete_user(identifier)
        paths = [request.full_url for request in opener.requests]
        self.assertEqual(paths[0], "http://127.0.0.1:18096/jellyfin/Users/New")
        self.assertTrue(all("private-new" not in path for path in paths))
        with self.assertRaisesRegex(OnboardingClientError, "identifier"):
            client.delete_user("../administrator")

    def test_jellyfin_rejects_oversized_lifecycle_response(self) -> None:
        client = ExternalJellyfinClient(
            "http://127.0.0.1:18096/jellyfin", self.token
        )
        oversized = FakeResponse(None)
        oversized.raw = b"X" * (MAX_RESPONSE_BYTES + 1)
        opener = FakeOpener([oversized])
        with patch(
            "external_viewer.client.urllib.request.build_opener",
            return_value=opener,
        ), self.assertRaisesRegex(OnboardingClientError, "safety limit"):
            client.public_users()

    def test_jellyseerr_request_only_import_and_login(self) -> None:
        client = JellyseerrClient(
            "http://127.0.0.1:15055/api/v1", self.jellyseerr_key
        )
        opener = FakeOpener([
            FakeResponse({"defaultPermissions": 32, "newPlexLogin": False}),
            FakeResponse({"results": []}),
            FakeResponse([{"id": NEW_ID, "username": "private-new"}]),
            FakeResponse([{"id": 3, "permissions": 32}], 201),
            FakeResponse(None, 204),
            FakeResponse({"permissions": 32}),
        ])
        with patch(
            "external_viewer.client.urllib.request.build_opener",
            return_value=opener,
        ):
            client.assert_safe_defaults()
            self.assertEqual(client.users(), [])
            self.assertEqual(len(client.available_jellyfin_users()), 1)
            self.assertEqual(client.import_jellyfin_user(NEW_ID), 3)
            client.delete_user(3)
            self.assertEqual(
                client.authenticate_user("private-new", "A" * 24), 32
            )
        self.assertEqual(len(opener.requests), 6)

    def test_clients_reject_redirects_before_forwarding_credentials(self) -> None:
        handler = _RejectRedirect()
        with self.assertRaisesRegex(ClientError, "redirects"):
            handler.http_error_302(object(), object(), 302, "Found", {})

    def test_jellyseerr_rejects_public_urls_tokens_and_timeouts(self) -> None:
        with self.assertRaisesRegex(OnboardingConfigError, "loopback"):
            JellyseerrClient("https://example.com/api/v1", self.jellyseerr_key)
        self.jellyseerr_key.chmod(0o644)
        with self.assertRaisesRegex(OnboardingClientError, "private regular"):
            JellyseerrClient(
                "http://127.0.0.1:15055/api/v1", self.jellyseerr_key
            )
        self.jellyseerr_key.chmod(0o600)
        for timeout in (True, 0, float("nan"), 121):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(
                OnboardingClientError, "timeout"
            ):
                JellyseerrClient(
                    "http://127.0.0.1:15055/api/v1",
                    self.jellyseerr_key,
                    timeout,
                )


class FakeJellyfin:
    def __init__(self, rows: list[object], password: str) -> None:
        self.rows = copy.deepcopy(rows)
        self.folder_rows = copy.deepcopy(FOLDERS)
        self.password = password
        self.deleted: list[str] = []
        self.update_count = 0

    def users(self) -> list[object]:
        return copy.deepcopy(self.rows)

    def virtual_folders(self) -> list[object]:
        return copy.deepcopy(self.folder_rows)

    def update_policy(self, user_id: object, policy: dict[str, object]) -> None:
        self.update_count += 1
        for row in self.rows:
            if row["Id"] == user_id:
                row["Policy"] = copy.deepcopy(policy)
                return
        raise RuntimeError("unknown fake user")

    def create_user(self, username: str, password: str) -> str:
        if password != self.password:
            raise RuntimeError("unexpected fake password")
        self.rows.append({
            "Id": NEW_ID,
            "Name": username,
            "HasPassword": True,
            "Policy": unsafe_policy(),
        })
        return NEW_ID

    def delete_user(self, user_id: object) -> None:
        self.deleted.append(str(user_id))
        self.rows = [row for row in self.rows if row["Id"] != user_id]

    def authenticate_user(self, username: str, password: str) -> tuple[str, str]:
        if password != self.password:
            raise RuntimeError("fake authentication failed")
        matches = [row for row in self.rows if row["Name"] == username]
        if len(matches) != 1:
            raise RuntimeError("fake authentication target mismatch")
        return str(matches[0]["Id"]), "viewer-token-value"

    def user_views(self, user_id: object, token: str) -> list[object]:
        if token != "viewer-token-value":
            raise RuntimeError("invalid fake viewer token")
        target = next(row for row in self.rows if row["Id"] == user_id)
        enabled = set(target["Policy"]["EnabledFolders"])
        return [row for row in self.folder_rows if row["ItemId"] in enabled]

    def logout(self, token: str) -> None:
        if token != "viewer-token-value":
            raise RuntimeError("invalid fake logout token")

    def public_users(self) -> list[object]:
        return [
            row for row in self.rows
            if row["Policy"].get("IsHidden") is not True
        ]


class FakeJellyseerr:
    def __init__(
        self,
        jellyfin: FakeJellyfin,
        *,
        commit_then_fail_import: bool = False,
        fail_auth: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.jellyfin = jellyfin
        self.rows: list[dict[str, object]] = [
            {
                "id": 1,
                "username": "private-admin",
                "jellyfinUsername": "private-admin",
                "permissions": 2,
                "userType": 3,
            },
            {
                "id": 2,
                "username": "private-existing",
                "jellyfinUsername": "private-existing",
                "permissions": 32,
                "userType": 3,
            },
        ]
        self.commit_then_fail_import = commit_then_fail_import
        self.fail_auth = fail_auth
        self.fail_delete = fail_delete
        self.deleted: list[int] = []

    def assert_safe_defaults(self) -> None:
        return

    def users(self) -> list[dict[str, object]]:
        return copy.deepcopy(self.rows)

    def available_jellyfin_users(self) -> list[dict[str, object]]:
        return [
            {"id": row["Id"], "username": row["Name"]}
            for row in self.jellyfin.rows
        ]

    def import_jellyfin_user(self, jellyfin_user_id: str) -> int:
        source = next(row for row in self.jellyfin.rows if row["Id"] == jellyfin_user_id)
        self.rows.append({
            "id": 3,
            "username": source["Name"],
            "jellyfinUsername": source["Name"],
            "permissions": 32,
            "userType": 3,
        })
        if self.commit_then_fail_import:
            raise RuntimeError("injected post-commit import failure")
        return 3

    def delete_user(self, user_id: int) -> None:
        if self.fail_delete:
            raise RuntimeError("injected delete failure")
        self.deleted.append(user_id)
        self.rows = [row for row in self.rows if row["id"] != user_id]

    def authenticate_user(self, username: str, password: str) -> int:
        if self.fail_auth or not any(
            row["username"] == username for row in self.rows
        ):
            raise RuntimeError("fake Jellyseerr authentication failed")
        return 32


class FakeVault:
    def __init__(self, password: str) -> None:
        self.password = password
        self.available: list[str] = []
        self.stored: list[ViewerCredential] = []

    def generate_credential(self, username: str) -> ViewerCredential:
        return ViewerCredential(username, self.password)

    def assert_available(self, username: str) -> None:
        self.available.append(username)

    def store(self, credential: ViewerCredential) -> None:
        self.stored.append(credential)


class CLITest(TemporaryTest):
    def fake_clients(self) -> tuple[FakeJellyfin, FakeJellyseerr]:
        jellyfin = FakeJellyfin(self.compliant_users(), self.credential().password)
        return jellyfin, FakeJellyseerr(jellyfin)

    def test_preflight_output_is_aggregate_only(self) -> None:
        jellyfin = FakeJellyfin(self.compliant_users(), self.credential().password)
        jellyseerr = FakeJellyseerr(jellyfin)
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = onboarding_cli.main(["--config", str(self.onboarding_path)])
        self.assertEqual(status, 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "preflight_passed")
        self.assertTrue(result["ready"])
        self.assertNotIn("private-new", output.getvalue())
        self.assertNotIn(NEW_ID, output.getvalue())

    def test_terminal_apply_output_is_human_readable_and_aggregate(self) -> None:
        jellyfin, jellyseerr = self.fake_clients()
        output = io.StringIO()
        with patch.object(
            onboarding_cli, "_human_output", return_value=True
        ), patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main([
                "--config", str(self.onboarding_path), "--apply"
            ])
        rendered = output.getvalue()
        self.assertEqual(status, 0)
        self.assertIn("External viewer onboarding", rendered)
        self.assertIn("[1/5] Revalidating safety conditions", rendered)
        self.assertIn("✓ Onboarding completed", rendered)
        self.assertIn("External viewers: 1 → 2", rendered)
        self.assertIn("Jellyseerr: login verified; request-only", rendered)
        self.assertNotIn("{", rendered)
        self.assertNotIn("private-new", rendered)
        self.assertNotIn(self.credential().password, rendered)
        self.assertNotIn(NEW_ID, rendered)

    def test_terminal_failure_reports_verified_rollback_without_identity(self) -> None:
        jellyfin = FakeJellyfin(
            self.compliant_users(), self.credential().password
        )
        jellyseerr = FakeJellyseerr(jellyfin, fail_auth=True)
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(
            onboarding_cli, "_human_output", return_value=True
        ), patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = onboarding_cli.main([
                "--config", str(self.onboarding_path), "--apply"
            ])
        rendered = output.getvalue()
        self.assertEqual(status, 1)
        self.assertIn("Importing and verifying Jellyseerr account... failed", rendered)
        self.assertIn("[rollback] Restoring and verifying prior state... done", rendered)
        self.assertIn("was rolled back", errors.getvalue())
        self.assertNotIn("private-new", rendered + errors.getvalue())
        self.assertNotIn(self.credential().password, rendered + errors.getvalue())
        self.assertNotIn(NEW_ID, rendered + errors.getvalue())

    def test_terminal_cancellation_explains_bitwarden_was_not_written(self) -> None:
        document = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            "\n".join(
                line for line in document.splitlines()
                if not line.startswith("credential_file =")
            ) + "\n",
            encoding="utf-8",
        )
        jellyfin, jellyseerr = self.fake_clients()
        vault = FakeVault(self.credential().password)
        output = io.StringIO()
        with patch.object(
            onboarding_cli, "_human_output", return_value=True
        ), patch.object(
            onboarding_cli, "_interactive", return_value=True
        ), patch("builtins.input", return_value="private-new"), patch.object(
            onboarding_cli, "_confirm_apply", return_value=False
        ), patch.object(
            onboarding_cli.BitwardenVault, "open", return_value=vault
        ), patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main([
                "--config",
                str(self.onboarding_path),
                "--bitwarden",
                "generate",
            ])
        rendered = output.getvalue()
        self.assertEqual(status, 0)
        self.assertEqual(vault.stored, [])
        self.assertIn("Generated a 32-character password", rendered)
        self.assertIn("Planned changes:", rendered)
        self.assertIn("No changes made.", rendered)
        self.assertIn("credential was not stored in Bitwarden", rendered)
        self.assertNotIn("private-new", rendered)
        self.assertNotIn(self.credential().password, rendered)

    def test_json_flag_overrides_terminal_presentation(self) -> None:
        args = onboarding_cli.parse_args(["--json"])
        with patch.object(onboarding_cli.sys.stdout, "isatty", return_value=True):
            self.assertFalse(onboarding_cli._human_output(args))

    def test_prompts_for_each_viewer_without_persisting_credentials(self) -> None:
        document = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            "\n".join(
                line for line in document.splitlines()
                if not line.startswith("credential_file =")
            ) + "\n",
            encoding="utf-8",
        )
        jellyfin, jellyseerr = self.fake_clients()
        output = io.StringIO()
        with patch.object(
            onboarding_cli, "_prompt_credential", return_value=self.credential()
        ) as prompt, patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main(["--config", str(self.onboarding_path)])
        self.assertEqual(status, 0)
        prompt.assert_called_once_with()
        self.assertFalse((self.root / "new-viewer.json").exists())
        self.assertNotIn("private-new", output.getvalue())

    def test_bitwarden_save_mode_uses_the_hidden_prompt(self) -> None:
        document = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            "\n".join(
                line for line in document.splitlines()
                if not line.startswith("credential_file =")
            ) + "\n",
            encoding="utf-8",
        )
        vault = FakeVault(self.credential().password)
        with patch.object(
            onboarding_cli, "_interactive", return_value=True
        ), patch.object(
            onboarding_cli, "_prompt_credential", return_value=self.credential()
        ) as prompt, patch.object(
            onboarding_cli.BitwardenVault, "open", return_value=vault
        ):
            credential, selected_vault = onboarding_cli._credential(
                self.onboarding(), "save"
            )
        prompt.assert_called_once_with()
        self.assertEqual(credential, self.credential())
        self.assertIs(selected_vault, vault)
        self.assertEqual(vault.available, ["private-new"])

    def test_bitwarden_generation_is_stored_after_preflight_and_recorded(self) -> None:
        document = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            "\n".join(
                line for line in document.splitlines()
                if not line.startswith("credential_file =")
            ) + "\n",
            encoding="utf-8",
        )
        jellyfin, jellyseerr = self.fake_clients()
        vault = FakeVault(self.credential().password)
        output = io.StringIO()
        with patch.object(
            onboarding_cli, "_interactive", return_value=True
        ), patch("builtins.input", return_value="private-new"), patch.object(
            onboarding_cli.BitwardenVault, "open", return_value=vault
        ), patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main([
                "--config",
                str(self.onboarding_path),
                "--apply",
                "--bitwarden",
                "generate",
            ])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["credential_storage"], "bitwarden")
        self.assertTrue(result["credentials_persisted"])
        self.assertTrue(result["account_names_persisted"])
        self.assertEqual(vault.available, ["private-new"])
        self.assertEqual(vault.stored, [self.credential()])
        self.assertNotIn(self.credential().password, output.getvalue())
        attempt = next(path for path in self.state.iterdir() if path.is_dir())
        receipt = json.loads(
            (attempt / "external-viewer-onboarding-receipt.json").read_text()
        )
        self.assertEqual(receipt["credential_storage"], "bitwarden")
        self.assertTrue(receipt["credentials_persisted"])

    def test_bitwarden_is_not_stored_when_preflight_fails(self) -> None:
        document = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            "\n".join(
                line for line in document.splitlines()
                if not line.startswith("credential_file =")
            ) + "\n",
            encoding="utf-8",
        )
        jellyfin, jellyseerr = self.fake_clients()
        jellyfin.rows[1]["Policy"]["EnableContentDownloading"] = True
        vault = FakeVault(self.credential().password)
        errors = io.StringIO()
        with patch.object(
            onboarding_cli, "_interactive", return_value=True
        ), patch("builtins.input", return_value="private-new"), patch.object(
            onboarding_cli.BitwardenVault, "open", return_value=vault
        ), patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stderr(errors):
            status = onboarding_cli.main([
                "--config",
                str(self.onboarding_path),
                "--apply",
                "--bitwarden",
                "generate",
            ])
        self.assertEqual(status, 1)
        self.assertEqual(vault.stored, [])
        self.assertNotIn("private-new", errors.getvalue())

    def test_apply_uses_a_fresh_private_attempt_directory(self) -> None:
        jellyfin, jellyseerr = self.fake_clients()
        output = io.StringIO()
        with patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main([
                "--config", str(self.onboarding_path), "--apply"
            ])
        self.assertEqual(status, 0)
        attempts = [path for path in self.state.iterdir() if path.is_dir()]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].stat().st_mode & 0o777, 0o700)
        self.assertTrue(
            (attempts[0] / "external-viewer-onboarding-receipt.json").exists()
        )

    def test_guided_preflight_reuses_prompted_credential_for_apply(self) -> None:
        document = self.onboarding_path.read_text(encoding="utf-8")
        self.onboarding_path.write_text(
            "\n".join(
                line for line in document.splitlines()
                if not line.startswith("credential_file =")
            ) + "\n",
            encoding="utf-8",
        )
        jellyfin, jellyseerr = self.fake_clients()
        output = io.StringIO()
        with patch.object(
            onboarding_cli, "_prompt_credential", return_value=self.credential()
        ) as prompt, patch.object(
            onboarding_cli, "_interactive", return_value=True
        ), patch.object(
            onboarding_cli, "_confirm_apply", return_value=True
        ), patch.object(
            onboarding_cli, "ExternalJellyfinClient", return_value=jellyfin
        ), patch.object(
            onboarding_cli, "JellyseerrClient", return_value=jellyseerr
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main(["--config", str(self.onboarding_path)])
        self.assertEqual(status, 0)
        prompt.assert_called_once_with()
        results = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [row.get("status") for row in results],
            ["preflight_passed", "completed"],
        )
        self.assertEqual([row.get("ready") for row in results], [True, False])
        self.assertTrue(results[1]["applied"])

    def test_hidden_prompt_requires_matching_passwords(self) -> None:
        with patch.object(
            onboarding_cli, "_interactive", return_value=True
        ), patch("builtins.input", return_value="private-new"), patch.object(
            onboarding_cli.getpass,
            "getpass",
            side_effect=["A" * 24, "B" * 24],
        ), self.assertRaisesRegex(OnboardingConfigError, "do not match"):
            onboarding_cli._prompt_credential()

    def test_setup_reprompts_for_an_unavailable_policy(self) -> None:
        with patch.object(onboarding_cli, "_interactive", return_value=True), patch(
            "builtins.input",
            side_effect=[str(self.root / "missing-policy"), str(self.policy_path)],
        ), contextlib.redirect_stderr(io.StringIO()):
            selected = onboarding_cli._prompt_policy(self.policy_path)
        self.assertEqual(selected, self.policy_path)

    def test_setup_writes_the_default_shape_without_a_credential_file(self) -> None:
        config_path = self.root / "accessible" / "onboarding.toml"
        state_dir = self.root / "setup-state"
        output = io.StringIO()
        answers = [
            str(self.policy_path),
            "http://127.0.0.1:15055/api/v1",
            str(self.jellyseerr_key),
            str(state_dir),
        ]
        with patch.object(onboarding_cli, "_interactive", return_value=True), patch(
            "builtins.input", side_effect=answers
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main([
                "--config", str(config_path), "--setup"
            ])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["configuration_created"])
        config = load_onboarding_config(config_path)
        self.assertIsNone(config.credential_file)

    def test_setup_repairs_an_existing_configuration(self) -> None:
        output = io.StringIO()
        with patch.object(onboarding_cli, "_interactive", return_value=True), patch(
            "builtins.input", side_effect=["", "", "", ""]
        ), contextlib.redirect_stdout(output):
            status = onboarding_cli.main([
                "--config", str(self.onboarding_path), "--setup"
            ])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["configuration_created"])
        self.assertTrue(result["configuration_updated"])
        self.assertIsNone(load_onboarding_config(self.onboarding_path).credential_file)

    def test_failure_does_not_print_private_path_or_identity(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        private_path = self.root / "private-person-name.toml"
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = onboarding_cli.main(["--config", str(private_path)])
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("private-person-name", errors.getvalue())


class ServiceTest(TemporaryTest):
    def clients(
        self,
        *,
        commit_then_fail_import: bool = False,
        fail_auth: bool = False,
        fail_delete: bool = False,
    ) -> tuple[FakeJellyfin, FakeJellyseerr]:
        jellyfin = FakeJellyfin(self.compliant_users(), self.credential().password)
        jellyseerr = FakeJellyseerr(
            jellyfin,
            commit_then_fail_import=commit_then_fail_import,
            fail_auth=fail_auth,
            fail_delete=fail_delete,
        )
        return jellyfin, jellyseerr

    def test_invalid_credential_storage_is_refused_before_mutation(self) -> None:
        jellyfin, jellyseerr = self.clients()
        before = jellyfin.users()
        with self.assertRaisesRegex(OnboardingError, "storage result"):
            apply(
                replace(
                    self.onboarding(), credential_storage="private-file"
                ),
                self.policy(),
                self.credential(),
                jellyfin,
                jellyseerr,
            )
        self.assertEqual(jellyfin.users(), before)

    def test_preflight_is_aggregate_only_and_does_not_mutate(self) -> None:
        jellyfin, jellyseerr = self.clients()
        before = jellyfin.users()
        result = preflight(
            self.policy(), self.credential(), jellyfin, jellyseerr
        )
        rendered = json.dumps(result, sort_keys=True)
        self.assertEqual(result["status"], "preflight_passed")
        self.assertTrue(result["ready"])
        self.assertEqual(result["configured_external_count"], 1)
        self.assertNotIn("private-existing", rendered)
        self.assertNotIn("private-new", rendered)
        self.assertNotIn(NEW_ID, rendered)
        self.assertEqual(jellyfin.users(), before)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_apply_is_backup_first_verified_and_request_only(self) -> None:
        jellyfin, jellyseerr = self.clients()
        events: list[tuple[str, str]] = []
        result = apply(
            self.onboarding(),
            self.policy(),
            self.credential(),
            jellyfin,
            jellyseerr,
            progress=lambda phase, status: events.append((phase, status)),
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["applied"])
        self.assertEqual(events, [
            ("rollback_state", "start"),
            ("rollback_state", "done"),
            ("jellyfin", "start"),
            ("jellyfin", "done"),
            ("jellyseerr", "start"),
            ("jellyseerr", "done"),
            ("receipt", "start"),
            ("receipt", "done"),
        ])
        self.assertEqual(result["credential_storage"], "memory")
        self.assertFalse(result["credentials_persisted"])
        self.assertFalse(result["account_names_persisted"])
        self.assertEqual(result["policy_accounts_updated"], 1)
        self.assertEqual(result["jellyseerr_permission"], 32)
        self.assertEqual(len(jellyfin.rows), 3)
        target = next(row for row in jellyfin.rows if row["Id"] == NEW_ID)
        self.assertTrue(target["Policy"]["IsHidden"])
        self.assertTrue(target["Policy"]["EnableRemoteAccess"])
        self.assertFalse(target["Policy"]["EnableContentDownloading"])
        self.assertEqual(len(jellyseerr.rows), 3)
        self.assertEqual(jellyseerr.rows[-1]["permissions"], 32)
        persisted = load_config(self.policy_path)
        self.assertEqual([row.role for row in persisted.users], ["external", "external"])
        prestate = self.state / "jellyfin-policy.pre.toml"
        receipt = self.state / "external-viewer-onboarding-receipt.json"
        self.assertEqual(prestate.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        output = receipt.read_text(encoding="utf-8")
        self.assertNotIn("private-new", output)
        self.assertNotIn(NEW_ID, output)
        self.assertEqual(
            (self.root / "policy-backups").stat().st_mode & 0o777, 0o700
        )

    def test_progress_renderer_failure_cannot_change_transaction(self) -> None:
        jellyfin, jellyseerr = self.clients()

        def broken_progress(_phase: str, _status: str) -> None:
            raise RuntimeError("presentation unavailable")

        result = apply(
            self.onboarding(),
            self.policy(),
            self.credential(),
            jellyfin,
            jellyseerr,
            progress=broken_progress,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(jellyfin.rows), 3)
        self.assertEqual(len(jellyseerr.rows), 3)

    def test_toml_escapes_private_username(self) -> None:
        credential = {"username": 'private"viewer', "password": "A" * 24}
        self.credential_path.write_text(json.dumps(credential), encoding="utf-8")
        jellyfin = FakeJellyfin(self.compliant_users(), "A" * 24)
        jellyseerr = FakeJellyseerr(jellyfin)
        apply(
            self.onboarding(), self.policy(), self.credential(), jellyfin, jellyseerr
        )
        self.assertEqual(load_config(self.policy_path).users[-1].name, 'private"viewer')

    def test_acknowledged_import_failure_rolls_back_both_applications(self) -> None:
        jellyfin, jellyseerr = self.clients(fail_auth=True)
        original_policy = self.policy_path.read_bytes()
        events: list[tuple[str, str]] = []
        with self.assertRaisesRegex(OnboardingError, "rolled back"):
            apply(
                self.onboarding(),
                self.policy(),
                self.credential(),
                jellyfin,
                jellyseerr,
                progress=lambda phase, status: events.append((phase, status)),
            )
        self.assertIn(("jellyseerr", "failed"), events)
        self.assertEqual(events[-2:], [("rollback", "start"), ("rollback", "done")])
        self.assertEqual(len(jellyfin.rows), 2)
        self.assertEqual(len(jellyseerr.rows), 2)
        self.assertEqual(self.policy_path.read_bytes(), original_policy)
        self.assertEqual(jellyfin.deleted, [NEW_ID])
        self.assertEqual(jellyseerr.deleted, [3])
        self.assertFalse(
            (self.state / "external-viewer-onboarding-receipt.json").exists()
        )

    def test_ambiguous_post_commit_import_is_a_hard_failure(self) -> None:
        jellyfin, jellyseerr = self.clients(commit_then_fail_import=True)
        with self.assertRaisesRegex(OnboardingError, "rollback was incomplete"):
            apply(
                self.onboarding(), self.policy(), self.credential(), jellyfin, jellyseerr
            )
        self.assertEqual(len(jellyfin.rows), 2)
        self.assertEqual(len(jellyseerr.rows), 3)
        self.assertEqual(jellyseerr.deleted, [])

    def test_incomplete_rollback_is_a_hard_failure(self) -> None:
        jellyfin, jellyseerr = self.clients(fail_auth=True, fail_delete=True)
        with self.assertRaisesRegex(OnboardingError, "rollback was incomplete"):
            apply(
                self.onboarding(), self.policy(), self.credential(), jellyfin, jellyseerr
            )

    def test_private_prestate_failure_prevents_account_creation(self) -> None:
        jellyfin, jellyseerr = self.clients()
        self.state.chmod(0o500)
        try:
            with self.assertRaisesRegex(OnboardingError, "private onboarding state"):
                apply(
                    self.onboarding(), self.policy(), self.credential(),
                    jellyfin, jellyseerr,
                )
        finally:
            self.state.chmod(0o700)
        self.assertEqual(len(jellyfin.rows), 2)
        self.assertEqual(len(jellyseerr.rows), 2)

    def test_refuses_drift_before_account_creation(self) -> None:
        jellyfin, jellyseerr = self.clients()
        jellyfin.rows[1]["Policy"]["EnableContentDownloading"] = True
        with self.assertRaisesRegex(OnboardingError, "policy has drift"):
            preflight(self.policy(), self.credential(), jellyfin, jellyseerr)
        self.assertEqual(len(jellyfin.rows), 2)


if __name__ == "__main__":
    unittest.main()
