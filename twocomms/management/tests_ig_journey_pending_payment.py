"""Actual ORM/GET evidence for a captionless receipt awaiting a manager."""
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from management.models import (IgClient, IgCommercialEpisode, IgFunnelResetAudit,
    IgPaymentConfirmationReview, IgPaymentObservationSource, InstagramBotMessage, InstagramBotSettings)
from management.services import ig_payment_observation as observation
from management.services.ig_journey_snapshot import build_journey_snapshot
from management.services.instagram_bot import ingress_provider_namespace


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class PendingReceiptJourneyTests(TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        env.start()
        self.addCleanup(env.stop)
        self.settings = InstagramBotSettings.objects.create(pk=1, ig_user_id="journey-owner",
            page_id="journey-owner", is_enabled=False, ai_enabled=False)
        self.namespace = ingress_provider_namespace(self.settings)
        self.buyer = IgClient.objects.create(igsid="journey-receipt-owner", stage="new")
        self.episode = IgCommercialEpisode.objects.create(client=self.buyer, sequence=1, open_slot=1,
            materialization_key="journey-pending-receipt")
        self.buyer.current_commercial_episode = self.episode
        self.buyer.save(update_fields=["current_commercial_episode"])
        self.source = InstagramBotMessage.objects.create(client=self.buyer, sender_id=self.buyer.igsid,
            role="user", source="webhook", status="done", text="", provider_namespace=self.namespace,
            provider_created_at=timezone.now(), media_capture_eligible=True,
            private_media_state="active", private_media_delete_after=timezone.now() + timedelta(hours=1))
        part = {"source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64,
            "status": "owned", "private_storage": True, "storage_name": "private/receipt.jpg",
            "mime": "image/jpeg", "role": "receipt", "delete_after": (timezone.now() + timedelta(hours=1)).isoformat(),
            "receipt_inspection": {"schema_version": "ig-receipt-inspection-v1", "state": "inspected",
                "source_message_id": self.source.pk, "source_part_id": "mp1_" + "a" * 32,
                "content_hash": "b" * 64, "role": "receipt", "confidence": 0.95,
                "provider_model": "gemini-receipt-fixture", "request_id": "offline-journey-receipt",
                "receipt_facts": {"amount": "970.00", "currency": "UAH", "payment_status": "completed"}}}
        self.source.attachment_media = [part]
        self.source.save(update_fields=["attachment_media"])
        self.review = IgPaymentConfirmationReview.objects.create(client=self.buyer, dedupe_key="pending-captionless",
            watermark_message_id=self.source.pk, evidence={"media": [{**part, "message_id": self.source.pk}]})
        self.episode.primary_payment_review = self.review
        self.episode.save(update_fields=["primary_payment_review"])
        IgPaymentObservationSource.objects.create(message=self.source, client=self.buyer,
            provider_namespace=self.namespace, source_digest=observation._source_digest(self.source, self.namespace),
            reset_floor=1, state="applied", observed_media_digest=observation._media_digest(self.source))
        self.user = get_user_model().objects.create_superuser("journey-receipt-reader", "journey@example.test", "x")
        self.client.force_login(self.user)

    @staticmethod
    def payment(snapshot):
        return next((node for node in snapshot["graph"]["nodes"] if node["id"] == "guide:payment"),
            {"state": "open", "facts": []})

    def _read(self, *, get=False):
        with ExitStack() as stack:
            protected = [stack.enter_context(patch(target)) for target in (
                "management.services.call_ai_analysis.gemini_generate_text",
                "management.services.instagram_bot._provider_http",
                "management.services.instagram_bot._capture_message_media",
                "management.services.ig_receipt_inspection.inspect_receipt_media",
                "management.services.ig_private_media.private_media_storage",
            )]
            with CaptureQueriesContext(connection) as queries:
                if get:
                    response = self.client.get(reverse("management_bot_client_detail_api", args=[self.buyer.pk]))
                    self.assertEqual(response.status_code, 200)
                    snapshot = response.json()["journey"]
                else:
                    snapshot = build_journey_snapshot(self.buyer)
            for effect in protected:
                effect.assert_not_called()
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        return self.payment(snapshot)

    def test_captionless_current_primary_receipt_get_is_select_only_and_waits_for_manager(self):
        node = self._read(get=True)
        self.assertEqual(node["waiting"]["kind"], "manager_review")
        self.assertTrue(node["waiting"]["receipt_received"])
        self.assertEqual(node["waiting"]["receipt_evidence_refs"], [{"kind": "message", "id": self.source.pk}])
        self.assertNotEqual(node["state"], "complete")
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.stage, "new")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")

    def test_namespace_hash_and_expiry_changes_do_not_invent_a_received_receipt(self):
        original = self.source.attachment_media
        for field in ("namespace", "hash", "message_expiry", "part_expiry"):
            with self.subTest(field=field):
                self.source.provider_namespace = self.namespace if field != "namespace" else "foreign-owner"
                self.source.private_media_delete_after = timezone.now() + timedelta(hours=1)
                self.source.attachment_media = [{**original[0]}]
                if field == "hash":
                    self.source.attachment_media[0]["content_hash"] = "c" * 64
                elif field == "message_expiry":
                    self.source.private_media_delete_after = timezone.now() - timedelta(seconds=1)
                elif field == "part_expiry":
                    self.source.attachment_media[0]["delete_after"] = (timezone.now() - timedelta(seconds=1)).isoformat()
                self.source.save(update_fields=["provider_namespace", "private_media_delete_after", "attachment_media"])
                node = self._read()
                self.assertEqual(node["waiting"]["kind"], "manager_review")
                self.assertFalse(node["waiting"].get("receipt_received", False))

    def test_reset_and_foreign_primary_review_cannot_reuse_receipt_proof(self):
        IgFunnelResetAudit.objects.create(client=self.buyer, reset_after_message_id=self.source.pk, reason="offline reset")
        node = self._read()
        self.assertFalse(node["waiting"].get("receipt_received", False))
        other = IgClient.objects.create(igsid="foreign-pending-review")
        foreign = IgPaymentConfirmationReview.objects.create(client=other, dedupe_key="foreign-review")
        self.episode.primary_payment_review = foreign
        self.episode.save(update_fields=["primary_payment_review"])
        self.assertNotIn("waiting", self._read())

    def test_low_confidence_candidate_stays_pending_without_receipt_count(self):
        self.source.attachment_media[0]["receipt_inspection"]["confidence"] = 0.3
        self.source.save(update_fields=["attachment_media"])
        node = self._read()
        self.assertEqual(node["waiting"]["kind"], "manager_review")
        self.assertFalse(node["waiting"].get("receipt_received", False))
