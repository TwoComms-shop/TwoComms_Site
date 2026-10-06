from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgPaymentObservationSource, InstagramBotMessage, InstagramBotSettings
from management.services import ig_payment_observation as observation
from management.services.instagram_bot import ingress_provider_namespace


class PaymentObservationTests(TransactionTestCase):
    def setUp(self):
        self.settings = InstagramBotSettings.objects.create(pk=1,
            ig_user_id="observer-owner", page_id="observer-owner", ai_enabled=False, is_enabled=False)
        self.namespace = ingress_provider_namespace(self.settings)
        self.client = IgClient.objects.create(igsid="178400000000351",
            manager_takeover=True, bot_paused=True, paused_reason="manager_takeover")

    def source(self, *, role="user", text="Оплатив, ось чек", parts=None):
        return InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            role=role, source="webhook" if role == "user" else "echo", text=text,
            provider_namespace=self.namespace, status="done", media_capture_eligible=True,
            attachment_media=parts or [], provider_created_at=timezone.now())

    def test_ingress_queue_is_provider_free_and_idempotent_under_manager_pause(self):
        source = self.source()
        with patch("management.services.ig_payment_review.create_payment_review") as review, \
             patch("management.services.instagram_bot._capture_message_media") as capture:
            self.assertTrue(observation.enqueue_payment_observation(source.pk).queued)
            self.assertTrue(observation.enqueue_payment_observation(source.pk).queued)
        review.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(IgPaymentObservationSource.objects.count(), 1)

    @patch("management.services.ig_conversation_agreement.persist_conversation_agreement", return_value={"persisted": True})
    @patch("management.services.ig_payment_review.create_payment_review", return_value=SimpleNamespace(pk=17, evidence={}))
    @patch("management.services.ig_payment_review.apply_authenticated_manager_payment_confirmation", return_value=None)
    def test_worker_observes_accepted_user_and_manager_when_reply_and_ai_disabled(self, confirmation, review, agreement):
        for role in ("user", "manager"):
            source = self.source(role=role)
            result = observation.observe_payment_source(source.pk, allow_provider=False)
            self.assertTrue(result.observed, result.reason)
            row = IgPaymentObservationSource.objects.get(message=source)
            self.assertEqual(row.state, row.State.APPLIED)
            self.assertEqual(row.outcome["review_id"], 17)
            self.assertEqual(row.outcome["watermark_message_id"], source.pk)
        self.assertEqual(review.call_count, 2)
        self.assertEqual(agreement.call_count, 2)
        self.assertEqual(confirmation.call_count, 1)
        self.client.refresh_from_db()
        self.assertTrue(self.client.manager_takeover)
        self.assertTrue(self.client.bot_paused)
        self.assertFalse(InstagramBotMessage.objects.filter(role="model").exists())

    @patch("management.services.ig_conversation_agreement.persist_conversation_agreement", return_value={"persisted": True})
    @patch("management.services.ig_payment_review.create_payment_review", return_value=SimpleNamespace(pk=17, evidence={}))
    @patch("management.services.ig_payment_review.apply_authenticated_manager_payment_confirmation", return_value=SimpleNamespace(pk=17, evidence={}))
    def test_manager_confirmation_adapter_is_local_and_queue_bound(self, confirmation, review, agreement):
        source = self.source(role="manager", text="Оплату 970 грн отримали")
        result = observation.observe_payment_source(source.pk, allow_provider=True)
        self.assertTrue(result.observed, result.reason)
        self.assertFalse(review.call_args.kwargs["allow_provider"])
        self.assertEqual(confirmation.call_args.args, (source.pk,))
        row = IgPaymentObservationSource.objects.get(message=source)
        self.assertEqual(row.outcome["manager_payment_review_id"], 17)
        self.assertFalse(row.outcome["recognition_allowed"])

    def test_source_mutation_and_reset_reject_owned_claim(self):
        source = self.source()
        observation.enqueue_payment_observation(source.pk)
        claim = observation._claim(source.pk)
        InstagramBotMessage.objects.filter(pk=source.pk).update(text="Different source")
        with patch("management.services.ig_payment_review.create_payment_review") as review:
            result = observation._observe_claim(*claim)
        self.assertFalse(result.observed)
        review.assert_not_called()
        self.assertEqual(IgPaymentObservationSource.objects.get(message=source).state, "blocked")
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=source.pk + 1):
            self.assertEqual(observation.enqueue_payment_observation(source.pk).reason, "source_reset_changed")

    def test_erasure_between_claim_and_drain_is_graceful_and_provider_free(self):
        source = self.source()
        observation.enqueue_payment_observation(source.pk)
        claim = observation._claim(source.pk)
        source.delete()
        with patch("management.services.ig_payment_review.create_payment_review") as review:
            result = observation._observe_claim(*claim)
        self.assertEqual(result.reason, "observation_source_erased")
        review.assert_not_called()

    def test_claim_replaced_while_waiting_for_client_lock_never_observes(self):
        source = self.source()
        observation.enqueue_payment_observation(source.pk)
        claim = observation._claim(source.pk)

        @contextmanager
        def replaced_lock(_client_id):
            IgPaymentObservationSource.objects.filter(pk=claim[0]).update(claim_token="replacement")
            yield

        with patch("management.services.ig_commercial_episodes.commercial_episode_client_lock", replaced_lock), \
             patch("management.services.ig_payment_review.create_payment_review") as review, \
             patch("management.services.ig_conversation_agreement.persist_conversation_agreement") as agreement:
            result = observation._observe_claim(*claim)
        self.assertEqual(result.reason, "observation_claim_lost")
        review.assert_not_called()
        agreement.assert_not_called()

    @patch("management.services.ig_conversation_agreement.persist_conversation_agreement", return_value={"persisted": False})
    @patch("management.services.ig_payment_review.create_payment_review", return_value=None)
    def test_media_capture_transition_reopens_successful_local_observation(self, review, agreement):
        source = self.source()
        self.assertTrue(observation.observe_payment_source(source.pk, allow_provider=False).observed)
        source.attachment_media = [{"source_part_id": "mp1_" + "a" * 32,
            "status": "owned", "content_hash": "b" * 64, "private_storage": True}]
        source.save(update_fields=["attachment_media"])
        observation.enqueue_payment_observation(source.pk)
        self.assertEqual(IgPaymentObservationSource.objects.get(message=source).state, "pending")
        self.assertTrue(observation.observe_payment_source(source.pk, allow_provider=False).observed)
        self.assertEqual(review.call_count, 2)

    @patch("management.services.ig_conversation_agreement.persist_conversation_agreement", return_value={"persisted": False})
    @patch("management.services.ig_payment_review.create_payment_review", side_effect=RuntimeError("bounded failure"))
    def test_failed_observation_retries_without_losing_source_watermark(self, review, agreement):
        source = self.source()
        result = observation.observe_payment_source(source.pk, allow_provider=False)
        self.assertFalse(result.observed)
        row = IgPaymentObservationSource.objects.get(message=source)
        self.assertEqual(row.state, "pending")
        self.assertEqual(row.message_id, source.pk)
        self.assertEqual(row.attempts, 1)
        self.assertGreater(row.next_attempt_at, timezone.now())
        self.assertEqual(row.last_error, "observation_RuntimeError")

    def test_backlog_adoption_requires_explicit_cutover_and_excludes_history(self):
        live = self.source()
        historical = self.source()
        InstagramBotMessage.objects.filter(pk=historical.pk).update(source="poll_history")
        with override_settings(IG_PAYMENT_OBSERVATION_CUTOVER_AT=None):
            self.assertEqual(observation.reconcile_payment_observation_sources(), 0)
        with override_settings(IG_PAYMENT_OBSERVATION_CUTOVER_AT=(live.created_at.isoformat())):
            self.assertEqual(observation.reconcile_payment_observation_sources(), 1)
        self.assertFalse(IgPaymentObservationSource.objects.filter(message=historical).exists())

    def test_command_preview_and_rejected_whole_history_replay_do_not_write(self):
        source = self.source()
        output = StringIO()
        with patch("management.services.ig_payment_review.create_payment_review") as review:
            call_command("reconcile_ig_payment_observations", message_id=source.pk, stdout=output)
        self.assertIn('"dry_run": true', output.getvalue())
        self.assertEqual(IgPaymentObservationSource.objects.count(), 0)
        review.assert_not_called()
        with self.assertRaises(CommandError):
            call_command("reconcile_ig_payment_observations")
        with self.assertRaises(CommandError):
            call_command("reconcile_ig_payment_observations", message_id=source.pk, allow_provider=True)

    @patch("management.services.ig_conversation_agreement.persist_conversation_agreement", return_value={"persisted": False})
    def test_standalone_ocr_failure_without_review_retains_retry_then_observes(self, agreement):
        source = self.source(text="", parts=[{"status": "owned", "source_part_id": "mp1_" + "a" * 32,
            "content_hash": "b" * 64, "private_storage": True}])

        def failed_recognition(*args, **kwargs):
            source.refresh_from_db()
            source.attachment_media[0]["receipt_inspection"] = {
                "state": "deferred", "reason": "receipt_provider_failed"}
            source.save(update_fields=["attachment_media"])
            return None

        with patch("management.services.ig_payment_review.create_payment_review", side_effect=failed_recognition):
            result = observation.observe_payment_source(source.pk)
        self.assertEqual(result.reason, "receipt_recognition_pending")
        row = IgPaymentObservationSource.objects.get(message=source)
        self.assertEqual(row.state, "pending")
        self.assertIsNone(row.outcome["review_id"])

        def successful_recognition(*args, **kwargs):
            source.refresh_from_db()
            source.attachment_media[0]["receipt_inspection"] = {"state": "inspected"}
            source.save(update_fields=["attachment_media"])
            return SimpleNamespace(pk=19, evidence={})

        with patch("management.services.ig_payment_review.create_payment_review", side_effect=successful_recognition):
            result = observation.observe_payment_source(source.pk, now=row.next_attempt_at)
        self.assertTrue(result.observed)
        row.refresh_from_db()
        self.assertEqual(row.state, "applied")
        self.assertEqual(row.outcome["review_id"], 19)
        self.assertEqual(row.attempts, 2)

    def test_receipt_capability_blocks_for_manual_and_quota_waits(self):
        source = self.source(parts=[{"receipt_inspection": {"state": "deferred", "reason": "receipt_capability_unavailable"}}])
        self.assertEqual(observation._pending_media(source, None, allow_provider=True)[0], "receipt_capability_unavailable")
        source.attachment_media[0]["receipt_inspection"]["reason"] = "receipt_quota_unavailable"
        reason, retry_at = observation._pending_media(source, None, allow_provider=True)
        self.assertEqual(reason, "receipt_recognition_pending")
        self.assertGreater(retry_at, timezone.now())

    def test_owned_generic_image_busy_before_inspection_cannot_complete_observation(self):
        source = self.source(text="", parts=[{"status": "owned", "private_storage": True,
            "mime": "image/jpeg", "source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64}])
        self.assertEqual(observation._pending_media(source, None, allow_provider=True)[0], "receipt_recognition_pending")
        source.attachment_media[0]["receipt_inspection"] = {"state": "inspected", "role": "other"}
        self.assertEqual(observation._pending_media(source, None, allow_provider=True)[0], "")

    def test_captionless_low_confidence_bound_receipt_is_unreadable_and_never_paid(self):
        source = self.source(text="", parts=[{"status": "owned", "private_storage": True,
            "source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64, "role": "other"}])
        from management.services.ig_conversation_routes import conversation_route_reset_floor
        bound = {"state": "uncertain", "role": "payment_candidate", "confidence": 0.3,
            "receipt_facts": {}, "source_message_id": source.pk}
        with patch("management.services.ig_receipt_inspection.bound_receipt_inspection", return_value=bound):
            result = observation.read_receipt_observation(self.client, episode_id=None,
                source_namespace=self.namespace, reset_floor=conversation_route_reset_floor(self.client.pk),
                watermark={"message_id": source.pk, "event_at": source.provider_created_at.isoformat()})
        self.assertEqual(result["observation"]["state"], "unreadable")
        self.assertTrue(result["observation"]["needs_manual"])
        self.assertFalse(result["observation"]["payment_verified"])
        self.assertEqual(result["observation"]["source_message_ids"], [source.pk])

    def test_reset_sources_do_not_starve_cutover_adoption(self):
        from management.models import IgFunnelResetAudit
        old = self.source()
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=old.pk, reason="test reset")
        current = self.source()
        with override_settings(IG_PAYMENT_OBSERVATION_CUTOVER_AT=old.created_at.isoformat()):
            self.assertEqual(observation.reconcile_payment_observation_sources(limit=1), 1)
        self.assertFalse(IgPaymentObservationSource.objects.filter(message=old).exists())
        self.assertTrue(IgPaymentObservationSource.objects.filter(message=current).exists())

    def test_crashed_claims_cannot_exceed_bounded_attempt_limit(self):
        source = self.source()
        observation.enqueue_payment_observation(source.pk)
        IgPaymentObservationSource.objects.filter(message=source).update(attempts=observation.MAX_ATTEMPTS)
        self.assertIsNone(observation._claim(source.pk))
        row = IgPaymentObservationSource.objects.get(message=source)
        self.assertEqual(row.state, "failed")
        self.assertEqual(row.outcome["receipt_disposition"], "needs_manual")
        self.assertEqual(row.attempts, observation.MAX_ATTEMPTS)

    @skipUnless(connection.vendor == "mysql", "Native MariaDB row locks required")
    def test_native_two_workers_claim_exactly_one_source(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        source = self.source()
        observation.enqueue_payment_observation(source.pk)
        barrier = Barrier(2)

        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return observation._claim(source.pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(claim), pool.submit(claim)
            outcomes = [first.result(timeout=15), second.result(timeout=15)]
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        row = IgPaymentObservationSource.objects.get(message=source)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.state, "processing")

    @skipUnless(connection.vendor == "mysql", "Native MariaDB reset visibility required")
    @patch("management.services.ig_conversation_agreement.persist_conversation_agreement", return_value={"persisted": False})
    def test_native_reset_before_dispatch_rejects_observation_provider_guard(self, agreement):
        from concurrent.futures import ThreadPoolExecutor
        from management.models import IgFunnelResetAudit
        source = self.source()
        provider_calls = []

        def reset():
            close_old_connections()
            try:
                IgFunnelResetAudit.objects.create(client_id=self.client.pk,
                    reset_after_message_id=source.pk, reason="concurrent reset")
            finally:
                close_old_connections()

        def review(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(reset).result(timeout=15)
            if kwargs["pre_dispatch_guard"]() is True:
                provider_calls.append("unexpected provider dispatch")
            return None

        with patch("management.services.ig_payment_review.create_payment_review", side_effect=review):
            result = observation.observe_payment_source(source.pk)
        self.assertEqual(result.reason, "source_reset_changed")
        self.assertEqual(provider_calls, [])
        self.assertEqual(IgPaymentObservationSource.objects.get(message=source).state, "blocked")

    def recognized_receipt(self):
        from management.services.ig_receipt_inspection import SCHEMA_VERSION
        source = self.source(text="", parts=[])
        source.private_media_state = "active"
        source.private_media_delete_after = timezone.now() + timedelta(days=1)
        source.attachment_media = [{"status": "owned", "private_storage": True,
            "role": "receipt", "source_part_id": "mp1_" + "a" * 32, "content_hash": "b" * 64,
            "delete_after": source.private_media_delete_after.isoformat(),
            "receipt_inspection": {"schema_version": SCHEMA_VERSION, "state": "inspected",
                "source_message_id": source.pk, "source_part_id": "mp1_" + "a" * 32,
                "content_hash": "b" * 64, "role": "receipt", "confidence": 0.99,
                "provider_model": "test-media-model", "request_id": "test-receipt-source",
                "receipt_facts": {"amount": "970.00", "currency": "UAH",
                    "recipient_name": "Test recipient", "payment_status": "completed"}}}]
        source.save(update_fields=["private_media_state", "private_media_delete_after", "attachment_media"])
        return source

    def receipt_read(self, watermark):
        from management.services.ig_conversation_routes import conversation_route_reset_floor
        return observation.read_receipt_observation(self.client,
            episode_id=self.client.current_commercial_episode_id, source_namespace=self.namespace,
            reset_floor=conversation_route_reset_floor(self.client.pk),
            watermark={"message_id": watermark.pk, "event_at": watermark.provider_created_at.isoformat()})

    def test_receipt_survives_more_than_eighty_plaintext_messages_without_provider_io(self):
        receipt = self.recognized_receipt()
        latest = receipt
        for _ in range(observation.CONTEXT_LIMIT + 5):
            latest = self.source(text="Дякую")
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, \
             patch("management.services.instagram_bot._capture_message_media") as capture:
            result = self.receipt_read(latest)
        self.assertEqual(result["observation"]["state"], "observed")
        self.assertEqual(result["observation"]["receipts"][0]["receipt_facts"]["amount"], "970.00")
        self.assertEqual(result["observation"]["source_message_ids"], [receipt.pk])
        self.assertFalse(result["observation"]["payment_verified"])
        self.assertTrue(self.client.manager_takeover)
        provider.assert_not_called()
        capture.assert_not_called()

        receipt.private_media_delete_after = timezone.now() - timedelta(seconds=1)
        receipt.save(update_fields=["private_media_delete_after"])
        self.assertEqual(self.receipt_read(latest)["observation"]["state"], "absent")
        receipt.private_media_delete_after = timezone.now() + timedelta(days=1)
        receipt.save(update_fields=["private_media_delete_after"])
        from management.models import IgFunnelResetAudit
        IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=receipt.pk, reason="source reset")
        self.assertEqual(self.receipt_read(latest)["observation"]["state"], "absent")

    def test_current_review_receipt_survives_more_than_eighty_unrelated_media_sources(self):
        from management.models import IgCommercialEpisode, IgPaymentConfirmationReview
        from management.services.ig_commercial_episodes import _new_episode
        receipt = self.recognized_receipt()
        review = IgPaymentConfirmationReview.objects.create(client=self.client,
            dedupe_key="receipt-read-current-review", watermark_message_id=receipt.pk,
            evidence={"media": [{"role": "receipt", "message_id": receipt.pk}]})
        with transaction.atomic():
            _new_episode(self.client, materialization_key="receipt-read-current-episode",
                repeat_kind=IgCommercialEpisode.RepeatKind.FIRST_PURCHASE, review=review,
                opened_watermark_message_id=receipt.pk)
        latest = receipt
        for _ in range(observation.CONTEXT_LIMIT + 5):
            latest = self.source(text="Інше фото", parts=[{"status": "metadata_only", "role": "product"}])
        result = self.receipt_read(latest)
        self.assertEqual(result["observation"]["state"], "observed")
        self.assertEqual(result["observation"]["receipts"][0]["receipt_facts"]["amount"], "970.00")
        self.assertEqual(result["observation"]["source_message_ids"], [receipt.pk])
        self.assertLessEqual(len(result["source_rows"]), observation.CONTEXT_LIMIT)
        self.assertEqual(review.status, review.Status.PENDING)

    def anchor_review(self, source, media):
        from management.models import IgCommercialEpisode, IgPaymentConfirmationReview
        from management.services.ig_commercial_episodes import _new_episode
        review = IgPaymentConfirmationReview.objects.create(client=self.client,
            dedupe_key="source-bound-current-review", watermark_message_id=source.pk,
            evidence={"media": media, "messages": [{"message_id": source.pk, "role": "user",
                "quote": " ".join(source.text.split())[:300]}]})
        with transaction.atomic():
            _new_episode(self.client, materialization_key="source-bound-current-episode",
                repeat_kind=IgCommercialEpisode.RepeatKind.FIRST_PURCHASE, review=review,
                opened_watermark_message_id=source.pk)
        return review

    def test_current_review_anchor_survives_eight_newer_recognized_receipts(self):
        receipt = self.recognized_receipt()
        self.anchor_review(receipt, [{"role": "receipt", "message_id": receipt.pk}])
        latest = receipt
        for _ in range(12):
            latest = self.recognized_receipt()
        result = self.receipt_read(latest)
        facts = result["observation"]["receipts"]
        self.assertEqual(len(facts), 8)
        self.assertEqual(facts[0]["source_message_id"], receipt.pk)
        self.assertEqual(facts[0]["receipt_facts"]["amount"], "970.00")
        self.assertIn(receipt.pk, result["observation"]["source_message_ids"])
        self.assertEqual(result["observation"]["omitted_receipt_count"], 5)

    def test_more_than_thirty_two_candidates_keep_anchor_and_bounded_matching_refs(self):
        receipt = self.recognized_receipt()
        self.anchor_review(receipt, [{"role": "receipt", "message_id": receipt.pk}])
        latest = receipt
        for _ in range(45):
            latest = self.source(parts=[{"role": "payment_candidate", "status": "owned",
                "private_storage": True, "source_part_id": "mp1_" + "c" * 32, "content_hash": "d" * 64}])
        result = self.receipt_read(latest)
        self.assertEqual(result["observation"]["receipts"][0]["source_message_id"], receipt.pk)
        self.assertEqual(len(result["source_refs"]), 32)
        self.assertEqual(len(result["source_rows"]), 32)
        self.assertEqual(result["observation"]["source_message_ids"],
            [row["message_id"] for row in result["source_refs"]])
        self.assertEqual([row["message_id"] for row in result["source_rows"]],
            [row["message_id"] for row in result["source_refs"]])
        self.assertTrue(result["observation"]["pending_sources"])
        self.assertEqual(result["observation"]["omitted_source_count"], 14)
        self.assertEqual(len(result["observation"]["omitted_source_message_ids"]), 14)

    def test_pdf_review_metadata_is_source_bound_manual_unreadable_without_ocr(self):
        source = self.source(text="Оплатив, чек PDF", parts=[{
            "source_part_id": "mp1_" + "e" * 32, "url": "https://lookaside.fbsbx.com/test.pdf",
            "type": "file", "mime": "application/pdf", "status": "owned", "private_storage": True,
            "storage_name": "test.pdf", "content_hash": "f" * 64,
            "delete_after": (timezone.now() + timedelta(hours=1)).isoformat()}])
        source.private_media_state = "active"
        source.private_media_delete_after = timezone.now() + timedelta(days=1)
        source.save(update_fields=["private_media_state", "private_media_delete_after"])
        part = {**source.attachment_media[0], "message_id": source.pk, "role": "payment_candidate",
            "receipt_inspection": {"state": "deferred", "reason": "receipt_document_not_readable"}}
        self.anchor_review(source, [part])
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, \
             patch("management.services.instagram_bot._capture_message_media") as capture:
            result = self.receipt_read(source)
        self.assertEqual(result["observation"]["state"], "unreadable")
        self.assertTrue(result["observation"]["needs_manual"])
        manual = result["observation"]["receipts"][0]
        self.assertEqual(manual["reason"], "receipt_document_not_readable")
        self.assertEqual(manual["receipt_facts"], {})
        self.assertNotIn("url", manual)
        provider.assert_not_called()
        capture.assert_not_called()
        source.attachment_media[0]["delete_after"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        source.save(update_fields=["attachment_media"])
        self.assertEqual(self.receipt_read(source)["observation"]["state"], "absent")

    def test_exact_text_receipt_link_is_manual_unreadable_without_fetch_and_mutation_rejected(self):
        source = self.source(text="Оплатив, чек https://receipts.example.test/receipt-351", parts=[])
        self.anchor_review(source, [{"message_id": source.pk, "type": "receipt_link", "role": "receipt",
            "url": "https://receipts.example.test/receipt-351"}])
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, \
             patch("management.services.instagram_bot.download_image") as fetch:
            result = self.receipt_read(source)
        self.assertEqual(result["observation"]["state"], "unreadable")
        self.assertEqual(result["observation"]["receipts"][0]["reason"], "receipt_link_not_fetched")
        self.assertEqual(result["observation"]["source_message_ids"], [source.pk])
        self.assertFalse(result["observation"]["payment_verified"])
        provider.assert_not_called()
        fetch.assert_not_called()
        source.text = "Це інша адреса https://receipts.example.test/another"
        source.save(update_fields=["text"])
        self.assertEqual(self.receipt_read(source)["observation"]["state"], "absent")
