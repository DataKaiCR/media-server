"""Bounded in-memory Bitwarden credential storage for viewer onboarding."""

from __future__ import annotations

import base64
import getpass
import json
import os
import shutil
import subprocess
from typing import Any

from .config import ViewerCredential, viewer_credential


_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_ITEM_PREFIX = "Cine Pelencho external viewer: "
_ITEM_NOTES = (
    "External Cine Pelencho viewer and request-only Jellyseerr account. "
    "Requests require operator approval; network access is managed separately."
)


class BitwardenError(RuntimeError):
    """Bitwarden is unavailable or did not preserve the exact credential."""


def _json(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise BitwardenError(f"Bitwarden returned invalid {label}") from error


class BitwardenVault:
    """An unlocked CLI session kept only in this process environment."""

    def __init__(self, executable: str, environment: dict[str, str]) -> None:
        self.executable = executable
        self.environment = environment

    @classmethod
    def open(cls, *, interactive: bool) -> "BitwardenVault":
        executable = shutil.which("bw")
        if executable is None:
            raise BitwardenError("Bitwarden CLI is not installed")
        vault = cls(executable, os.environ.copy())
        status = vault._status()
        if status == "unauthenticated":
            raise BitwardenError("Bitwarden CLI must be logged in before onboarding")
        if status == "locked":
            if not interactive:
                raise BitwardenError("Bitwarden is locked and no terminal is available")
            vault._unlock()
        if vault._status() != "unlocked":
            raise BitwardenError("Bitwarden did not unlock")
        return vault

    def _run(self, *arguments: str, input_data: str | None = None) -> str:
        try:
            result = subprocess.run(
                [self.executable, *arguments],
                input=input_data,
                capture_output=True,
                text=True,
                env=self.environment,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise BitwardenError("Bitwarden operation failed") from error
        if result.returncode != 0:
            raise BitwardenError("Bitwarden operation failed")
        if len(result.stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise BitwardenError("Bitwarden output exceeded the safety limit")
        return result.stdout

    def _status(self) -> str:
        document = _json(self._run("status"), "status")
        status = document.get("status") if isinstance(document, dict) else None
        if status not in {"locked", "unlocked", "unauthenticated"}:
            raise BitwardenError("Bitwarden returned an invalid status")
        return str(status)

    def _unlock(self) -> None:
        master_password = getpass.getpass("Bitwarden master password: ")
        if not master_password:
            raise BitwardenError("Bitwarden unlock failed")
        unlock_environment = dict(self.environment)
        unlock_environment["CINE_PELENCHO_BW_PASSWORD"] = master_password
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "unlock",
                    "--passwordenv",
                    "CINE_PELENCHO_BW_PASSWORD",
                    "--raw",
                ],
                capture_output=True,
                text=True,
                env=unlock_environment,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise BitwardenError("Bitwarden unlock failed") from error
        finally:
            master_password = ""
            unlock_environment.pop("CINE_PELENCHO_BW_PASSWORD", None)
        session = result.stdout.strip() if result.returncode == 0 else ""
        if not 20 <= len(session) <= 4096 or any(
            character.isspace() for character in session
        ):
            raise BitwardenError("Bitwarden unlock failed")
        self.environment["BW_SESSION"] = session

    def generate_credential(self, username: str) -> ViewerCredential:
        password = self._run(
            "generate",
            "--uppercase",
            "--lowercase",
            "--number",
            "--special",
            "--length",
            "32",
            "--minNumber",
            "2",
            "--minSpecial",
            "2",
            "--ambiguous",
        ).strip()
        return viewer_credential(username, password)

    @staticmethod
    def _item_name(username: str) -> str:
        return _ITEM_PREFIX + username

    def _viewer_items(self) -> list[dict[str, object]]:
        document = _json(
            self._run("list", "items", "--search", _ITEM_PREFIX.rstrip()),
            "item list",
        )
        if not isinstance(document, list) or any(
            not isinstance(item, dict) for item in document
        ):
            raise BitwardenError("Bitwarden returned an invalid item list")
        return document

    def assert_available(self, username: str) -> None:
        self._run("sync")
        target = self._item_name(username).casefold()
        matches = [
            item for item in self._viewer_items()
            if str(item.get("name", "")).casefold() == target
        ]
        if matches:
            raise BitwardenError(
                "Bitwarden already contains this external viewer credential"
            )

    def store(self, credential: ViewerCredential) -> None:
        self.assert_available(credential.username)
        item = {
            "type": 1,
            "name": self._item_name(credential.username),
            "notes": _ITEM_NOTES,
            "favorite": False,
            "fields": [],
            "login": {
                "username": credential.username,
                "password": credential.password,
                "totp": None,
                "uris": [],
            },
            "reprompt": 1,
        }
        encoded = base64.b64encode(
            json.dumps(item, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        created = _json(
            self._run("create", "item", input_data=encoded), "created item"
        )
        if not isinstance(created, dict) or not isinstance(created.get("id"), str):
            raise BitwardenError("Bitwarden returned an invalid created item")
        self._run("sync")
        target = self._item_name(credential.username).casefold()
        matches = [
            stored for stored in self._viewer_items()
            if str(stored.get("name", "")).casefold() == target
        ]
        if len(matches) != 1:
            raise BitwardenError("Bitwarden credential verification failed")
        login = matches[0].get("login")
        if (
            not isinstance(login, dict)
            or login.get("username") != credential.username
            or login.get("password") != credential.password
        ):
            raise BitwardenError("Bitwarden credential verification failed")
