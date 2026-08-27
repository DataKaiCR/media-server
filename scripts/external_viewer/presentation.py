"""Human-safe terminal presentation for external-viewer onboarding."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import sys
import time
from typing import Callable, Iterator, TextIO


_APPLY_PHASE_LABELS = {
    "safety": "Revalidating safety conditions",
    "vault": "Storing and verifying Bitwarden credential",
    "rollback_state": "Creating rollback state and checking for drift",
    "jellyfin": "Creating and restricting Jellyfin account",
    "jellyseerr": "Importing and verifying Jellyseerr account",
    "receipt": "Writing private receipt",
}


@dataclass
class HumanPresenter:
    """Render only aggregate, explicitly whitelisted operator feedback."""

    enabled: bool
    stream: TextIO = field(default_factory=lambda: sys.stdout)
    clock: Callable[[], float] = time.monotonic
    _run_started: float = field(init=False, default=0.0)
    _credential_mode: str = field(init=False, default="memory")
    _before_count: int | None = field(init=False, default=None)
    _phase_order: tuple[str, ...] = field(init=False, default=())
    _active_phase: tuple[str, float] | None = field(init=False, default=None)

    def introduction(self, credential_mode: str) -> None:
        if not self.enabled:
            return
        self._run_started = self.clock()
        self._credential_mode = credential_mode
        labels = {
            "generate": "Bitwarden — generate",
            "save": "Bitwarden — save",
            "file": "protected credential file",
            "memory": "memory only",
        }
        print("External viewer onboarding", file=self.stream)
        print(f"Credential mode: {labels[credential_mode]}\n", file=self.stream)

    @contextmanager
    def operation(self, label: str, success: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        started = self.clock()
        print(f"→ {label}...", file=self.stream, flush=True)
        try:
            yield
        except BaseException:
            elapsed = self.clock() - started
            print(f"✗ {label} failed ({elapsed:.1f}s)", file=self.stream)
            raise
        elapsed = self.clock() - started
        print(f"✓ {success} ({elapsed:.1f}s)", file=self.stream)

    def note(self, message: str) -> None:
        if self.enabled:
            print(f"✓ {message}", file=self.stream)

    def show_preflight(
        self, result: dict[str, object], *, bitwarden_pending: bool
    ) -> None:
        if not self.enabled:
            return
        configured = result["configured_external_count"]
        request_only = result["jellyseerr_request_only_count"]
        self._before_count = int(configured)
        print("\nCurrent state:", file=self.stream)
        print(f"  External viewers: {configured}", file=self.stream)
        print("  Jellyfin policy: compliant", file=self.stream)
        print("  Public Jellyfin profiles: none", file=self.stream)
        print(
            f"  Jellyseerr viewers: {request_only} request-only",
            file=self.stream,
        )
        print("  Automatic provisioning: disabled", file=self.stream)
        credential_state = (
            "prepared in memory; Bitwarden storage pending confirmation"
            if bitwarden_pending
            else "held in memory; not persisted by onboarding"
        )
        print(f"  Credential: {credential_state}", file=self.stream)

    def show_plan(self, *, bitwarden: bool) -> None:
        if not self.enabled:
            return
        print("\nPlanned changes:", file=self.stream)
        number = 1
        if bitwarden:
            print(
                f"  {number}. Store and verify the credential in Bitwarden",
                file=self.stream,
            )
            number += 1
        print(
            f"  {number}. Create one hidden Jellyfin viewer with Movies and TV only",
            file=self.stream,
        )
        print(
            f"  {number + 1}. Import it into Jellyseerr with request-only access",
            file=self.stream,
        )
        print(file=self.stream)

    def cancelled(self, *, bitwarden: bool) -> None:
        if not self.enabled:
            return
        print("\nNo changes made.", file=self.stream)
        if bitwarden:
            print("The credential was not stored in Bitwarden.", file=self.stream)

    def preflight_only(self) -> None:
        if self.enabled:
            print("\nPreflight only; no changes made.", file=self.stream)

    def configure_apply(self, *, bitwarden: bool) -> None:
        phases = ["safety"]
        if bitwarden:
            phases.append("vault")
        phases.extend(("rollback_state", "jellyfin", "jellyseerr", "receipt"))
        self._phase_order = tuple(phases)

    def apply_phase(self, phase: str, status: str) -> None:
        if not self.enabled:
            return
        if phase == "rollback":
            self._rollback_phase(status)
            return
        if phase not in self._phase_order:
            return
        position = self._phase_order.index(phase) + 1
        prefix = f"[{position}/{len(self._phase_order)}]"
        label = _APPLY_PHASE_LABELS[phase]
        if status == "start":
            self._active_phase = (phase, self.clock())
            print(f"{prefix} {label}...", end="", file=self.stream, flush=True)
            return
        self._finish_phase(phase, status)

    def _rollback_phase(self, status: str) -> None:
        if status == "start":
            self._active_phase = ("rollback", self.clock())
            print(
                "[rollback] Restoring and verifying prior state...",
                end="",
                file=self.stream,
                flush=True,
            )
            return
        self._finish_phase("rollback", status)

    def _finish_phase(self, phase: str, status: str) -> None:
        active = self._active_phase
        if active is None or active[0] != phase:
            return
        elapsed = self.clock() - active[1]
        word = "done" if status == "done" else "failed"
        print(f" {word} ({elapsed:.1f}s)", file=self.stream)
        self._active_phase = None

    def record_baseline(self, result: dict[str, object]) -> None:
        if self._before_count is None:
            self._before_count = int(result["configured_external_count"])

    def complete(self, result: dict[str, object]) -> None:
        if not self.enabled:
            return
        elapsed = self.clock() - self._run_started
        after = int(result["configured_external_count"])
        print(f"\n✓ Onboarding completed ({elapsed:.1f}s)\n", file=self.stream)
        if self._before_count is None:
            print(f"  External viewers: {after}", file=self.stream)
        else:
            print(
                f"  External viewers: {self._before_count} → {after}",
                file=self.stream,
            )
        if result["credential_storage"] == "bitwarden":
            credential = "stored and verified in Bitwarden"
        elif self._credential_mode == "file":
            credential = "loaded from protected file; no new copy persisted"
        else:
            credential = "used from memory and not persisted by onboarding"
        print(f"  Credential: {credential}", file=self.stream)
        print(
            "  Jellyfin: login verified; hidden; Movies and TV only",
            file=self.stream,
        )
        print(
            "  Jellyseerr: login verified; request-only",
            file=self.stream,
        )
        print("  Policy backup: created", file=self.stream)
        rollback = "yes" if result["rollback_required"] else "no"
        print(f"  Rollback required: {rollback}", file=self.stream)

    def setup_complete(self, result: dict[str, object]) -> None:
        if not self.enabled:
            return
        action = "created" if result["configuration_created"] else "updated"
        print(f"✓ Private onboarding configuration {action}.", file=self.stream)
        print("  No viewer credentials were persisted.", file=self.stream)
        print("  Next: run preflight or apply onboarding.", file=self.stream)
