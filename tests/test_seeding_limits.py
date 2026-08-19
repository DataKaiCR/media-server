import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from seeding_evidence.client import ClientError  # noqa: E402
from seeding_evidence.config import load_config  # noqa: E402
from seeding_evidence.policy_client import (  # noqa: E402
    QBittorrentPolicyClient,
    ShareLimits,
)
from seeding_evidence.share_limits import (  # noqa: E402
    build_share_limit_plan,
    enforce_share_limits,
)


SECRET_NAME = "PRIVATE NATIVE LIMIT TITLE"
SECRET_HASH = "a" * 40
STANDARD_HASH = "b" * 40
CONTRIBUTOR_HASH = "c" * 40
STEWARDSHIP_HASH = "d" * 40
CONFLICT_HASH = "e" * 40
UNCLASSIFIED_HASH = "f" * 40


def torrent_row(
    torrent_hash,
    tag,
    *,
    ratio_limit=-2,
    seeding_limit=-2,
    inactive_limit=-2,
    ratio=0,
    seed_seconds=0,
    force=False,
):
    return {
        "name": SECRET_NAME,
        "hash": torrent_hash,
        "tags": tag,
        "ratio_limit": ratio_limit,
        "seeding_time_limit": seeding_limit,
        "inactive_seeding_time_limit": inactive_limit,
        "ratio": ratio,
        "seeding_time": seed_seconds,
        "progress": 1,
        "force_start": force,
        "state": "forcedUP" if force else "stalledUP",
    }


class FakePolicyClient:
    def __init__(self, torrents, *, safe_global=True, remove_reached=False, fail_once=False):
        self.torrents = copy.deepcopy(torrents)
        self.preferences = {
            "max_ratio_enabled": not safe_global,
            "max_seeding_time_enabled": False,
            "max_inactive_seeding_time_enabled": False,
            "max_ratio_act": 0 if safe_global else 3,
        }
        self.remove_reached = remove_reached
        self.fail_once = fail_once
        self.authenticated = False
        self.global_updates = 0
        self.limit_calls = []

    def authenticate(self):
        self.authenticated = True

    def get_json(self, endpoint):
        if endpoint == "torrents/info":
            return copy.deepcopy(self.torrents)
        if endpoint == "app/preferences":
            return copy.deepcopy(self.preferences)
        raise AssertionError(endpoint)

    def set_safe_global_share_behavior(self):
        self.global_updates += 1
        self.preferences.update(
            {
                "max_ratio_enabled": False,
                "max_seeding_time_enabled": False,
                "max_inactive_seeding_time_enabled": False,
                "max_ratio_act": 0,
            }
        )

    def set_share_limits(self, hashes, limits):
        self.limit_calls.append((hashes, limits))
        selected = []
        for row in self.torrents:
            if row["hash"] not in hashes:
                continue
            row["ratio_limit"] = limits.ratio
            row["seeding_time_limit"] = limits.seeding_minutes
            row["inactive_seeding_time_limit"] = limits.inactive_minutes
            selected.append(row)
        if self.fail_once:
            self.fail_once = False
            raise ClientError("simulated ambiguous transport failure")
        if self.remove_reached:
            for row in selected:
                reached = (
                    limits.ratio >= 0
                    and row["ratio"] >= limits.ratio
                    or limits.seeding_minutes >= 0
                    and row["seeding_time"] / 60 >= limits.seeding_minutes
                )
                if reached:
                    self.torrents.remove(row)


class SeedingShareLimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self):
        path = self.root / "config.toml"
        path.write_text(
            f'''version = 1
report_dir = {json.dumps(str(self.root / "reports"))}

[qbittorrent]
url = "http://127.0.0.1:1"
timeout_seconds = 5

[[tiers]]
id = "common"
tag = "seed-common-3d"
minimum_days = 3
review_after_days = 3
protected = false
native_stop = true

[[tiers]]
id = "standard"
tag = "seed-standard-3x-14d"
minimum_days = 14
target_ratio = 3.0
review_after_days = 30
protected = false
threshold_mode = "any"
native_stop = true

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
        return load_config(path)

    def rows(self, *, force=False):
        return [
            torrent_row(SECRET_HASH, "seed-common-3d", force=force),
            torrent_row(
                STANDARD_HASH,
                "seed-standard-3x-14d",
                ratio=4,
            ),
            torrent_row(CONTRIBUTOR_HASH, "seed-contributor-5x-30d"),
            torrent_row(
                STEWARDSHIP_HASH,
                "seed-stewardship-90d",
                ratio_limit=-1,
                seeding_limit=-1,
                inactive_limit=-1,
            ),
            torrent_row(
                CONFLICT_HASH,
                "seed-common-3d, seed-standard-3x-14d",
            ),
            torrent_row(UNCLASSIFIED_HASH, ""),
        ]

    def test_plan_maps_tiers_to_native_or_limits_and_protects_others(self):
        plan = build_share_limit_plan(self.write_config(), self.rows(force=True))

        self.assertEqual(plan.classified_count, 4)
        self.assertEqual(plan.conflicting_count, 1)
        self.assertEqual(plan.unclassified_count, 1)
        self.assertEqual(plan.forced_torrent_count, 1)
        self.assertEqual(plan.drift_counts, {"common": 1, "contributor": 1, "standard": 1})
        by_tier = {change.tier_id: change for change in plan.changes}
        self.assertEqual(by_tier["common"].expected, ShareLimits(-1, 4320, -1))
        self.assertEqual(by_tier["standard"].expected, ShareLimits(3.0, 20160, -1))
        self.assertEqual(by_tier["contributor"].expected, ShareLimits(-1, -1, -1))
        self.assertTrue(by_tier["standard"].threshold_already_met)
        rendered = json.dumps(plan.aggregate()) + repr(plan.changes)
        self.assertNotIn(SECRET_NAME, rendered)
        self.assertNotIn(SECRET_HASH, rendered)

    def test_protected_tier_cannot_be_given_native_stop_limits(self):
        with self.assertRaisesRegex(ValueError, "protected"):
            build_share_limit_plan(
                self.write_config(),
                self.rows(),
                native_stop_tiers=("stewardship",),
            )

    def test_report_only_never_mutates(self):
        client = FakePolicyClient(self.rows(), safe_global=False)
        outcome = enforce_share_limits(self.write_config(), apply=False, client=client)

        self.assertFalse(outcome["mutation_performed"])
        self.assertFalse(outcome["global_stop_behavior_compliant"])
        self.assertEqual(outcome["share_limit_drift_count"], 3)
        self.assertEqual(client.limit_calls, [])
        self.assertEqual(client.global_updates, 0)

    def test_apply_is_native_idempotent_and_global_action_is_stop(self):
        rows = self.rows()[:4]
        client = FakePolicyClient(rows, safe_global=False)

        first = enforce_share_limits(self.write_config(), apply=True, client=client)
        second = enforce_share_limits(self.write_config(), apply=True, client=client)

        self.assertEqual(first["reconciled_count"], 3)
        self.assertEqual(first["remaining_share_limit_drift_count"], 0)
        self.assertTrue(first["global_stop_behavior_compliant"])
        self.assertEqual(first["verification_failure_count"], 0)
        self.assertEqual(client.global_updates, 1)
        self.assertEqual(second["reconciled_count"], 0)
        self.assertFalse(second["mutation_performed"])
        self.assertEqual(client.preferences["max_ratio_act"], 0)
        self.assertFalse(client.preferences["max_ratio_enabled"])

    def test_already_reached_native_limit_may_be_removed_by_arr_during_verification(self):
        rows = [
            torrent_row(
                STANDARD_HASH,
                "seed-standard-3x-14d",
                ratio=4,
            )
        ]
        client = FakePolicyClient(rows, remove_reached=True)

        outcome = enforce_share_limits(self.write_config(), apply=True, client=client)

        self.assertEqual(outcome["reconciled_count"], 1)
        self.assertEqual(outcome["removed_after_native_stop_count"], 1)
        self.assertEqual(outcome["verification_failure_count"], 0)
        self.assertEqual(client.torrents, [])

    def test_ambiguous_transport_failure_restores_previous_limits(self):
        rows = [torrent_row(SECRET_HASH, "seed-common-3d")]
        client = FakePolicyClient(rows, fail_once=True)

        with self.assertRaisesRegex(ClientError, "rolled back"):
            enforce_share_limits(self.write_config(), apply=True, client=client)

        restored = client.torrents[0]
        self.assertEqual(restored["ratio_limit"], -2)
        self.assertEqual(restored["seeding_time_limit"], -2)
        self.assertEqual(restored["inactive_seeding_time_limit"], -2)

    def test_policy_client_allows_only_limits_and_safe_stop_action(self):
        client = QBittorrentPolicyClient(self.write_config().qbittorrent)
        requests = []

        def capture(request):
            requests.append(request)
            return b""

        with patch.object(client, "_read", side_effect=capture):
            client.set_share_limits(
                (SECRET_HASH,),
                ShareLimits(3.0, 20160, -1),
            )
            client.set_safe_global_share_behavior()

        share = parse_qs(requests[0].data.decode("ascii"))
        self.assertEqual(share["ratioLimit"], ["3.0"])
        self.assertEqual(share["seedingTimeLimit"], ["20160"])
        settings = json.loads(parse_qs(requests[1].data.decode("ascii"))["json"][0])
        self.assertEqual(settings["max_ratio_act"], 0)
        self.assertFalse(settings["max_ratio_enabled"])
        with self.assertRaisesRegex(ClientError, "policy-mutation allowlist"):
            client._post_policy_form("torrents/delete", {"hashes": SECRET_HASH})

    def test_policy_client_rejects_unsafe_limit_values(self):
        client = QBittorrentPolicyClient(self.write_config().qbittorrent)
        with self.assertRaisesRegex(ClientError, "ratio"):
            client.set_share_limits((SECRET_HASH,), ShareLimits(0, 100, -1))
        with self.assertRaisesRegex(ClientError, "time"):
            client.set_share_limits((SECRET_HASH,), ShareLimits(-1, 0, -1))


if __name__ == "__main__":
    unittest.main()
