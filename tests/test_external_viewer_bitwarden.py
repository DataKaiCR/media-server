"""Tests for in-memory Bitwarden viewer credential storage."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from external_viewer.bitwarden import BitwardenError, BitwardenVault  # noqa: E402
from external_viewer.config import ViewerCredential  # noqa: E402


class FakeBitwarden:
    def __init__(self, *, initial_status: str = "locked") -> None:
        self.initial_status = initial_status
        self.items: list[dict[str, object]] = []
        self.calls: list[tuple[list[str], str | None, dict[str, str]]] = []

    def __call__(
        self,
        arguments: list[str],
        *,
        input: str | None = None,
        env: dict[str, str] | None = None,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(env or {})
        self.calls.append((list(arguments), input, environment))
        command = arguments[1:]
        if command == ["status"]:
            status = (
                "unlocked"
                if environment.get("BW_SESSION") == "S" * 40
                else self.initial_status
            )
            return subprocess.CompletedProcess(arguments, 0, json.dumps({"status": status}), "")
        if command == [
            "unlock",
            "--passwordenv",
            "CINE_PELENCHO_BW_PASSWORD",
            "--raw",
        ]:
            if environment.get("CINE_PELENCHO_BW_PASSWORD") != "master-secret":
                raise AssertionError("master password was not passed through the environment")
            return subprocess.CompletedProcess(arguments, 0, "S" * 40 + "\n", "")
        if command and command[0] == "generate":
            return subprocess.CompletedProcess(arguments, 0, "Aa1!" * 8 + "\n", "")
        if command == ["sync"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        if command[:2] == ["list", "items"]:
            return subprocess.CompletedProcess(arguments, 0, json.dumps(self.items), "")
        if command == ["create", "item"]:
            if input is None:
                raise AssertionError("encoded item was not provided on stdin")
            item = json.loads(base64.b64decode(input).decode("utf-8"))
            item["id"] = "private-vault-item-id"
            self.items.append(item)
            return subprocess.CompletedProcess(arguments, 0, json.dumps(item), "")
        raise AssertionError(f"unexpected Bitwarden command: {command}")


class BitwardenVaultTest(unittest.TestCase):
    def test_unlocks_generates_and_stores_without_secret_arguments(self) -> None:
        fake = FakeBitwarden()
        with patch(
            "external_viewer.bitwarden.shutil.which", return_value="/usr/bin/bw"
        ), patch(
            "external_viewer.bitwarden.getpass.getpass",
            return_value="master-secret",
        ), patch("external_viewer.bitwarden.subprocess.run", side_effect=fake):
            vault = BitwardenVault.open(interactive=True)
            credential = vault.generate_credential("private-new")
            vault.store(credential)

        self.assertEqual(credential.password, "Aa1!" * 8)
        self.assertEqual(len(fake.items), 1)
        stored_login = fake.items[0]["login"]
        self.assertEqual(stored_login["username"], credential.username)
        self.assertEqual(stored_login["password"], credential.password)
        rendered_arguments = "\n".join(
            " ".join(arguments) for arguments, _input, _env in fake.calls
        )
        self.assertNotIn(credential.username, rendered_arguments)
        self.assertNotIn(credential.password, rendered_arguments)
        self.assertNotIn("master-secret", rendered_arguments)
        create_call = next(
            row for row in fake.calls if row[0][1:] == ["create", "item"]
        )
        self.assertIsNotNone(create_call[1])
        self.assertTrue(
            all(
                row[2].get("BW_SESSION") == "S" * 40
                for row in fake.calls[2:]
            )
        )
        self.assertTrue(
            all(
                "CINE_PELENCHO_BW_PASSWORD" not in row[2]
                for index, row in enumerate(fake.calls)
                if index != 1
            )
        )

    def test_refuses_duplicate_exact_viewer_items(self) -> None:
        fake = FakeBitwarden(initial_status="unlocked")
        fake.items.append({
            "id": "existing",
            "name": "Cine Pelencho external viewer: private-new",
            "login": {"username": "private-new", "password": "A" * 24},
        })
        environment = {"BW_SESSION": "S" * 40}
        with patch.dict("os.environ", environment, clear=True), patch(
            "external_viewer.bitwarden.shutil.which", return_value="/usr/bin/bw"
        ), patch("external_viewer.bitwarden.subprocess.run", side_effect=fake):
            vault = BitwardenVault.open(interactive=False)
            with self.assertRaisesRegex(BitwardenError, "already contains"):
                vault.store(ViewerCredential("private-new", "B" * 24))
        self.assertEqual(len(fake.items), 1)

    def test_requires_login_and_a_terminal_for_unlock(self) -> None:
        for status, interactive, expected in (
            ("unauthenticated", True, "logged in"),
            ("locked", False, "no terminal"),
        ):
            with self.subTest(status=status), patch(
                "external_viewer.bitwarden.shutil.which", return_value="/usr/bin/bw"
            ), patch(
                "external_viewer.bitwarden.subprocess.run",
                side_effect=FakeBitwarden(initial_status=status),
            ), self.assertRaisesRegex(BitwardenError, expected):
                BitwardenVault.open(interactive=interactive)


if __name__ == "__main__":
    unittest.main()
