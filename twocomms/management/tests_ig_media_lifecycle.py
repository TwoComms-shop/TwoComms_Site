"""Pure lifecycle DTO tests; no Django, ORM, filesystem or provider."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from management.services.ig_private_media_lifecycle import RETENTION_POLICY_VERSION, project_private_media_lifecycle


NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def part(**changes):
    result = {"source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64,
        "status": "owned", "capture_state": "owned", "private_storage": True,
        "storage_name": "private/customer/secret.ogg", "url": "https://signed.example/private?token=secret",
        "mime": "audio/ogg", "delete_after": (NOW + timedelta(days=2)).isoformat()}
    result.update(changes)
    return result


def project(raw=None, **changes):
    values = {"message_state": "active", "owner_verified": True, "now": NOW}
    values.update(changes)
    return project_private_media_lifecycle(part() if raw is None else raw, **values)


def captured_part(seconds=60 * 86400):
    raw = part(delete_after=(NOW + timedelta(seconds=seconds)).isoformat())
    raw["retention_policy"] = {"version": RETENTION_POLICY_VERSION, "retention_seconds": seconds,
        "captured_at": NOW.isoformat(), "delete_after": raw["delete_after"],
        "source_part_id": raw["source_part_id"], "content_hash": raw["content_hash"]}
    return raw


class PrivateMediaLifecyclePureTests(unittest.TestCase):
    def test_active_is_guarded_preview_eligibility_and_never_file_proof(self):
        dto = project()
        self.assertEqual(dto["state"], "active")
        self.assertTrue(dto["readable"])
        self.assertEqual(dto["readability"], "preview_eligible")
        self.assertEqual(dto["policy"], {"state": "unknown", "version": None, "retention_seconds": None})

    def test_unknown_expiry_does_not_invent_new_sixty_days_or_evergreen_read(self):
        for raw_due in (None, "", "unknown", "2026-10-06T12:00:00"):
            with self.subTest(raw_due=raw_due):
                dto = project(part(delete_after=raw_due))
                self.assertEqual(dto["state"], "unverified")
                self.assertEqual(dto["reason"], "expiry_unknown")
                self.assertFalse(dto["readable"])
                self.assertFalse(dto["expiry_known"])
                self.assertIsNone(dto["deletion_due"])
                self.assertIsNone(dto["policy"]["retention_seconds"])

    def test_deadline_elapsed_is_expired_and_never_deleted(self):
        dto = project(part(delete_after=NOW.isoformat()))
        self.assertEqual(dto["state"], "expired")
        self.assertFalse(dto["readable"])
        self.assertNotEqual(dto["reason"], "deletion_confirmed")

    def test_earliest_private_sibling_and_message_deadline_are_used_without_mutation(self):
        raw = part()
        sibling = part(delete_after=(NOW - timedelta(seconds=1)).isoformat())
        original = deepcopy([raw, sibling])
        dto = project(raw, message_delete_after=NOW + timedelta(days=60), message_parts=[raw, sibling])
        self.assertEqual(dto["state"], "expired")
        self.assertEqual(dto["deletion_due"], sibling["delete_after"])
        self.assertEqual([raw, sibling], original)
        sibling["private_storage"] = False
        self.assertEqual(project(raw, message_parts=[sibling])["state"], "active")

    def test_delete_pending_deleting_failure_and_retry_have_distinct_finite_states(self):
        for row_state, expected in (("delete_pending", "pending"), ("deleting", "deleting"), ("delete_failed", "delete_failed")):
            with self.subTest(row_state=row_state):
                dto = project(message_state=row_state, message_delete_after=NOW + timedelta(seconds=60))
                self.assertEqual(dto["state"], expected)
                self.assertFalse(dto["readable"])
                self.assertEqual(dto["retry_due"], (NOW + timedelta(seconds=60)).isoformat() if row_state == "delete_failed" else None)

    def test_deleted_requires_canonical_completion_and_captured_part_tombstone(self):
        raw = part(status="expired", private_storage=False, storage_name="", delete_after=None)
        dto = project(raw, message_state="deleted")
        self.assertEqual(dto["state"], "deleted")
        self.assertEqual(dto["reason"], "deletion_confirmed")
        self.assertIsNone(dto["deletion_due"])
        self.assertFalse(dto["readable"])
        self.assertNotEqual(project(part(status="owned"), message_state="deleted")["state"], "deleted")

    def test_capture_pending_missing_and_unverified_are_not_interchangeable(self):
        for status, capture, expected in (("pending", "discovered", "pending"), ("acquiring", "fetching", "pending"),
                ("unavailable", "failed", "missing"), ("metadata_only", "metadata_only", "missing"),
                ("owned", "unknown", "unverified")):
            with self.subTest(status=status):
                dto = project(part(status=status, capture_state=capture))
                self.assertEqual(dto["state"], expected)
                self.assertFalse(dto["readable"])

    def test_erasure_owner_and_wrong_part_or_hash_never_enable_preview(self):
        for kwargs in ({"erasure_started": True}, {"owner_verified": False}, {"owner_verified": "true"}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(project(**kwargs)["readable"])
        for changes in ({"source_part_id": "foreign-provider-id"}, {"content_hash": "wrong"}, {"private_storage": False}):
            self.assertEqual(project(part(**changes))["state"], "unverified")
        self.assertFalse(project(part(capture_state=[]))["readable"])
        self.assertFalse(project(part(mime="application/pdf"))["readable"])

    def test_only_bound_verified_capture_policy_can_show_actual_sixty_day_cap(self):
        raw = captured_part()
        dto = project(raw)
        self.assertEqual(dto["policy"], {"state": "verified", "version": RETENTION_POLICY_VERSION, "retention_seconds": 60 * 86400})
        for seconds in (3600, 3 * 86400):
            self.assertEqual(project(captured_part(seconds))["policy"]["retention_seconds"], seconds)
        raw["status"] = "pending"
        self.assertEqual(project(raw)["policy"]["state"], "unknown")

    def test_unbound_future_envelope_unknown_version_wrong_deadline_or_foreign_part_gives_no_policy_proof(self):
        variants = [{"version": RETENTION_POLICY_VERSION, "retention_seconds": 60 * 86400}]
        for change in ({"version": "future-v9"}, {"retention_seconds": 90 * 86400}, {"retention_seconds": True},
                       {"source_part_id": "mp1_" + "c" * 32}, {"content_hash": "c" * 64},
                       {"delete_after": (NOW + timedelta(days=61)).isoformat()}):
            variants.append({**captured_part()["retention_policy"], **change})
        for policy in variants:
            with self.subTest(policy=policy):
                dto = project(part(retention_policy=policy))
                self.assertEqual(dto["policy"]["state"], "unknown")
                self.assertIsNone(dto["policy"]["retention_seconds"])

    def test_completed_deletion_retains_bound_policy_without_fabricating_timer(self):
        raw = captured_part()
        raw.update(status="expired", private_storage=False, storage_name="", delete_after=None)
        dto = project(raw, message_state="deleted")
        self.assertEqual(dto["policy"]["state"], "verified")
        self.assertIsNone(dto["deletion_due"])

    def test_result_never_contains_paths_urls_tokens_hashes_or_input_mutable_objects(self):
        raw = captured_part()
        original = deepcopy(raw)
        dto = project(raw)
        serialized = json.dumps(dto)
        for private in (raw["storage_name"], raw["url"], raw["source_part_id"], raw["content_hash"], "secret"):
            self.assertNotIn(private, serialized)
        dto["policy"]["retention_seconds"] = 1
        self.assertEqual(raw, original)

    def test_malformed_data_and_clock_stay_finite_and_unreadable(self):
        for raw in ({"status": [], "capture_state": {}}, {}, "provider URL"):
            self.assertFalse(project(raw)["readable"])
        self.assertEqual(project(message_state=[])["reason"], "lifecycle_unverified")
        self.assertEqual(project(now="not a clock")["reason"], "clock_unverified")

    def test_extreme_captured_policy_timestamp_is_finite_unknown_without_overflow(self):
        raw = captured_part(3600)
        extreme = datetime.max.replace(tzinfo=timezone.utc).isoformat()
        raw["delete_after"] = extreme
        raw["retention_policy"].update(captured_at=extreme, delete_after=extreme)
        self.assertEqual(project(raw)["policy"]["state"], "unknown")
