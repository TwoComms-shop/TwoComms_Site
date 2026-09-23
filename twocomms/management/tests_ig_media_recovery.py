"""Pure B01.4 media recovery policy and prepared-blob contracts."""
import json
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from management.services import ig_media_recovery as recovery
from management.services import ig_media_url_policy as policy


class MediaFailureTaxonomyTests(SimpleTestCase):
    def test_temporary_network_and_http_failures_get_one_bounded_retry(self):
        now = timezone.now()
        outcomes = [
            policy.FetchOutcome(success=False, reason=policy.REASON_DNS_FAILED),
            policy.FetchOutcome(success=False, reason=policy.REASON_TRANSPORT),
            policy.FetchOutcome(success=False, reason=policy.REASON_DEADLINE),
            policy.FetchOutcome(
                success=False,
                reason=policy.REASON_STATUS,
                status_code=408,
            ),
            policy.FetchOutcome(
                success=False,
                reason=policy.REASON_STATUS,
                status_code=425,
            ),
            policy.FetchOutcome(
                success=False,
                reason=policy.REASON_STATUS,
                status_code=429,
            ),
            policy.FetchOutcome(
                success=False,
                reason=policy.REASON_STATUS,
                status_code=503,
            ),
        ]

        for outcome in outcomes:
            with self.subTest(reason=outcome.reason, status=outcome.status_code):
                plan = recovery.plan_capture_failure(
                    outcome,
                    attempts=1,
                    now=now,
                )
                self.assertEqual(plan.failure_class, recovery.FAILURE_TEMPORARY)
                self.assertTrue(plan.retryable)
                self.assertFalse(plan.terminal)
                self.assertEqual(
                    plan.next_attempt_at,
                    now + timedelta(seconds=recovery.RETRY_BASE_SECONDS),
                )
                self.assertEqual(
                    plan.resolution_action,
                    recovery.RESOLUTION_RETRY,
                )

    def test_expired_statuses_are_terminal_and_request_resend(self):
        now = timezone.now()
        for status in (401, 403, 404, 410):
            with self.subTest(status=status):
                plan = recovery.plan_capture_failure(
                    policy.FetchOutcome(
                        success=False,
                        reason=policy.REASON_STATUS,
                        status_code=status,
                    ),
                    attempts=1,
                    now=now,
                )
                self.assertEqual(plan.failure_class, recovery.FAILURE_EXPIRED)
                self.assertEqual(plan.status, "expired")
                self.assertFalse(plan.retryable)
                self.assertTrue(plan.terminal)
                self.assertEqual(plan.resolution_action, recovery.RESOLUTION_RESEND)

    def test_security_mime_size_and_pixel_failures_never_retry(self):
        now = timezone.now()
        reasons = (
            policy.REASON_PRIVATE,
            f"{policy.REDIRECT_REASON_PREFIX}{policy.REASON_LOOPBACK}",
            policy.REASON_CONTENT_TYPE,
            policy.REASON_UNVERIFIABLE_MIME,
            policy.REASON_SIGNATURE,
            policy.REASON_DECLARED_TOO_LARGE,
            policy.REASON_STREAM_TOO_LARGE,
            policy.REASON_IMAGE_DECODE,
            policy.REASON_IMAGE_PIXELS,
        )

        for reason in reasons:
            with self.subTest(reason=reason):
                plan = recovery.plan_capture_failure(
                    policy.FetchOutcome(success=False, reason=reason),
                    attempts=1,
                    now=now,
                )
                self.assertEqual(plan.failure_class, recovery.FAILURE_PERMANENT)
                self.assertFalse(plan.retryable)
                self.assertTrue(plan.terminal)
                self.assertIsNone(plan.next_attempt_at)

    def test_second_attempt_or_shared_deadline_exhausts_without_more_retry(self):
        now = timezone.now()
        outcome = policy.FetchOutcome(
            success=False,
            reason=policy.REASON_TRANSPORT,
        )

        attempts_exhausted = recovery.plan_capture_failure(
            outcome,
            attempts=2,
            now=now,
        )
        deadline_exhausted = recovery.plan_capture_failure(
            outcome,
            attempts=1,
            now=now,
            deadline_at=now + timedelta(seconds=10),
        )

        self.assertTrue(attempts_exhausted.terminal)
        self.assertFalse(attempts_exhausted.retryable)
        self.assertTrue(deadline_exhausted.terminal)
        self.assertFalse(deadline_exhausted.retryable)
        self.assertEqual(
            attempts_exhausted.part_updates()["resolution_action"],
            recovery.RESOLUTION_RESEND,
        )

    def test_retry_due_and_future_retry_use_persisted_deadline(self):
        now = timezone.now()
        plan = recovery.plan_capture_failure(
            policy.FetchOutcome(success=False, reason=policy.REASON_TRANSPORT),
            attempts=1,
            now=now,
        )
        part = plan.part_updates()

        self.assertFalse(recovery.retry_due(part, now=now))
        self.assertEqual(recovery.pending_retry_at([part], now=now), plan.next_attempt_at)
        self.assertTrue(
            recovery.retry_due(
                part,
                now=plan.next_attempt_at,
            )
        )


class PreparedBlobContractTests(SimpleTestCase):
    def test_video_descriptor_is_accepted_for_private_preview(self):
        raw = b"video bytes"
        descriptor = recovery.prepared_blob_descriptor(
            storage_name="ig_message_media/41/clip.mp4",
            mime_type="video/mp4",
            body_bytes=raw,
        )
        self.assertTrue(recovery.prepared_blob_matches(descriptor, raw))
        self.assertEqual(recovery.prepared_part_updates(descriptor)["status"], "storing")

    def test_descriptor_is_json_safe_stable_and_verifies_exact_bytes(self):
        raw = b"private image bytes"
        first = recovery.prepared_blob_descriptor(
            storage_name="ig_message_media/41/abc.jpg",
            mime_type="image/jpeg",
            body_bytes=raw,
        )
        second = recovery.prepared_blob_descriptor(
            storage_name="ig_message_media/41/abc.jpg",
            mime_type="image/jpeg",
            body_bytes=raw,
        )

        self.assertEqual(first, second)
        self.assertNotIn(raw, first.values())
        json.dumps(recovery.prepared_part_updates(first))
        self.assertTrue(recovery.prepared_blob_matches(first, raw))
        self.assertFalse(recovery.prepared_blob_matches(first, b"changed"))

    def test_owned_finalization_requires_verified_prepared_bytes(self):
        raw = b"captured bytes"
        descriptor = recovery.prepared_blob_descriptor(
            storage_name="ig_message_media/52/blob.png",
            mime_type="image/png",
            body_bytes=raw,
        )

        updates = recovery.owned_part_updates(
            descriptor,
            verified_body_bytes=raw,
        )

        self.assertEqual(updates["status"], "owned")
        self.assertEqual(updates["storage_name"], descriptor["storage_name"])
        self.assertEqual(updates["content_hash"], descriptor["content_hash"])
        self.assertEqual(updates["prepared_blob"], {})
        self.assertEqual(updates["capture_deadline_at"], "")
        with self.assertRaisesRegex(
            recovery.MediaRecoveryError,
            "prepared_blob_mismatch",
        ):
            recovery.owned_part_updates(
                descriptor,
                verified_body_bytes=b"wrong",
            )

    def test_descriptor_rejects_unsafe_path_mime_and_empty_bytes(self):
        cases = (
            {
                "storage_name": "../escape.jpg",
                "mime_type": "image/jpeg",
                "body_bytes": b"x",
            },
            {
                "storage_name": "/absolute.jpg",
                "mime_type": "image/jpeg",
                "body_bytes": b"x",
            },
            {
                "storage_name": "private/file.txt",
                "mime_type": "text/plain",
                "body_bytes": b"x",
            },
            {
                "storage_name": "private/empty.jpg",
                "mime_type": "image/jpeg",
                "body_bytes": b"",
            },
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(recovery.MediaRecoveryError):
                    recovery.prepared_blob_descriptor(**case)
