"""Pure contracts for image-aware Instagram workflow and payment safety."""
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import os
import stat
import tempfile
from unittest.mock import Mock, mock_open, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import InstagramBotMessage, InstagramBotSettings


class MediaSemanticsTests(SimpleTestCase):
    def test_equal_rank_capture_merge_keeps_provider_native_provenance(self):
        from management.services.instagram_bot import _merge_attachment_media

        url = "https://lookaside.example/story-mention.jpg"
        existing = [{
            "url": url,
            "provenance": "live_webhook",
            "status": "pending",
            "media_type": "story_mention",
            "provider_object_key": "story_mention:story-object-1",
            "provider_media_id": "media-1",
            "provider_event_id": "mid-1",
            "target_username": "twocomms",
            "provider_native_mention": True,
        }]
        normalized = [{
            "url": url,
            "provenance": "live_webhook",
            "status": "pending",
            "media_type": "",
            "provider_object_key": "",
            "provider_media_id": "",
            "provider_event_id": "",
            "target_username": "",
            "provider_native_mention": False,
        }]

        merged = _merge_attachment_media(existing, normalized)

        self.assertEqual(merged[0]["provider_object_key"], "story_mention:story-object-1")
        self.assertEqual(merged[0]["provider_media_id"], "media-1")
        self.assertEqual(merged[0]["provider_event_id"], "mid-1")
        self.assertEqual(merged[0]["target_username"], "twocomms")
        self.assertTrue(merged[0]["provider_native_mention"])

    def test_expired_url_tombstone_survives_attachment_replay_without_refetch(self):
        from management.services.instagram_bot import (
            _media_part_capture_pending,
            _merge_attachment_media,
        )

        tombstone = {
            "source_part_id": "mp1_" + "a" * 32,
            "original_index": 0,
            "identity_origin": "ingress",
            "provenance": "live_webhook",
            "status": "unavailable",
            "error_kind": "download_failed",
            "url_metadata_expired": True,
        }
        replay = {
            "url": "https://lookaside.fbsbx.com/file?token=secret",
            "original_index": 0,
            "provenance": "live_webhook",
            "status": "pending",
        }

        merged = _merge_attachment_media([tombstone], [replay], message_scope="tombstone")

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_part_id"], tombstone["source_part_id"])
        self.assertNotIn("url", merged[0])
        self.assertFalse(_media_part_capture_pending(merged[0]))

    @patch("management.services.ig_payment_review._raw_media_by_mid")
    def test_historical_webhook_raw_media_stays_metadata_only_and_not_retryable(
        self, raw_media
    ):
        from management.services.ig_payment_review import (
            _augment_messages_with_raw_media,
            _review_media_needs_owned_retry,
        )

        raw_media.return_value = {
            "historical-webhook-mid": [{
                "url": "https://lookaside.example/historical-webhook.jpg",
                "type": "ig_post",
            }],
        }
        augmented = _augment_messages_with_raw_media(object(), [{
            "id": 1,
            "mid": "historical-webhook-mid",
            "role": "user",
            "source": "webhook",
            "media_capture_eligible": False,
        }])

        media = augmented[0]["media"]
        self.assertEqual(media[0]["provenance"], "historical_import")
        self.assertEqual(media[0]["status"], "metadata_only")
        self.assertFalse(_review_media_needs_owned_retry({"media": media}))

    @patch("management.services.ig_payment_review._raw_media_by_mid")
    def test_eligible_webhook_raw_media_is_pending_for_capture(self, raw_media):
        from management.services.ig_payment_review import _augment_messages_with_raw_media

        raw_media.return_value = {
            "eligible-webhook-mid": [{
                "url": "https://lookaside.example/live-webhook.jpg",
                "type": "ig_post",
            }],
        }
        augmented = _augment_messages_with_raw_media(object(), [{
            "id": 2,
            "mid": "eligible-webhook-mid",
            "role": "user",
            "source": "webhook",
            "media_capture_eligible": True,
        }])

        media = augmented[0]["media"]
        self.assertEqual(media[0]["provenance"], "live_webhook")
        self.assertEqual(media[0]["status"], "pending")

    @patch("management.services.ig_payment_review._raw_media_by_mid")
    def test_payment_review_does_not_bind_stale_unmatched_media_by_print_text(
        self, raw_media
    ):
        from management.services.ig_payment_review import (
            _augment_messages_with_raw_media,
        )

        raw_media.return_value = {"__unmatched__": [{
            "url": "https://lookaside.example/stale-payment-media.jpg",
            "event_at": (timezone.now() - timedelta(days=30)).isoformat(),
            "raw_event_id": 1001,
        }]}
        messages = [{
            "id": 1,
            "mid": "current-print-mid",
            "role": "user",
            "text": "Принт ось цей",
            "attachments": "",
            "attachment_media": [],
            "source": "webhook",
            "created_at": timezone.now().isoformat(),
        }]

        augmented = _augment_messages_with_raw_media(object(), messages)

        self.assertEqual(augmented[0]["media"], [])
        self.assertEqual(augmented[0]["attachments"], "")

    def test_terminal_unavailable_media_is_not_retryable(self):
        from management.services.ig_payment_review import _review_media_needs_owned_retry

        evidence = {
            "media": [{
                "url": "https://cdn.example/failed-receipt.jpg",
                "provenance": "live_webhook",
                "status": "unavailable",
                "capture_attempts": 2,
                "error_kind": "download_failed",
            }],
        }

        self.assertFalse(_review_media_needs_owned_retry(evidence))

    def test_pending_live_media_is_retryable(self):
        from management.services.ig_payment_review import _review_media_needs_owned_retry

        self.assertTrue(_review_media_needs_owned_retry({
            "media": [{
                "url": "https://cdn.example/pending-receipt.jpg",
                "provenance": "live_webhook",
                "status": "pending",
            }],
        }))

    def test_product_question_image_is_interest_not_receipt(self):
        from management.services.ig_payment_review import classify_media_items

        items = classify_media_items(
            "Який тут розмір і колір?",
            [{"url": "https://cdn.example/post.jpg", "type": "image"}],
        )
        self.assertEqual(items[0]["role"], "product")
        self.assertEqual(items[0]["intent"], "question")
        self.assertFalse(items[0]["payment_evidence"])

    def test_purchase_image_is_actionable_product_candidate(self):
        from management.services.ig_payment_review import classify_media_items

        items = classify_media_items(
            "Хочу саме таку, оформлюйте",
            [{"url": "https://cdn.example/photo.jpg", "type": "image"}],
        )
        self.assertEqual(items[0]["role"], "product")
        self.assertEqual(items[0]["intent"], "purchase_candidate")
        self.assertTrue(items[0]["actionable"])

    def test_product_reference_after_order_is_actionable(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {
                "id": 233,
                "role": "user",
                "text": "Мені потрібно 2 футболки: 1. Базова S 2. Оверсайз XS. Принт однаковий",
            },
            {"id": 234, "role": "user", "text": "Почекаємо)"},
            {
                "id": 235,
                "role": "user",
                "text": "Принт ось цей",
                "attachments": '["https://cdn.example/product.jpg"]',
            },
            {"id": 236, "role": "manager", "text": "Оплата на IBAN, надішліть чек"},
            {
                "id": 237,
                "role": "user",
                "text": "Оплатила, ось чек",
                "attachments": '["https://cdn.example/receipt.jpg"]',
            },
        ])

        product_media = [item for item in result["media"] if item.get("url") == "https://cdn.example/product.jpg"]
        self.assertEqual(len(product_media), 1)
        self.assertEqual(product_media[0]["intent"], "purchase_candidate")
        self.assertTrue(product_media[0]["actionable"])
        self.assertTrue(product_media[0]["catalog_match_allowed"])

    def test_product_media_attached_to_order_lines_is_actionable(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {
                "id": 233,
                "role": "user",
                "text": "Мені потрібно 2 футболки: 1. Базова S 2. Оверсайз XS",
                "media": [{"url": "https://cdn.example/order-product.jpg", "type": "ig_post"}],
            },
        ])

        product_media = result["media"]
        self.assertEqual(len(product_media), 1)
        self.assertEqual(product_media[0]["intent"], "purchase_candidate")
        self.assertTrue(product_media[0]["actionable"])

    def test_custom_reference_is_not_catalog_product(self):
        from management.services.ig_payment_review import classify_media_items

        items = classify_media_items(
            "Можете зробити такий принт на футболці?",
            [{"url": "https://cdn.example/reference.jpg", "type": "image"}],
        )
        self.assertEqual(items[0]["role"], "custom_reference")
        self.assertEqual(items[0]["intent"], "custom_print_request")
        self.assertFalse(items[0]["catalog_match_allowed"])

    def test_receipt_wins_over_product_language_in_payment_context(self):
        from management.services.ig_payment_review import classify_media_items

        items = classify_media_items(
            "Оплатила, ось чек",
            [{"url": "https://cdn.example/receipt.jpg", "type": "image"}],
            payment_context=True,
        )
        self.assertEqual(items[0]["role"], "receipt")
        self.assertEqual(items[0]["intent"], "payment_evidence")
        self.assertTrue(items[0]["payment_evidence"])
        self.assertFalse(items[0]["catalog_match_allowed"])

    def test_product_question_stays_product_after_earlier_payment_context(self):
        from management.services.ig_payment_review import classify_media_items

        items = classify_media_items(
            "Підкажіть, який тут розмір?",
            [{"url": "https://cdn.example/product.jpg", "type": "image"}],
            payment_context=True,
        )
        self.assertEqual(items[0]["role"], "product")
        self.assertEqual(items[0]["intent"], "question")

    def test_unrelated_image_stays_unresolved(self):
        from management.services.ig_payment_review import classify_media_items

        items = classify_media_items(
            "",
            [{"url": "https://cdn.example/random.jpg", "type": "image"}],
        )
        self.assertEqual(items[0]["role"], "other")
        self.assertEqual(items[0]["intent"], "unknown")
        self.assertTrue(items[0]["uncertain"])

    def test_manager_payment_instruction_does_not_turn_product_question_into_receipt(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "manager", "text": "Оплата на IBAN, після цього надішліть чек.", "attachments": ""},
            {"id": 2, "role": "user", "text": "Який тут розмір?", "attachments": '["https://cdn.example/product.jpg"]'},
        ])
        self.assertFalse(result["needs_review"])
        self.assertEqual([item["role"] for item in result["media"]], ["product"])
        self.assertFalse(any(item["role"] == "receipt" for item in result["media"]))

    def test_old_payment_context_does_not_turn_later_generic_image_into_receipt(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "Беру оверсайз XS"},
            {"id": 2, "role": "manager", "text": "Оплата на IBAN, сума 790 грн. Надішліть чек."},
            {"id": 3, "role": "user", "text": "Дякую, ще подумаю"},
            {"id": 4, "role": "user", "text": "", "attachments": '["https://cdn.example/unrelated.jpg"]'},
        ])

        self.assertFalse(result["needs_review"])
        self.assertEqual(result["media"][-1]["role"], "other")

    def test_image_immediately_after_receipt_request_is_payment_evidence(self):
        from management.services.ig_payment_review import extract_payment_review_evidence

        result = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "Беру оверсайз XS"},
            {"id": 2, "role": "manager", "text": "Оплата на IBAN, сума 790 грн. Надішліть чек."},
            {"id": 3, "role": "user", "text": "", "attachments": '["https://cdn.example/receipt.jpg"]'},
        ])

        self.assertTrue(result["needs_review"])
        self.assertEqual(result["media"][-1]["role"], "payment_candidate")

    @patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", b"image"))
    @patch("management.services.bot_vision.classify_media_roles", return_value=[{
        "source_image_index": 0,
        "role": "product",
        "confidence": 0.96,
        "reason": "скриншот картки товару",
    }])
    def test_vision_can_reclassify_payment_candidate_as_product(self, classify, _download):
        from management.services.ig_payment_review import _resolve_payment_media_candidates

        media = [{
            "url": "https://cdn.example/unknown.jpg",
            "provenance": "live_webhook",
            "status": "owned",
            "storage_name": "ig_message_media/unknown.jpg",
            "mime": "image/jpeg",
            "role": "payment_candidate",
            "intent": "payment_evidence_candidate",
            "payment_evidence": True,
            "catalog_match_allowed": False,
        }]
        resolved = _resolve_payment_media_candidates(media)

        self.assertEqual(resolved[0]["role"], "product")
        self.assertEqual(resolved[0]["intent"], "interest")
        self.assertFalse(resolved[0]["payment_evidence"])
        self.assertFalse(resolved[0]["catalog_match_allowed"])
        classify.assert_called_once_with([("image/jpeg", b"image")])

    def test_vision_product_result_removes_image_only_payment_evidence(self):
        from management.services.ig_payment_review import (
            _reconcile_payment_evidence_after_media_resolution,
            extract_payment_review_evidence,
        )

        extracted = extract_payment_review_evidence([
            {"id": 1, "role": "user", "text": "Беру базову S"},
            {"id": 2, "role": "manager", "text": "Оплата на IBAN, надішліть чек"},
            {"id": 3, "role": "user", "text": "", "attachments": '["https://cdn.example/product.jpg"]'},
        ])
        self.assertTrue(extracted["needs_review"])

        reconciled = _reconcile_payment_evidence_after_media_resolution(
            extracted,
            [{"url": "https://cdn.example/product.jpg", "message_id": 3, "role": "product"}],
        )

        self.assertFalse(reconciled["needs_review"])
        self.assertEqual(reconciled["message_ids"], [])
        self.assertEqual(reconciled["evidence"], [])

    def test_explicit_payment_statement_survives_product_image_reclassification(self):
        from management.services.ig_payment_review import (
            _reconcile_payment_evidence_after_media_resolution,
            extract_payment_review_evidence,
        )

        extracted = extract_payment_review_evidence([
            {
                "id": 3,
                "role": "user",
                "text": "Я вже оплатила, перевірте, будь ласка",
                "attachments": '["https://cdn.example/product.jpg"]',
            },
        ])
        reconciled = _reconcile_payment_evidence_after_media_resolution(
            extracted,
            [{"url": "https://cdn.example/product.jpg", "message_id": 3, "role": "product"}],
        )

        self.assertTrue(reconciled["needs_review"])
        self.assertEqual(reconciled["message_ids"], [3])

    @patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", b"image"))
    @patch("management.services.bot_vision.classify_media_roles", return_value=[{
        "source_image_index": 0,
        "role": "receipt",
        "confidence": 0.4,
        "reason": "нечітко",
    }])
    def test_low_confidence_vision_keeps_payment_candidate_unresolved(self, _classify, _download):
        from management.services.ig_payment_review import _resolve_payment_media_candidates

        media = [{
            "url": "https://cdn.example/unknown.jpg",
            "provenance": "live_webhook",
            "status": "owned",
            "storage_name": "ig_message_media/unknown.jpg",
            "mime": "image/jpeg",
            "role": "payment_candidate",
        }]
        resolved = _resolve_payment_media_candidates(media)

        self.assertEqual(resolved[0]["role"], "payment_candidate")
        self.assertTrue(resolved[0]["uncertain"])


class PrivateMessageMediaStorageTests(TestCase):
    def test_erasure_during_download_prevents_storage_write(self):
        from management.models import IgClient
        from management.services import instagram_bot

        client = IgClient.objects.create(igsid="erase-during-download")
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid, client=client,
            role=InstagramBotMessage.Role.USER, source="webhook",
            mid="erase-during-download-mid", media_capture_eligible=True,
            attachment_media=[{
                "url": "https://lookaside.invalid/photo.png",
                "provenance": "live_webhook", "status": "pending",
            }],
        )

        def download(_url, **_kwargs):
            IgClient.objects.filter(pk=client.pk).update(
                privacy_erasure_started_at=timezone.now(),
            )
            return "image/png", b"private-test-bytes"

        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ), patch.object(instagram_bot, "download_image", side_effect=download), patch(
            "management.services.ig_private_media.HardenedPrivateMediaStorage._save"
        ) as save:
            instagram_bot._capture_message_media(row)

        save.assert_not_called()
        row.refresh_from_db()
        self.assertNotEqual(row.attachment_media[0]["status"], "owned")

    def _private_row(self, private_root, *, sender):
        from django.core.files.base import ContentFile
        from management.services.ig_private_media import private_media_storage

        with override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve())
        ):
            storage = private_media_storage()
            name = storage.save(
                f"ig_message_media/{sender}/voice.ogg",
                ContentFile(b"leased-voice"),
            )
        row = InstagramBotMessage.objects.create(
            sender_id=sender,
            role=InstagramBotMessage.Role.USER,
            private_media_state="active",
            private_media_delete_after=timezone.now() + timedelta(hours=1),
            attachment_media=[{
                "status": "owned",
                "private_storage": True,
                "storage_name": name,
                "mime": "audio/ogg",
                "content_hash": "hash",
            }],
        )
        return row, name

    def test_production_missing_private_root_is_a_deploy_error(self):
        from management.checks import private_instagram_media_check

        with override_settings(DEBUG=False, IG_PRIVATE_MEDIA_ROOT=""):
            errors = private_instagram_media_check(None)

        self.assertEqual([error.id for error in errors], ["management.E901"])

    @patch("management.services.instagram_bot.download_image")
    def test_missing_production_root_fails_before_customer_media_download(self, download):
        from django.core.exceptions import ImproperlyConfigured
        from management.services.instagram_bot import _capture_message_media

        row = InstagramBotMessage.objects.create(
            sender_id="missing-private-root",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            mid="missing-private-root-mid",
            media_capture_eligible=True,
            attachment_media=[{
                "url": "https://lookaside.invalid/private.jpg",
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )
        with override_settings(DEBUG=False, IG_PRIVATE_MEDIA_ROOT=""):
            with self.assertRaises(ImproperlyConfigured):
                _capture_message_media(row)

        download.assert_not_called()

    def test_private_root_inside_public_media_is_rejected(self):
        from django.core.exceptions import ImproperlyConfigured
        from management.services.instagram_bot import _private_media_storage

        with tempfile.TemporaryDirectory() as public_root, override_settings(
            MEDIA_ROOT=public_root,
            IG_PRIVATE_MEDIA_ROOT=str(Path(public_root) / "private"),
        ):
            with self.assertRaises(ImproperlyConfigured):
                _private_media_storage()

    def test_private_root_rejects_wrong_mode_and_symlink(self):
        from django.core.exceptions import ImproperlyConfigured
        from management.services.instagram_bot import _private_media_storage

        with tempfile.TemporaryDirectory() as root:
            canonical = Path(root).resolve()
            os.chmod(canonical, 0o755)
            with override_settings(IG_PRIVATE_MEDIA_ROOT=str(canonical)):
                with self.assertRaises(ImproperlyConfigured):
                    _private_media_storage()
            os.chmod(canonical, 0o700)
            link = canonical.parent / f"{canonical.name}-link"
            link.symlink_to(canonical, target_is_directory=True)
            self.addCleanup(link.unlink, missing_ok=True)
            with override_settings(IG_PRIVATE_MEDIA_ROOT=str(link)):
                with self.assertRaises(ImproperlyConfigured):
                    _private_media_storage()

    def test_private_root_must_be_owned_by_worker_euid(self):
        from django.core.exceptions import ImproperlyConfigured
        from management.services.instagram_bot import _private_media_storage

        with tempfile.TemporaryDirectory() as root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(root).resolve()),
        ), patch(
            "management.services.ig_private_media.os.geteuid",
            return_value=os.geteuid() + 1,
        ):
            with self.assertRaises(ImproperlyConfigured):
                _private_media_storage()

    def test_delete_waits_for_blob_use_lease_then_finalizes(self):
        from management.services.ig_private_media import (
            acquire_blob_use,
            private_media_storage,
            purge_due,
            release_blob_use,
            request_deletion,
        )

        with tempfile.TemporaryDirectory() as private_root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve()),
        ):
            row, name = self._private_row(
                private_root,
                sender="private-lease-delete",
            )
            token = acquire_blob_use(row.pk, seconds=120)
            self.assertTrue(token)
            now = timezone.now()
            request_deletion([row.pk], now=now, immediate=True)

            self.assertEqual(purge_due(now=now, limit=1), 0)
            self.assertTrue(private_media_storage().exists(name))
            release_blob_use(row.pk, token)
            self.assertEqual(purge_due(now=now, limit=1), 1)

            row.refresh_from_db()
            self.assertEqual(row.private_media_state, "deleted")
            self.assertFalse(private_media_storage().exists(name))

    def test_late_capture_finish_becomes_deletion_debt_after_privacy_fence(self):
        from django.core.files.base import ContentFile
        from management.models import IgClient
        from management.services import instagram_bot
        from management.services.ig_private_media import (
            delete_immediately,
            private_media_storage,
        )

        with tempfile.TemporaryDirectory() as private_root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve()),
        ):
            client = IgClient.objects.create(igsid="late-capture-fence")
            url = "https://lookaside.invalid/late.jpg"
            row = InstagramBotMessage.objects.create(
                sender_id=client.igsid,
                client=client,
                role=InstagramBotMessage.Role.USER,
                source="webhook",
                mid="late-capture-fence-mid",
                media_capture_eligible=True,
                attachment_media=[{
                    "url": url,
                    "provenance": "live_webhook",
                    "status": "pending",
                }],
            )
            capture_token, _item, use_token = instagram_bot._claim_media_capture(
                row.pk,
                url,
            )
            client.privacy_erasure_started_at = timezone.now()
            client.save(update_fields=["privacy_erasure_started_at", "updated_at"])
            row.private_media_state = "delete_pending"
            row.private_media_delete_after = timezone.now()
            row.save(update_fields=[
                "private_media_state", "private_media_delete_after",
            ])
            storage = private_media_storage()
            name = storage.save(
                f"ig_message_media/{row.pk}/late.jpg",
                ContentFile(b"late-capture"),
            )

            media = instagram_bot._finish_media_capture(
                row.pk,
                url,
                capture_token,
                {
                    "status": "owned",
                    "storage_name": name,
                    "private_storage": True,
                    "mime": "image/jpeg",
                    "bytes": 12,
                    "content_hash": "late",
                    "delete_after": timezone.now().isoformat(),
                },
                use_token=use_token,
            )

            self.assertEqual(media[0]["status"], "delete_pending")
            self.assertEqual(delete_immediately([row.pk]), 1)
            self.assertFalse(storage.exists(name))
            row.refresh_from_db()
            self.assertEqual(row.private_media_state, "deleted")

    def test_capture_claim_refuses_client_erasure_fence(self):
        from management.models import IgClient
        from management.services.instagram_bot import _claim_media_capture

        client = IgClient.objects.create(
            igsid="capture-claim-fenced",
            privacy_erasure_started_at=timezone.now(),
        )
        url = "https://lookaside.invalid/fenced.jpg"
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            media_capture_eligible=True,
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )

        self.assertIsNone(_claim_media_capture(row.pk, url))

    def test_ingress_cannot_create_message_after_client_erasure_fence(self):
        from management.models import IgClient, InstagramBotSettings
        from management.services.instagram_bot import enqueue_inbound

        client = IgClient.objects.create(
            igsid="ingress-erasure-fenced",
            privacy_erasure_started_at=timezone.now(),
        )
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.allowed_senders = ""
        settings_obj.save(update_fields=["is_enabled", "allowed_senders"])

        created = enqueue_inbound(
            settings_obj,
            sender_id=client.igsid,
            text="new media",
            mid="ingress-erasure-fenced-mid",
            source="webhook",
            attachments=["https://lookaside.invalid/after-fence.jpg"],
            received_at=timezone.now(),
        )

        self.assertFalse(created)
        self.assertFalse(InstagramBotMessage.objects.filter(
            mid="ingress-erasure-fenced-mid"
        ).exists())

    def test_stale_delete_claim_recovers_after_blob_was_already_removed(self):
        from management.services.ig_private_media import (
            claim_deletion,
            private_media_storage,
            purge_due,
            request_deletion,
        )

        with tempfile.TemporaryDirectory() as private_root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve()),
        ):
            row, name = self._private_row(
                private_root,
                sender="private-crash-delete",
            )
            now = timezone.now()
            request_deletion([row.pk], now=now, immediate=True)
            claim = claim_deletion(row.pk, now=now)
            self.assertIsNotNone(claim)
            private_media_storage().delete(name)
            InstagramBotMessage.objects.filter(pk=row.pk).update(
                private_media_delete_claimed_at=now - timedelta(minutes=10),
            )

            self.assertEqual(purge_due(now=now, limit=1), 1)
            row.refresh_from_db()
            self.assertEqual(row.private_media_state, "deleted")
            self.assertEqual(row.attachment_media[0]["status"], "expired")

    @patch("management.services.instagram_bot.download_image")
    def test_audio_is_private_has_no_public_url_and_is_purged(self, download):
        from management.services.instagram_bot import (
            _capture_message_media,
            _private_media_storage,
            purge_expired_private_message_media,
        )
        from management.models import IgClient

        download.return_value = ("audio/ogg", b"private-voice")
        with tempfile.TemporaryDirectory() as private_root, tempfile.TemporaryDirectory() as public_root:
            with override_settings(
                IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve()),
                IG_PRIVATE_MEDIA_RETENTION_SECONDS=3600,
                MEDIA_ROOT=public_root,
            ):
                client = IgClient.objects.create(igsid="private-audio-client")
                row = InstagramBotMessage.objects.create(
                    sender_id=client.igsid,
                    client=client,
                    role=InstagramBotMessage.Role.USER,
                    source="webhook",
                    mid="private-audio-mid",
                    media_capture_eligible=True,
                    attachments='["https://lookaside.invalid/voice.ogg"]',
                    attachment_media=[{
                        "url": "https://lookaside.invalid/voice.ogg",
                        "media_type": "audio",
                        "provenance": "live_webhook",
                        "status": "pending",
                    }],
                )

                _capture_message_media(row)

                row.refresh_from_db()
                owned = row.attachment_media[0]
                self.assertTrue(owned["private_storage"])
                self.assertNotIn("local_url", owned)
                private_path = Path(private_root) / owned["storage_name"]
                self.assertTrue(private_path.exists())
                self.assertFalse((Path(public_root) / owned["storage_name"]).exists())
                self.assertEqual(stat.S_IMODE(os.stat(private_root).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(private_path.stat().st_mode), 0o600)
                with self.assertRaises(ValueError):
                    _private_media_storage().url(owned["storage_name"])
                self.assertIsNotNone(row.private_media_delete_after)

                purged = purge_expired_private_message_media(
                    now=row.private_media_delete_after + timedelta(seconds=1),
                )

                self.assertEqual(purged, 1)
                row.refresh_from_db()
                self.assertEqual(row.attachment_media[0]["status"], "expired")
                self.assertNotIn("storage_name", row.attachment_media[0])
                self.assertIsNone(row.private_media_delete_after)

    @patch("requests.post")
    def test_private_audio_is_uploaded_directly_without_public_url(self, post):
        from management.services.instagram_bot import (
            _private_media_storage,
            _telegram_private_media_call,
        )

        observed = {}

        def send(url, *, data, files, timeout):
            observed.update({
                "url": url,
                "data": data,
                "field": next(iter(files)),
                "mime": next(iter(files.values()))[2],
                "raw": next(iter(files.values()))[1].read(),
                "timeout": timeout,
            })
            return SimpleNamespace(status_code=200, text='{"ok":true}')

        post.side_effect = send
        with tempfile.TemporaryDirectory() as private_root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve()),
        ):
            row = InstagramBotMessage.objects.create(
                sender_id="private-telegram-audio",
                role=InstagramBotMessage.Role.USER,
                private_media_state="active",
            )
            _private_media_storage()
            name = "ig_message_media/voice.ogg"
            path = Path(private_root) / name
            path.parent.mkdir(parents=True)
            path.write_bytes(b"voice")
            code, body = _telegram_private_media_call(
                token="redacted-token",
                chat="777",
                media={
                    "private_storage_name": name,
                    "mime": "audio/ogg",
                    "message_id": str(row.pk),
                },
                caption="Voice evidence",
                reply_to_message_id="10",
            )

        self.assertEqual((code, body), (200, '{"ok":true}'))
        self.assertTrue(observed["url"].endswith("/sendAudio"))
        self.assertEqual(observed["field"], "audio")
        self.assertEqual(observed["mime"], "audio/ogg")
        self.assertEqual(observed["raw"], b"voice")


class HistoricalAttachmentOwnershipTests(TestCase):
    def setUp(self):
        from management.models import IgClient

        self.client = IgClient.get_or_create_for_sender("historical-media-owner")

    def message(self, *, source: str, url: str):
        return InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось фото",
            status=InstagramBotMessage.Status.DONE,
            source=source,
            attachments=json.dumps([url]),
            media_capture_eligible=source == "webhook",
        )

    @patch("management.services.instagram_bot.download_image")
    def test_historical_attachment_is_metadata_only_and_never_downloaded(self, download):
        from management.services import instagram_bot

        row = self.message(
            source="manual_refresh",
            url="https://lookaside.example/expired-history.jpg",
        )

        media = instagram_bot._capture_message_media(row)

        download.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(media, row.attachment_media)
        self.assertEqual(media[0]["provenance"], "historical_import")
        self.assertEqual(media[0]["status"], "metadata_only")
        self.assertNotIn("storage_name", media[0])

    @patch("management.services.instagram_bot.download_image")
    def test_webhook_ingress_persists_live_provenance_without_network_io(self, download):
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "allowed_senders"])
        url = "https://lookaside.example/new-webhook.jpg"

        self.assertTrue(instagram_bot.enqueue_inbound(
            settings,
            sender_id="owned-media-webhook",
            text="Ось фото",
            mid="owned-media-webhook-mid",
            source="webhook",
            attachments=[url],
        ))

        row = InstagramBotMessage.objects.get(mid="owned-media-webhook-mid")
        self.assertEqual(len(row.attachment_media), 1)
        part = row.attachment_media[0]
        self.assertEqual(part["url"], url)
        self.assertEqual(part["provenance"], "live_webhook")
        self.assertEqual(part["status"], "pending")
        self.assertEqual(part["original_index"], 0)
        self.assertEqual(part["identity_origin"], "ingress")
        self.assertTrue(part["source_part_id"].startswith("mp1_"))
        self.assertEqual(part["capture_state"], "discovered")
        self.assertEqual(part["inspection"]["state"], "uninspected")
        download.assert_not_called()

    def test_persisted_inspection_updates_only_hash_bound_part(self):
        from management.services import instagram_bot

        owned_hash = hashlib.sha256(b"owned").hexdigest()
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Два фото",
            source="webhook",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        row.attachment_media = instagram_bot._normalize_message_media([
            {
                "url": "https://lookaside.example/owned.jpg",
                "provenance": "live_webhook",
                "status": "owned",
                "content_hash": owned_hash,
            },
            {
                "url": "https://lookaside.example/missing.jpg",
                "provenance": "live_webhook",
                "status": "unavailable",
                "error_kind": "download_failed",
            },
        ], message_scope=row.pk, identity_origin="ingress")
        row.save(update_fields=["attachment_media"])
        first = row.attachment_media[0]
        artifact = {
            "schema_version": 1,
            "image_observations": [{
                "source_part_id": first["source_part_id"],
                "content_hash": owned_hash,
                "outcome": "understood",
                "evidence_code": "visual_content",
                "type_code": "product",
            }],
            "media_request": {
                "request_id": "request-bound",
                "provider_model": "gemini-actual",
                "inline_count_known": True,
                "actual_inline_count": 1,
                "submitted_parts": [{
                    "source_part_id": first["source_part_id"],
                    "content_hash": owned_hash,
                }],
            },
        }

        instagram_bot._persist_turn_intelligence(row, artifact)
        row.refresh_from_db()

        self.assertEqual(row.attachment_media[0]["inspection"]["state"], "inspected")
        self.assertEqual(
            row.attachment_media[1]["inspection"]["outcome"],
            "capture_unavailable",
        )
        self.assertEqual(row.turn_intelligence_artifact["media_coverage"]["inspected"], 1)
        self.assertEqual(row.turn_intelligence_artifact["media_coverage"]["missing"], 1)

    def test_wrong_hash_reply_artifact_does_not_mark_part_inspected(self):
        from management.services import instagram_bot

        owned_hash = hashlib.sha256(b"owned").hexdigest()
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Фото",
            source="webhook",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        row.attachment_media = instagram_bot._normalize_message_media([{
            "url": "https://lookaside.example/hash.jpg",
            "provenance": "live_webhook",
            "status": "owned",
            "content_hash": owned_hash,
        }], message_scope=row.pk, identity_origin="ingress")
        row.save(update_fields=["attachment_media"])
        part = row.attachment_media[0]
        wrong_hash = hashlib.sha256(b"other").hexdigest()

        instagram_bot._persist_turn_intelligence(row, {
            "schema_version": 1,
            "image_observations": [{
                "source_part_id": part["source_part_id"],
                "content_hash": wrong_hash,
                "outcome": "understood",
            }],
            "media_request": {
                "request_id": "request-wrong",
                "provider_model": "gemini-actual",
                "inline_count_known": True,
                "actual_inline_count": 1,
                "submitted_parts": [{
                    "source_part_id": part["source_part_id"],
                    "content_hash": wrong_hash,
                }],
            },
        })
        row.refresh_from_db()

        self.assertEqual(row.attachment_media[0]["inspection"]["state"], "uninspected")

    def test_erasure_fence_rejects_late_inspection_persistence(self):
        from management.services import instagram_bot

        owned_hash = hashlib.sha256(b"owned").hexdigest()
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Фото",
            source="webhook",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        row.attachment_media = instagram_bot._normalize_message_media([{
            "url": "https://lookaside.example/erased.jpg",
            "provenance": "live_webhook",
            "status": "owned",
            "content_hash": owned_hash,
        }], message_scope=row.pk, identity_origin="ingress")
        row.save(update_fields=["attachment_media"])
        part = row.attachment_media[0]
        self.client.privacy_erasure_started_at = timezone.now()
        self.client.save(update_fields=["privacy_erasure_started_at"])

        instagram_bot._persist_turn_intelligence(row, {
            "schema_version": 1,
            "image_observations": [{
                "source_part_id": part["source_part_id"],
                "content_hash": owned_hash,
                "outcome": "understood",
            }],
            "media_request": {
                "request_id": "request-erased",
                "provider_model": "gemini-actual",
                "inline_count_known": True,
                "actual_inline_count": 1,
                "submitted_parts": [{
                    "source_part_id": part["source_part_id"],
                    "content_hash": owned_hash,
                }],
            },
        })
        row.refresh_from_db()

        self.assertEqual(row.turn_intelligence_artifact, {})
        self.assertEqual(row.attachment_media[0]["inspection"]["state"], "uninspected")

    def test_equal_url_parts_are_claimed_and_finalized_independently(self):
        from management.services import instagram_bot

        url = "https://lookaside.example/equal-source.jpg"
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Два фото",
            source="webhook",
            status=InstagramBotMessage.Status.PROCESSING,
            media_capture_eligible=True,
        )
        row.attachment_media = instagram_bot._normalize_message_media([
            {"url": url, "provenance": "live_webhook", "status": "pending"},
            {"url": url, "provenance": "live_webhook", "status": "pending"},
        ], message_scope=row.pk, identity_origin="ingress")
        row.save(update_fields=["attachment_media"])
        first_id, second_id = [
            item["source_part_id"] for item in row.attachment_media
        ]

        first_token, _first, first_use = instagram_bot._claim_media_capture(
            row.pk,
            first_id,
        )
        instagram_bot._finish_media_capture(
            row.pk,
            first_id,
            first_token,
            {
                "status": "owned",
                "storage_name": "private/first.jpg",
                "private_storage": True,
                "mime": "image/jpeg",
                "bytes": 5,
                "content_hash": hashlib.sha256(b"first").hexdigest(),
            },
            use_token=first_use,
        )
        second_token, _second, second_use = instagram_bot._claim_media_capture(
            row.pk,
            second_id,
        )
        instagram_bot._finish_media_capture(
            row.pk,
            second_id,
            second_token,
            {
                "status": "owned",
                "storage_name": "private/second.jpg",
                "private_storage": True,
                "mime": "image/jpeg",
                "bytes": 6,
                "content_hash": hashlib.sha256(b"second").hexdigest(),
            },
            use_token=second_use,
        )
        row.refresh_from_db()

        self.assertEqual(len(row.attachment_media), 2)
        self.assertEqual(
            [item["storage_name"] for item in row.attachment_media],
            ["private/first.jpg", "private/second.jpg"],
        )
        self.assertEqual(
            [item["source_part_id"] for item in row.attachment_media],
            [first_id, second_id],
        )

    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"new-live-image"),
    )
    @patch("management.services.instagram_bot._private_media_storage")
    def test_history_promotion_downloads_only_the_new_live_attachment(
        self,
        storage,
        download,
    ):
        from management.services import instagram_bot
        storage = storage.return_value

        old_url = "https://lookaside.example/expired-import.jpg"
        live_url = "https://lookaside.example/new-live.jpg"
        event_at = timezone.now()
        existing = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось фото",
            mid="promoted-owned-media-mid",
            status=InstagramBotMessage.Status.DONE,
            source="manual_refresh",
            attachments=json.dumps([old_url]),
            provider_created_at=event_at,
            processed_at=event_at,
        )
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.reply_after = event_at - timedelta(seconds=1)
        settings.save(update_fields=["is_enabled", "allowed_senders", "reply_after"])

        self.assertTrue(instagram_bot.enqueue_inbound(
            settings,
            sender_id=self.client.igsid,
            text="Ось фото",
            mid=existing.mid,
            source="webhook",
            attachments=[live_url],
            received_at=event_at,
        ))
        existing.refresh_from_db()
        storage.exists.return_value = False
        storage.save.side_effect = lambda name, _content: name
        storage.open.return_value = mock_open(read_data=b"new-live-image").return_value
        storage.url.return_value = "/media/ig_message_media/promoted/new.jpg"

        media = instagram_bot._capture_message_media(existing)

        self.assertEqual(download.call_args_list[0].args, (live_url,))
        self.assertEqual(download.call_count, 1)
        self.assertEqual(
            [(item["url"], item["provenance"]) for item in media],
            [
                (old_url, "historical_import"),
                (live_url, "live_webhook"),
            ],
        )

    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"same-url-live-image"),
    )
    @patch("management.services.instagram_bot._private_media_storage")
    def test_delayed_webhook_promotes_same_historical_url_to_live_owned(
        self, storage, download
    ):
        from management.services import instagram_bot
        storage = storage.return_value

        url = "https://lookaside.example/same-delayed-url.jpg"
        event_at = timezone.now()
        existing = InstagramBotMessage.objects.create(
            client=self.client, sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER, text="Ось фото",
            mid="same-url-promotion-mid", status=InstagramBotMessage.Status.DONE,
            source="manual_refresh", attachments=json.dumps([url]),
            attachment_media=[{
                "url": url, "provenance": "historical_import",
                "status": "metadata_only",
            }],
            provider_created_at=event_at, processed_at=event_at,
        )
        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.allowed_senders = ""
        settings.reply_after = event_at - timedelta(seconds=1)
        settings.save(update_fields=["is_enabled", "allowed_senders", "reply_after"])

        self.assertTrue(instagram_bot.enqueue_inbound(
            settings, sender_id=self.client.igsid, text="Ось фото",
            mid=existing.mid, source="webhook", attachments=[url],
            received_at=event_at,
        ))
        existing.refresh_from_db()
        storage.exists.return_value = False
        storage.save.side_effect = lambda name, _content: name
        storage.open.return_value = mock_open(read_data=b"same-url-live-image").return_value
        storage.url.return_value = "/media/ig_message_media/promoted/same.jpg"

        media = instagram_bot._capture_message_media(existing)

        self.assertEqual(download.call_count, 1)
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["provenance"], "live_webhook")
        self.assertEqual(media[0]["status"], "owned")

    @patch("management.services.instagram_bot.download_image")
    def test_stale_historical_capture_cannot_overwrite_live_owned_media(self, download):
        from management.services import instagram_bot

        url = "https://lookaside.example/stale-history.jpg"
        existing = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось фото",
            mid="stale-owned-mid",
            status=InstagramBotMessage.Status.DONE,
            source="webhook",
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "owned",
                "storage_name": "ig_message_media/stale-owned.jpg",
                "local_url": "/media/ig_message_media/stale-owned.jpg",
                "mime": "image/jpeg",
                "bytes": 5,
            }],
        )
        stale = InstagramBotMessage.objects.get(pk=existing.pk)
        stale.attachment_media = [{
            "url": url,
            "provenance": "historical_import",
            "status": "metadata_only",
        }]

        instagram_bot._capture_message_media(stale)

        existing.refresh_from_db()
        self.assertEqual(existing.attachment_media[0]["status"], "owned")
        self.assertEqual(
            existing.attachment_media[0]["storage_name"],
            "ig_message_media/stale-owned.jpg",
        )
        download.assert_not_called()

    @patch("management.services.instagram_bot.download_image", return_value=None)
    def test_migrated_historical_webhook_url_stays_metadata_only(self, download):
        from management.services import instagram_bot

        url = "https://lookaside.example/imported-webhook-history.jpg"
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось фото",
            mid="imported-webhook-history-mid",
            status=InstagramBotMessage.Status.DONE,
            source="webhook",
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "historical_import",
                "status": "metadata_only",
            }],
        )

        media = instagram_bot._capture_message_media(row)

        self.assertEqual(media[0]["provenance"], "historical_import")
        self.assertEqual(media[0]["status"], "metadata_only")
        download.assert_not_called()

    @patch("management.services.instagram_bot.download_image", return_value=None)
    @patch("management.services.ig_payment_review._raw_media_by_mid")
    def test_stale_unmatched_raw_media_is_not_bound_by_print_text(
        self, raw_media, download
    ):
        from management.services import instagram_bot

        event_at = timezone.now() - timedelta(days=30)
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Принт ось цей",
            mid="stale-unmatched-mid",
            status=InstagramBotMessage.Status.DONE,
            source="webhook",
            attachments="",
            media_capture_eligible=True,
            provider_created_at=timezone.now(),
        )
        raw_media.return_value = {"__unmatched__": [{
            "url": "https://lookaside.example/old-unmatched.jpg",
            "event_at": event_at.isoformat(),
            "raw_event_id": 999,
        }]}

        media = instagram_bot._capture_message_media(row)

        self.assertEqual(media, [])
        download.assert_not_called()

    @patch("management.services.instagram_bot._private_media_storage")
    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"fresh-webhook-image"),
    )
    def test_live_webhook_media_is_owned_once_and_reused_without_redownload(
        self,
        download,
        storage,
    ):
        from management.services import instagram_bot
        storage = storage.return_value

        storage.exists.return_value = False
        storage.save.side_effect = lambda name, _content: name
        storage.open.side_effect = lambda *_args, **_kwargs: mock_open(
            read_data=b"fresh-webhook-image"
        ).return_value
        row = self.message(
            source="webhook",
            url="https://lookaside.example/fresh-webhook.jpg",
        )

        first = instagram_bot._capture_message_media(row)
        second = instagram_bot._capture_message_media(row)
        images = instagram_bot._collect_media_images(second, message_id=row.pk)

        self.assertEqual(download.call_count, 1)
        self.assertEqual(storage.save.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(second[0]["provenance"], "live_webhook")
        self.assertEqual(second[0]["status"], "owned")
        self.assertEqual(images, [("image/jpeg", b"fresh-webhook-image")])

    @patch("management.services.ig_payment_review._raw_media_by_mid")
    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"raw-live-image"),
    )
    @patch("management.services.instagram_bot._private_media_storage")
    def test_raw_only_live_webhook_media_is_captured_before_analysis(
        self,
        storage,
        download,
        raw_media,
    ):
        from management.services import instagram_bot
        storage = storage.return_value

        storage.exists.return_value = False
        storage.save.side_effect = lambda name, _content: name
        storage.open.return_value = mock_open(read_data=b"raw-live-image").return_value
        raw_media.return_value = {
            "raw-only-mid": [{
                "url": "https://lookaside.example/raw-live.jpg",
                "type": "ig_post",
                "raw_event_id": 77,
            }],
        }
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось цей принт",
            mid="raw-only-mid",
            status=InstagramBotMessage.Status.DONE,
            source="webhook",
            attachments="",
            media_capture_eligible=True,
        )

        media = instagram_bot._capture_message_media(row)

        self.assertEqual(download.call_args.args, ("https://lookaside.example/raw-live.jpg",))
        self.assertEqual(media[0]["provenance"], "live_webhook")
        self.assertEqual(media[0]["status"], "owned")
        self.assertTrue(media[0]["storage_name"].startswith(f"ig_message_media/{row.pk}/"))

    @patch("management.services.ig_payment_review._raw_media_by_mid")
    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"expired-history"),
    )
    def test_legacy_raw_only_webhook_media_is_never_promoted_to_live(
        self,
        download,
        raw_media,
    ):
        from management.services import instagram_bot

        raw_media.return_value = {
            "legacy-raw-only-mid": [{
                "url": "https://lookaside.example/expired-raw-history.jpg",
                "type": "ig_post",
                "raw_event_id": 79,
            }],
        }
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Старий принт",
            mid="legacy-raw-only-mid",
            status=InstagramBotMessage.Status.DONE,
            source="webhook",
            attachments="",
            provider_created_at=timezone.now() - timedelta(days=30),
        )

        media = instagram_bot._capture_message_media(row)

        self.assertEqual(media, [])
        download.assert_not_called()

    @patch("management.services.ig_payment_review._raw_media_by_mid")
    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"unmatched-live-image"),
    )
    @patch("management.services.instagram_bot._private_media_storage")
    def test_unmatched_raw_live_media_uses_timestamp_bounded_capture(
        self, storage, download, raw_media
    ):
        from management.services import instagram_bot
        storage = storage.return_value

        storage.exists.return_value = False
        storage.save.side_effect = lambda name, _content: name
        storage.open.return_value = mock_open(read_data=b"unmatched-live-image").return_value
        storage.url.return_value = "/media/ig_message_media/raw/unmatched.jpg"
        row = InstagramBotMessage.objects.create(
            client=self.client, sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER, text="Принт ось цей",
            mid="normalized-mid", status=InstagramBotMessage.Status.DONE,
            source="webhook", attachments="", provider_created_at=timezone.now(),
            media_capture_eligible=True,
        )
        raw_media.return_value = {"__unmatched__": [{
            "url": "https://lookaside.example/unmatched-live.jpg",
            "type": "ig_post", "event_at": row.provider_created_at.isoformat(),
            "raw_event_id": 78,
        }]}

        media = instagram_bot._capture_message_media(row)

        self.assertEqual(download.call_count, 1)
        self.assertEqual(media[0]["provenance"], "live_webhook")
        self.assertEqual(media[0]["status"], "owned")

    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"one-image"),
    )
    @patch("management.services.instagram_bot._private_media_storage")
    def test_capture_budget_does_not_delete_unacquired_metadata(self, storage, download):
        from management.services import instagram_bot
        storage = storage.return_value

        storage.exists.return_value = False
        storage.save.side_effect = lambda name, _content: name
        storage.open.return_value = mock_open(read_data=b"one-image").return_value
        storage.url.return_value = "/media/ig_message_media/budget/0.jpg"
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Три фото",
            status=InstagramBotMessage.Status.DONE,
            source="webhook",
            media_capture_eligible=True,
            attachments=json.dumps([
                "https://lookaside.example/one.jpg",
                "https://lookaside.example/two.jpg",
                "https://lookaside.example/three.jpg",
            ]),
            attachment_media=[
                {"url": url, "provenance": "live_webhook", "status": "pending"}
                for url in (
                    "https://lookaside.example/one.jpg",
                    "https://lookaside.example/two.jpg",
                    "https://lookaside.example/three.jpg",
                )
            ],
        )

        media = instagram_bot._capture_message_media(row, limit=1)

        self.assertEqual(len(media), 3)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(sum(item["status"] == "owned" for item in media), 1)
        self.assertEqual(sum(item["status"] == "pending" for item in media), 2)

    @patch("management.services.instagram_bot.download_image")
    def test_unknown_media_provenance_never_crosses_network_boundary(self, download):
        from management.services.instagram_bot import _collect_media_images
        from management.services.ig_payment_review import _resolve_payment_media_candidates

        unknown = {
            "url": "https://lookaside.example/unknown.jpg",
            "role": "payment_candidate",
            "intent": "payment_evidence",
        }

        self.assertEqual(_collect_media_images([unknown]), [])
        self.assertEqual(_resolve_payment_media_candidates([unknown]), [unknown])
        download.assert_not_called()

    def test_media_capture_claim_serializes_workers_and_preserves_owned_result(self):
        from management.services import instagram_bot

        url = "https://lookaside.example/race.jpg"
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось фото",
            status=InstagramBotMessage.Status.PROCESSING,
            source="webhook",
            media_capture_eligible=True,
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )

        first = instagram_bot._claim_media_capture(row.pk, url)
        second = instagram_bot._claim_media_capture(row.pk, url)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

        token, _item, use_token = first
        instagram_bot._finish_media_capture(row.pk, url, token, {
            "status": "owned",
            "storage_name": "ig_message_media/race.jpg",
            "local_url": "/media/ig_message_media/race.jpg",
            "mime": "image/jpeg",
            "bytes": 4,
            "content_hash": "abcd",
        }, use_token=use_token)

        self.assertIsNone(instagram_bot._claim_media_capture(row.pk, url))
        row.refresh_from_db()
        self.assertEqual(row.attachment_media[0]["status"], "owned")

    @patch("management.services.instagram_bot.download_image")
    def test_after_save_crash_resumes_verified_prepared_blob_without_refetch(
        self,
        download,
    ):
        from django.core.files.base import ContentFile
        from management.services import instagram_bot
        from management.services.ig_media_recovery import (
            prepared_blob_descriptor,
        )
        from management.services.ig_private_media import private_media_storage

        with tempfile.TemporaryDirectory() as private_root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve()),
        ):
            url = "https://lookaside.example/prepared-crash.jpg"
            row = InstagramBotMessage.objects.create(
                client=self.client,
                sender_id=self.client.igsid,
                role=InstagramBotMessage.Role.USER,
                text="Ось фото",
                status=InstagramBotMessage.Status.PROCESSING,
                source="webhook",
                media_capture_eligible=True,
                attachments=json.dumps([url]),
                attachment_media=[{
                    "url": url,
                    "provenance": "live_webhook",
                    "status": "pending",
                }],
            )
            token, item, use_token = instagram_bot._claim_media_capture(row.pk, url)
            raw = b"already-saved-private-bytes"
            descriptor = prepared_blob_descriptor(
                storage_name=f"ig_message_media/{row.pk}/prepared.jpg",
                mime_type="image/jpeg",
                body_bytes=raw,
            )
            self.assertIsNotNone(instagram_bot._persist_prepared_media_blob(
                row.pk,
                item["source_part_id"],
                token,
                use_token,
                descriptor,
            ))
            storage = private_media_storage()
            self.assertEqual(
                storage.save(descriptor["storage_name"], ContentFile(raw)),
                descriptor["storage_name"],
            )
            row.refresh_from_db()
            media = [dict(part) for part in row.attachment_media]
            media[0]["capture_started_at"] = (
                timezone.now() - timedelta(minutes=10)
            ).isoformat()
            row.attachment_media = media
            row.private_media_use_until = timezone.now() - timedelta(seconds=1)
            row.save(update_fields=["attachment_media", "private_media_use_until"])

            recovered = instagram_bot._capture_message_media(row)

            download.assert_not_called()
            self.assertEqual(recovered[0]["status"], "owned")
            self.assertEqual(
                recovered[0]["content_hash"],
                descriptor["content_hash"],
            )
            self.assertEqual(recovered[0].get("prepared_blob"), {})

    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"fresh-valid-bytes"),
    )
    def test_bad_prepared_file_is_deleted_and_refetched_not_accepted(
        self,
        download,
    ):
        from django.core.files.base import ContentFile
        from management.services import instagram_bot
        from management.services.ig_media_recovery import prepared_blob_descriptor
        from management.services.ig_private_media import private_media_storage

        with tempfile.TemporaryDirectory() as private_root, override_settings(
            IG_PRIVATE_MEDIA_ROOT=str(Path(private_root).resolve()),
        ):
            url = "https://lookaside.example/partial-crash.jpg"
            row = InstagramBotMessage.objects.create(
                client=self.client,
                sender_id=self.client.igsid,
                role=InstagramBotMessage.Role.USER,
                text="Ось фото",
                status=InstagramBotMessage.Status.PROCESSING,
                source="webhook",
                media_capture_eligible=True,
                attachments=json.dumps([url]),
                attachment_media=[{
                    "url": url,
                    "provenance": "live_webhook",
                    "status": "pending",
                }],
            )
            token, item, use_token = instagram_bot._claim_media_capture(row.pk, url)
            descriptor = prepared_blob_descriptor(
                storage_name=f"ig_message_media/{row.pk}/partial.jpg",
                mime_type="image/jpeg",
                body_bytes=b"expected-complete-bytes",
            )
            instagram_bot._persist_prepared_media_blob(
                row.pk,
                item["source_part_id"],
                token,
                use_token,
                descriptor,
            )
            storage = private_media_storage()
            storage.save(
                descriptor["storage_name"],
                ContentFile(b"partial"),
            )
            row.refresh_from_db()
            media = [dict(part) for part in row.attachment_media]
            media[0]["capture_started_at"] = (
                timezone.now() - timedelta(minutes=10)
            ).isoformat()
            row.attachment_media = media
            row.private_media_use_until = timezone.now() - timedelta(seconds=1)
            row.save(update_fields=["attachment_media", "private_media_use_until"])

            recovered = instagram_bot._capture_message_media(row)

            self.assertEqual(download.call_count, 1)
            self.assertFalse(storage.exists(descriptor["storage_name"]))
            self.assertEqual(recovered[0]["status"], "owned")
            self.assertEqual(
                recovered[0]["content_hash"],
                hashlib.sha256(b"fresh-valid-bytes").hexdigest(),
            )

    def test_stale_capture_token_cannot_finalize_after_part_is_reclaimed(self):
        from management.services import instagram_bot

        url = "https://lookaside.example/stale-token.jpg"
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось фото",
            status=InstagramBotMessage.Status.PROCESSING,
            source="webhook",
            media_capture_eligible=True,
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )
        first_token, first_item, first_use = instagram_bot._claim_media_capture(
            row.pk,
            url,
        )
        row.refresh_from_db()
        media = [dict(part) for part in row.attachment_media]
        media[0]["capture_started_at"] = (
            timezone.now() - timedelta(minutes=10)
        ).isoformat()
        row.attachment_media = media
        row.private_media_use_until = timezone.now() - timedelta(seconds=1)
        row.save(update_fields=["attachment_media", "private_media_use_until"])
        second_token, _second_item, second_use = instagram_bot._claim_media_capture(
            row.pk,
            first_item["source_part_id"],
        )

        instagram_bot._finish_media_capture(
            row.pk,
            first_item["source_part_id"],
            first_token,
            {
                "status": "owned",
                "storage_name": "private/stale.jpg",
                "mime": "image/jpeg",
                "bytes": 5,
                "content_hash": hashlib.sha256(b"stale").hexdigest(),
            },
            use_token=first_use,
        )
        row.refresh_from_db()
        self.assertEqual(row.attachment_media[0]["status"], "acquiring")
        self.assertEqual(row.attachment_media[0]["capture_token"], second_token)

        instagram_bot._finish_media_capture(
            row.pk,
            first_item["source_part_id"],
            second_token,
            {
                "status": "owned",
                "storage_name": "private/current.jpg",
                "mime": "image/jpeg",
                "bytes": 7,
                "content_hash": hashlib.sha256(b"current").hexdigest(),
            },
            use_token=second_use,
        )
        row.refresh_from_db()
        self.assertEqual(row.attachment_media[0]["storage_name"], "private/current.jpg")

    @patch("management.services.instagram_bot.download_image")
    @patch("management.services.instagram_bot._private_media_storage")
    def test_permanent_mime_failure_is_typed_and_never_retried(
        self,
        _storage,
        download,
    ):
        from management.services import ig_media_url_policy, instagram_bot

        def rejected(_url, *, failure_context=None, **_kwargs):
            failure_context["outcome"] = ig_media_url_policy.FetchOutcome(
                success=False,
                reason=ig_media_url_policy.REASON_CONTENT_TYPE,
                status_code=200,
            )
            return None

        download.side_effect = rejected
        url = "https://lookaside.example/not-an-image"
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Ось файл",
            status=InstagramBotMessage.Status.PROCESSING,
            source="webhook",
            media_capture_eligible=True,
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )

        first = instagram_bot._capture_message_media(row)
        second = instagram_bot._capture_message_media(row)

        self.assertEqual(download.call_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["error_kind"], "content_type")
        self.assertEqual(first[0]["capture_failure_class"], "permanent")
        self.assertFalse(first[0]["capture_retryable"])
        self.assertTrue(first[0]["capture_terminal"])
        self.assertEqual(first[0]["resolution_action"], "request_resend")

    def test_reply_worker_captures_live_media_before_rule_classifier(self):
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = False
        settings.save(update_fields=["is_enabled", "ai_enabled"])
        url = "https://lookaside.example/payment.jpg"
        row = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Я оплатила, ось чек",
            status=InstagramBotMessage.Status.PROCESSING,
            processing_started_at=timezone.now(),
            source="webhook",
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )
        calls = []

        def capture(target, *args, **kwargs):
            calls.append("capture")
            target.attachment_media = [{
                "url": url,
                "provenance": "live_webhook",
                "status": "owned",
                "storage_name": "ig_message_media/payment.jpg",
                "mime": "image/jpeg",
            }]
            target.save(update_fields=["attachment_media"])
            return target.attachment_media

        def classify(_client, _message, *, media_context=None, **_kwargs):
            calls.append("classify")
            self.assertEqual(media_context[0]["status"], "owned")
            return {"interaction_type": "reaction_only"}

        with (
            patch.object(instagram_bot, "_capture_message_media", side_effect=capture),
            patch.object(instagram_bot, "_recover_current_message_media", side_effect=lambda target: target.attachment_media),
            patch.object(
                instagram_bot,
                "_collect_media_images",
                return_value=[("image/jpeg", b"owned-payment-image")],
            ),
            patch.object(instagram_bot, "_persist_commerce_turn", return_value=(None, None)),
            patch(
                "management.services.bot_sales_classifier.ensure_rule_classification",
                side_effect=classify,
            ),
        ):
            handled = instagram_bot._process_one_inside_reply_boundary(
                settings,
                row,
                permission=object(),
            )

        self.assertTrue(handled)
        self.assertEqual(calls[:2], ["capture", "classify"])

    @patch("management.services.bot_vision.match_many")
    @patch("management.services.bot_vision.classify_media_roles")
    @patch("management.services.instagram_bot.download_image")
    def test_historical_media_is_blocked_from_every_payment_vision_path(
        self,
        download,
        classify,
        match_many,
    ):
        from management.services.ig_payment_review import (
            _catalog_matches_for_media,
            _persist_review_media,
            _resolve_payment_media_candidates,
        )

        media = [{
            "url": "https://lookaside.example/expired-payment.jpg",
            "provenance": "historical_import",
            "status": "metadata_only",
            "role": "payment_candidate",
            "intent": "payment_evidence",
            "payment_evidence": True,
            "catalog_match_allowed": False,
        }]

        self.assertEqual(_resolve_payment_media_candidates(media), media)
        self.assertEqual(_persist_review_media(media), media)
        product_media = [{
            **media[0],
            "role": "product",
            "intent": "purchase_candidate",
            "actionable": True,
            "catalog_match_allowed": True,
        }]
        self.assertEqual(_catalog_matches_for_media(product_media), [])
        download.assert_not_called()
        classify.assert_not_called()
        match_many.assert_not_called()

    @patch("management.services.bot_vision.classify_media_roles")
    @patch("management.services.instagram_bot.download_image")
    def test_historical_local_media_url_never_reenters_payment_vision_network(
        self,
        download,
        classify,
    ):
        from management.services.ig_payment_review import _resolve_payment_media_candidates

        media = [{
            "url": "https://lookaside.example/expired-payment.jpg",
            "local_url": "/media/ig_payment_reviews/expired-payment.jpg",
            "provenance": "historical_import",
            "status": "metadata_only",
            "role": "payment_candidate",
            "intent": "payment_evidence",
            "payment_evidence": True,
            "catalog_match_allowed": False,
        }]

        resolved = _resolve_payment_media_candidates(media)

        self.assertEqual(resolved, media)
        download.assert_not_called()
        classify.assert_not_called()


class ReplyMediaRecoveryTests(SimpleTestCase):
    @override_settings(SITE_BASE_URL="https://twocomms.shop")
    def test_relative_persisted_media_url_is_absolutized_with_original_fallback(self):
        from management.services.instagram_bot import _telegram_media_url_candidates

        urls = _telegram_media_url_candidates({
            "local_url": "/media/ig_payment_reviews/evidence.jpg",
            "url": "https://lookaside.example/original.jpg",
        })
        self.assertEqual(urls, [
            "https://twocomms.shop/media/ig_payment_reviews/evidence.jpg",
            "https://lookaside.example/original.jpg",
        ])

    @patch("management.services.ig_payment_review._augment_messages_with_raw_media")
    def test_customer_worker_can_recover_raw_product_media(self, augment):
        from management.services import instagram_bot

        augment.return_value = [{
            "id": 10,
            "mid": "normalized-mid",
            "text": "Хочу таку",
            "attachments": '["https://cdn.example/post.jpg"]',
            "media": [{"url": "https://cdn.example/post.jpg", "type": "ig_post"}],
        }]
        row = SimpleNamespace(
            pk=10,
            mid="normalized-mid",
            text="Хочу таку",
            attachments="",
            role="user",
            client=SimpleNamespace(igsid="ig-1"),
        )
        recovered = instagram_bot._recover_current_message_media(row)
        self.assertEqual(recovered[0]["type"], "ig_post")
        self.assertIn("post.jpg", recovered[0]["url"])
        augment.assert_called_once()

    def test_receipt_media_never_reaches_catalog_matcher(self):
        from management.services.instagram_bot import _catalog_match_media

        media = [{"url": "https://cdn.example/receipt.jpg", "role": "receipt", "catalog_match_allowed": False}]
        self.assertEqual(_catalog_match_media(media), [])

    def test_only_explicit_product_media_reaches_catalog_matcher(self):
        from management.services.instagram_bot import _catalog_match_media

        product = {"url": "https://cdn.example/product.jpg", "role": "product", "catalog_match_allowed": True}
        receipt = {"url": "https://cdn.example/receipt.jpg", "role": "receipt", "catalog_match_allowed": False}
        self.assertEqual(_catalog_match_media([receipt, product]), [product])

    def test_echo_media_metadata_is_bounded_and_preserves_type(self):
        from management.services.instagram_bot import _echo_media_items

        items = _echo_media_items({
            "attachments": [
                {"type": "ig_post", "payload": {"url": "https://cdn.example/post.jpg"}},
                {"type": "image", "payload": {"url": "https://cdn.example/extra.jpg"}},
            ],
        })
        self.assertEqual(items[0]["type"], "ig_post")
        self.assertEqual(len(items), 2)


class PaymentLinkGateTests(SimpleTestCase):
    # F-DEBT-005: `finalize_paylink` читает БД (`services/instagram_bot.py`,
    # поиск сделки по клиенту), поэтому `SimpleTestCase` падал с
    # `DatabaseOperationForbidden`. Объявляем доступ к БД явно вместо
    # смены базового класса — так тест остаётся без транзакционной обёртки,
    # как и был задуман.
    databases = {"default"}

    def test_product_question_cannot_open_payment_link(self):
        from management.services.instagram_bot import payment_link_allowed

        client = SimpleNamespace(
            current_product_id=111,
            intent="product",
            stage="product_matched",
            buying_readiness=20,
        )
        self.assertFalse(payment_link_allowed(client, {"paylink": "full", "product": 111}, "Який розмір?"))

    def test_explicit_purchase_candidate_can_open_payment_link(self):
        from management.services.instagram_bot import payment_link_allowed

        client = SimpleNamespace(
            current_product_id=111,
            intent="product",
            stage="product_matched",
            buying_readiness=20,
        )
        self.assertTrue(payment_link_allowed(client, {"paylink": "full", "product": 111}, "Так, хочу, оформлюйте"))

    def test_explicit_payment_question_with_complete_configuration_can_open_link(self):
        from management.services.instagram_bot import payment_link_allowed

        client = SimpleNamespace(
            current_product_id=111,
            current_size="M",
            intent="payment",
            stage="product_matched",
            buying_readiness=80,
        )
        control = {"paylink": "full", "product": 111, "size": "M", "fit": "classic"}
        self.assertTrue(payment_link_allowed(client, control, "Как я могу оплатить?"))

    def test_stage_paid_alone_no_longer_blocks_a_repeat_purchase(self):
        """Стадия `paid` — рабочее состояние воронки, а не факт «больше не купит».

        Раньше здесь закреплялось обратное, и вместе с блокировкой по
        `client_has_verified_payment` это означало: любой, кто хоть раз оплатил,
        никогда больше не получит ссылку. Постоянный клиент не мог купить второй
        раз. Ирония в том, что W3 (IMP-013) как раз научила систему видеть
        покупателей — и этот гейт начал резать именно их.

        Дубль счёта отсекается точнее: по факту денег, то есть по оплаченной
        сделке без созданного заказа (`_has_open_paid_deal`), а не по стадии.
        """
        from management.services.instagram_bot import payment_link_allowed

        client = SimpleNamespace(
            pk=None,
            current_product_id=111,
            intent="payment",
            stage="paid",
            buying_readiness=100,
        )
        self.assertTrue(payment_link_allowed(client, {"paylink": "full", "product": 111}, "Так, хочу"))

    @patch("management.services.instagram_bot._has_open_paid_deal", return_value=True)
    def test_open_paid_deal_blocks_a_duplicate_invoice(self, _open_paid):
        """Оплаченная сделка без заказа — единственная причина не выдавать ссылку."""
        from management.services.instagram_bot import payment_link_allowed

        client = SimpleNamespace(
            pk=1,
            current_product_id=111,
            intent="payment",
            stage="checkout",
            buying_readiness=80,
        )
        self.assertFalse(payment_link_allowed(client, {"paylink": "full", "product": 111}, "Так, хочу"))

    def test_boolean_product_tag_is_not_a_product_id(self):
        from management.services.instagram_bot import payment_link_allowed

        client = SimpleNamespace(
            current_product_id=None,
            intent="payment",
            stage="checkout",
            buying_readiness=80,
        )
        self.assertFalse(payment_link_allowed(client, {"paylink": "full", "product": True}, "Так, хочу"))

    def test_only_purchase_candidate_media_can_pin_a_product(self):
        from management.services.instagram_bot import _should_pin_product_media

        self.assertFalse(_should_pin_product_media([
            {"role": "product", "intent": "question", "catalog_match_allowed": True},
        ]))
        self.assertTrue(_should_pin_product_media([
            {"role": "product", "intent": "purchase_candidate", "catalog_match_allowed": True},
        ]))

    @patch("management.services.instagram_bot.notify_manager")
    @patch("management.services.bot_orders.create_deal_and_link")
    @patch("management.services.instagram_bot._conversation_negotiated_price", return_value=None)
    def test_unverified_price_tag_fails_closed_before_provider_invoice(self, _price, create_link, notify):
        from management.services.instagram_bot import finalize_paylink

        client = SimpleNamespace(
            current_product_id=111,
            intent="payment",
            stage="checkout",
            buying_readiness=80,
            username="client",
            display_name="",
            set_stage=lambda *args, **kwargs: None,
        )
        reply = "Так, хочу. Ось посилання на оплату"
        result = finalize_paylink(
            reply,
            {"paylink": "full", "product": 111, "price": "500"},
            client,
            "ig-1",
        )
        create_link.assert_not_called()
        notify.assert_called_once()
        self.assertNotIn("посилання на оплат", result.casefold())

    @patch("management.services.bot_orders._validated_negotiated_price", return_value=Decimal("790.00"))
    def test_omitted_price_tag_still_uses_validated_conversation_offer(self, validate):
        from management.services.instagram_bot import _conversation_negotiated_price

        client = SimpleNamespace(pk=1)
        self.assertEqual(_conversation_negotiated_price(client, {}), Decimal("790.00"))
        validate.assert_called_once_with(client, None)


class NegotiatedPriceEvidenceTests(SimpleTestCase):
    def test_customer_cannot_self_authorize_an_arbitrary_price(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [SimpleNamespace(role="user", text="Так, оформлюйте за 20 грн")]
        self.assertIsNone(_accepted_conversation_price(rows))

    def test_unrelated_later_yes_does_not_accept_old_offer(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="manager", text="Ціна 790 грн"),
            SimpleNamespace(role="user", text="Який склад?"),
            SimpleNamespace(role="manager", text="Бавовна. Підходить?"),
            SimpleNamespace(role="user", text="Так"),
        ]
        self.assertIsNone(_accepted_conversation_price(rows))

    def test_product_switch_invalidates_an_earlier_accepted_price(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="manager", text="Ціна худі 790 грн"),
            SimpleNamespace(role="user", text="Так, оформлюйте"),
            SimpleNamespace(role="user", text="Ні, хочу іншу футболку"),
        ]
        self.assertIsNone(_accepted_conversation_price(rows))

    def test_prepayment_receipt_amount_cannot_replace_accepted_order_price(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="manager", text="Ціна футболки зі знижкою 790 грн"),
            SimpleNamespace(role="user", text="Так, оформлюйте"),
            SimpleNamespace(role="user", text="Оплатила передоплату 200 грн, ось чек"),
        ]
        self.assertEqual(_accepted_conversation_price(rows), Decimal("790.00"))
        self.assertIsNone(_accepted_conversation_price(rows, requested=Decimal("200")))

    def test_later_offer_supersedes_earlier_accepted_amount(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="manager", text="Домовились, ціна 700 грн"),
            SimpleNamespace(role="user", text="Так"),
            SimpleNamespace(role="manager", text="Актуальна сума разом 900 грн"),
            SimpleNamespace(role="user", text="Добре, оформлюйте"),
        ]
        self.assertEqual(_accepted_conversation_price(rows), Decimal("900.00"))
        self.assertIsNone(_accepted_conversation_price(rows, requested=Decimal("700")))

    def test_model_cannot_accept_customer_counteroffer(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="user", text="Можна за 20 грн?"),
            SimpleNamespace(role="model", text="Так, оформлюємо"),
        ]
        self.assertIsNone(_accepted_conversation_price(rows))

    def test_model_cannot_originate_price_override(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="assistant", text="Можу зробити за 20 грн"),
            SimpleNamespace(role="user", text="Так, оформлюйте"),
        ]
        self.assertIsNone(_accepted_conversation_price(rows))

    def test_manager_price_for_same_product_remains_authoritative(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="manager", text="Можу оформити цю футболку за 790 грн"),
            SimpleNamespace(role="user", text="Так, оформлюйте"),
        ]
        product = SimpleNamespace(pk=111, title="Футболка Харків", slug="kharkiv")
        self.assertEqual(_accepted_conversation_price(rows, product=product), Decimal("790.00"))

    def test_manager_can_accept_customer_counteroffer(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="user", text="Можна за 790 грн?"),
            SimpleNamespace(role="manager", text="Так, домовились"),
        ]
        self.assertEqual(_accepted_conversation_price(rows), Decimal("790.00"))

    def test_named_product_selection_starts_a_new_price_epoch(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            SimpleNamespace(role="manager", text="Ціна худі 790 грн"),
            SimpleNamespace(role="user", text="Так, оформлюйте"),
            SimpleNamespace(role="user", text="Тоді беру оверсайз Харків"),
        ]
        product = SimpleNamespace(pk=111, title="Футболка Харків Вокзальна Oversize")
        self.assertIsNone(_accepted_conversation_price(rows, product=product))

    def test_product_image_selection_starts_a_new_price_epoch(self):
        from management.services.bot_orders import _accepted_conversation_price

        rows = [
            {"role": "manager", "text": "Ціна футболки 790 грн"},
            {"role": "user", "text": "Так, оформлюйте"},
            {
                "role": "user",
                "text": "Хочу цю",
                "media": [{"url": "https://cdn.example/new.jpg", "role": "product"}],
            },
        ]
        product = SimpleNamespace(pk=111, title="Футболка Харків", slug="kharkiv")
        self.assertIsNone(_accepted_conversation_price(rows, product=product))

    def test_manual_draft_does_not_prefill_customer_counteroffer(self):
        from management.services.ig_payment_review import (
            _apply_catalog_matches_to_draft,
            _apply_validated_conversation_price_to_draft,
            extract_payment_review_evidence,
        )

        messages = [
            {"id": 1, "role": "user", "text": "Беру базову S за 20 грн"},
            {"id": 2, "role": "user", "text": "Я оплатила, ось чек", "attachments": "receipt.jpg"},
        ]
        extracted = extract_payment_review_evidence(messages)
        matches = [{"status": "matched", "product_id": 111, "title": "Базова футболка"}]
        _apply_validated_conversation_price_to_draft(extracted["order_draft"], messages, matches)

        self.assertEqual(extracted["order_draft"]["quoted_total"], "")
        self.assertIsNone(extracted["order_draft"]["items"][0]["unit_price"])
        self.assertIn(
            "conversation_price_not_authorized",
            extracted["order_draft"]["uncertainty_reasons"],
        )

    def test_manual_draft_uses_only_manager_accepted_price(self):
        from management.services.ig_payment_review import (
            _apply_catalog_matches_to_draft,
            _apply_validated_conversation_price_to_draft,
            extract_payment_review_evidence,
        )

        messages = [
            {"id": 1, "role": "manager", "text": "Можу оформити цю футболку за 790 грн"},
            {"id": 2, "role": "user", "text": "Так, оформлюйте"},
            {"id": 3, "role": "user", "text": "Я оплатила, ось чек", "attachments": "receipt.jpg"},
        ]
        extracted = extract_payment_review_evidence(messages)
        matches = [{"status": "matched", "product_id": 111, "title": "Базова футболка"}]
        _apply_catalog_matches_to_draft(extracted["order_draft"], matches)
        _apply_validated_conversation_price_to_draft(
            extracted["order_draft"],
            messages,
            matches,
        )

        self.assertEqual(extracted["order_draft"]["quoted_total"], "790")
        self.assertEqual(extracted["order_draft"]["items"][0]["unit_price"], "790.00")

    def test_multi_line_manager_total_remains_visible_without_unsafe_allocation(self):
        from management.services.ig_payment_review import (
            _apply_catalog_matches_to_draft,
            _apply_validated_conversation_price_to_draft,
            extract_payment_review_evidence,
        )

        messages = [
            {"id": 1, "role": "user", "text": "Мені потрібно 2 футболки: 1. Базова S 2. Оверсайз XS"},
            {"id": 2, "role": "manager", "text": "Сума: 2100 грн"},
            {"id": 3, "role": "user", "text": "По повній передоплаті"},
        ]
        extracted = extract_payment_review_evidence(messages)
        matches = [{"status": "matched", "product_id": 111, "title": "Футболка Харків"}]
        _apply_catalog_matches_to_draft(extracted["order_draft"], matches)
        _apply_validated_conversation_price_to_draft(extracted["order_draft"], messages, matches)

        self.assertEqual(extracted["order_draft"]["quoted_total"], "2100")
        self.assertTrue(all(item["unit_price"] is None for item in extracted["order_draft"]["items"]))
        self.assertIn("conversation_price_allocation_required", extracted["order_draft"]["uncertainty_reasons"])


class PaymentReviewDealBindingTests(SimpleTestCase):
    def _deal(self, **overrides):
        values = {
            "status": "awaiting_payment",
            "order_id": None,
            "payment_truth": "unverified",
            "payment_status": "unpaid",
            "product_ids": {111},
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_terminal_or_product_conflicting_deal_is_not_compatible(self):
        from management.services.ig_payment_review import _is_review_deal_compatible

        self.assertFalse(_is_review_deal_compatible(self._deal(status="order_created"), {111}))
        self.assertFalse(_is_review_deal_compatible(self._deal(product_ids={222}), {111}))
        self.assertFalse(_is_review_deal_compatible(self._deal(payment_truth="confirmed"), {111}))

    def test_current_unpaid_same_product_deal_is_compatible(self):
        from management.services.ig_payment_review import _is_review_deal_compatible

        self.assertTrue(_is_review_deal_compatible(self._deal(), {111}))


class TelegramPaymentReviewGateTests(SimpleTestCase):
    def _notification(self, **overrides):
        values = {
            "event_type": "payment_review",
            "status": "sent",
            "telegram_message_id": "88",
            "payload": {"chat_id": "-100", "media": [{"delivery_status": "sent"}]},
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_exact_sent_review_notification_is_actionable(self):
        from management.views import _payment_review_notification_gate

        self.assertEqual(_payment_review_notification_gate(self._notification(), "-100", 88), "")

    def test_wrong_message_and_failed_media_block_decision(self):
        from management.views import _payment_review_notification_gate

        self.assertEqual(
            _payment_review_notification_gate(self._notification(), "-100", 89),
            "Ця кнопка не належить цьому review",
        )
        self.assertEqual(
            _payment_review_notification_gate(
                self._notification(status="failed", payload={
                    "chat_id": "-100",
                    "main_delivery_message_id": "88",
                    "media": [{"delivery_status": "failed"}],
                }),
                "-100",
                88,
            ),
            "Докази ще не доставлені — відкрийте перевірку",
        )

    def test_main_alert_waits_until_receipt_media_is_delivered(self):
        from management.views import _payment_review_notification_gate

        notification = self._notification(
            status="sending",
            telegram_message_id="",
            payload={
                "chat_id": "-100",
                "main_delivery_message_id": "88",
                "media": [{"delivery_status": "sending"}],
            },
        )
        self.assertEqual(
            _payment_review_notification_gate(notification, "-100", 88),
            "Докази ще не доставлені — відкрийте перевірку",
        )

    @patch("management.services.ig_payment_review.transaction.atomic", return_value=nullcontext())
    def test_losing_opposite_transition_cannot_overwrite_winner_audit(self, _atomic):
        from management.ig_bot_models import IgPaymentConfirmationReview
        from management.services.ig_payment_review import cancel_review, confirm_review

        locked = SimpleNamespace(
            pk=42,
            status=IgPaymentConfirmationReview.Status.PENDING,
            evidence={},
            save=Mock(),
        )
        query = Mock()
        query.get.return_value = locked
        with patch.object(IgPaymentConfirmationReview.objects, "select_for_update", return_value=query):
            winner = confirm_review(
                SimpleNamespace(pk=42),
                actor=None,
                telegram_decision={"action": "confirm", "telegram_user_id": "7"},
            )
            winner_applied = winner._transitioned
            loser = cancel_review(
                SimpleNamespace(pk=42),
                actor=None,
                telegram_decision={"action": "cancel", "telegram_user_id": "8"},
            )

        self.assertTrue(winner_applied)
        self.assertFalse(loser._transitioned)
        self.assertEqual(locked.status, IgPaymentConfirmationReview.Status.CONFIRMED)
        self.assertEqual(locked.evidence["telegram_decision"]["action"], "confirm")


class CatalogHydrationPriceTests(TestCase):
    def test_single_variant_catalog_match_uses_variant_price(self):
        from management.services.ig_payment_review import _hydrate_catalog_match
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductStatus

        category = Category.objects.create(name="Футболки", slug="review-priced")
        product = Product.objects.create(
            title="Бойова квіточка",
            slug="review-priced-flower",
            category=category,
            price=1090,
            status=ProductStatus.PUBLISHED,
        )
        color = Color.objects.create(name="Термо-зелена", primary_hex="#A2AB92")
        variant = ProductColorVariant.objects.create(
            product=product,
            color=color,
            price_override=1450,
            is_default=True,
        )

        hydrated = _hydrate_catalog_match(
            {"product_id": product.pk, "confidence": 0.95, "reason": "збіг"},
            [],
            [],
        )

        self.assertEqual(hydrated["color_variant_id"], variant.pk)
        self.assertEqual(hydrated["catalog_price"], "1450")
        self.assertNotEqual(hydrated["catalog_price"], "1090")


class CatalogAssignmentTests(SimpleTestCase):
    def test_pending_review_refreshes_when_material_draft_changes(self):
        from management.services.ig_payment_review import _review_evidence_needs_refresh

        current = {
            "media_audit_v3": True,
            "order_draft": {"items": [{"product_id": 111}, {"product_id": None}], "quoted_total": ""},
            "media": [{"role": "product"}],
            "catalog_matches": [{"product_id": 111}],
        }
        extracted = {
            "media_audit_v3": True,
            "order_draft": {"items": [{"product_id": 111}, {"product_id": 111}], "quoted_total": "2100"},
            "media": [{"role": "product"}],
            "catalog_matches": [{"product_id": 111}],
        }

        self.assertTrue(_review_evidence_needs_refresh("pending", current, extracted))
        self.assertFalse(_review_evidence_needs_refresh("confirmed", current, extracted))
        self.assertFalse(_review_evidence_needs_refresh("pending", extracted, extracted))

    def test_persist_review_media_keeps_duplicate_owned_media_private(self):
        from management.services.ig_payment_review import _persist_review_media

        media = [
            {
                "url": "https://lookaside.example/signed-a.jpg",
                "provenance": "live_webhook",
                "status": "owned",
                "private_storage": True,
                "storage_name": "ig_message_media/provider-post.jpg",
                "local_url": "/media/ig_payment_reviews/legacy-copy.jpg",
                "ig_post_media_id": "post-123",
                "role": "product",
            },
            {
                "url": "https://lookaside.example/signed-b.jpg",
                "provenance": "live_webhook",
                "status": "owned",
                "private_storage": True,
                "storage_name": "ig_message_media/provider-post.jpg",
                "ig_post_media_id": "post-123",
                "role": "product",
            },
        ]

        persisted = _persist_review_media(media)

        self.assertNotIn("local_url", persisted[0])
        self.assertEqual(persisted[0]["storage_name"], "ig_message_media/provider-post.jpg")
        self.assertEqual(persisted[1]["storage_name"], "ig_message_media/provider-post.jpg")

    @patch("management.services.bot_vision.match_many", return_value=[{
        "product_id": 11,
        "confidence": 0.93,
        "source_image_indexes": [0],
        "reason": "локальне зображення збігається",
    }])
    @patch(
        "management.services.instagram_bot.download_image",
        side_effect=lambda url, **_kwargs: ("image/jpeg", b"local") if "/media/" in url else None,
    )
    def test_catalog_matching_prefers_persisted_local_media(self, _download, _match_many):
        from management.services.ig_payment_review import _catalog_matches_for_media

        with patch(
            "management.services.ig_payment_review._hydrate_catalog_match",
            return_value={"status": "matched", "product_id": 11, "confidence": 0.93},
        ):
            matches = _catalog_matches_for_media([{
                "url": "https://lookaside.example/expired-signed-url.jpg",
                "local_url": "/media/ig_payment_reviews/product.jpg",
                "role": "product",
                "intent": "purchase_candidate",
                "actionable": True,
                "catalog_match_allowed": True,
            }])

        self.assertEqual(matches[0]["product_id"], 11)
        self.assertIn("/media/", _download.call_args.args[0])

    def test_order_binding_ignores_old_product_question_media(self):
        from management.services.ig_payment_review import _catalog_order_media

        question = {
            "url": "https://cdn.example/question.jpg",
            "role": "product",
            "intent": "question",
            "actionable": False,
            "catalog_match_allowed": True,
        }
        purchase = {
            "url": "https://cdn.example/purchase.jpg",
            "role": "product",
            "intent": "purchase_candidate",
            "actionable": True,
            "catalog_match_allowed": True,
        }
        self.assertEqual(_catalog_order_media([question, purchase]), [purchase])

    @patch("management.services.ig_payment_review._hydrate_catalog_match")
    @patch("management.services.bot_vision.match_many", return_value=[{
        "product_id": 11,
        "confidence": 0.93,
        "source_image_indexes": [0],
        "reason": "дубль одного зображення",
    }])
    @patch(
        "management.services.instagram_bot.download_image",
        side_effect=lambda url, **_kwargs: ("image/jpeg", b"same-product") if "/media/" in url else None,
    )
    def test_catalog_matching_deduplicates_identical_media_but_keeps_source_indexes(
        self, _download, _match_many, hydrate
    ):
        from management.services.ig_payment_review import _catalog_matches_for_media

        hydrate.return_value = {"status": "matched", "product_id": 11, "confidence": 0.93}
        media = [
            {
                "url": "https://lookaside.example/expired-a.jpg",
                "local_url": "/media/ig_payment_reviews/a.jpg",
                "role": "product",
                "intent": "purchase_candidate",
                "actionable": True,
                "catalog_match_allowed": True,
            },
            {
                "url": "https://lookaside.example/expired-b.jpg",
                "local_url": "/media/ig_payment_reviews/b.jpg",
                "role": "product",
                "intent": "purchase_candidate",
                "actionable": True,
                "catalog_match_allowed": True,
            },
        ]

        matches = _catalog_matches_for_media(media)

        self.assertEqual(matches[0]["product_id"], 11)
        self.assertEqual(len(_match_many.call_args.args[0]), 1)
        self.assertEqual(hydrate.call_args.args[2], [0, 1])

    @patch("management.services.ig_payment_review._hydrate_catalog_match")
    @patch("management.services.bot_vision.match_many", return_value=[{
        "product_id": 11,
        "confidence": 0.93,
        "source_image_indexes": [0, 1],
        "reason": "два різні джерела одного замовлення",
    }])
    @patch(
        "management.services.instagram_bot.download_image",
        side_effect=lambda url, **_kwargs: (
            "image/jpeg", b"product-a" if url.endswith("/a.jpg") else b"product-b"
        ),
    )
    def test_catalog_matching_keeps_distinct_media_independent(self, _download, _match_many, hydrate):
        from management.services.ig_payment_review import _catalog_matches_for_media

        hydrate.return_value = {"status": "matched", "product_id": 11, "confidence": 0.93}
        media = [
            {
                "url": "https://lookaside.example/a.jpg",
                "local_url": "/media/ig_payment_reviews/a.jpg",
                "role": "product",
                "intent": "purchase_candidate",
                "actionable": True,
                "catalog_match_allowed": True,
            },
            {
                "url": "https://lookaside.example/b.jpg",
                "local_url": "/media/ig_payment_reviews/b.jpg",
                "role": "product",
                "intent": "purchase_candidate",
                "actionable": True,
                "catalog_match_allowed": True,
            },
        ]

        _catalog_matches_for_media(media)

        self.assertEqual(len(_match_many.call_args.args[0]), 2)
        self.assertEqual(hydrate.call_args.args[2], [0, 1])

    @patch("management.services.ig_payment_review._hydrate_catalog_match")
    @patch("management.services.bot_vision.match_many", return_value=[{
        "product_id": 11,
        "confidence": 0.93,
        "source_image_indexes": [0],
        "reason": "durable hash reused",
    }])
    @patch(
        "management.services.instagram_bot.download_image",
        return_value=("image/jpeg", b"same-product"),
    )
    def test_catalog_matching_skips_second_local_download_for_known_hash(
        self, download, _match_many, hydrate
    ):
        from management.services.ig_payment_review import _catalog_matches_for_media

        hydrate.return_value = {"status": "matched", "product_id": 11, "confidence": 0.93}
        media = [
            {
                "url": "https://lookaside.example/a.jpg",
                "local_url": "/media/ig_payment_reviews/a.jpg",
                "content_hash": "6966aafb2ab4821d23624e6f910a007c27ccd55ee9b18bcea14d078c1fdeace4",
                "role": "product",
                "intent": "purchase_candidate",
                "actionable": True,
                "catalog_match_allowed": True,
            },
            {
                "url": "https://lookaside.example/b.jpg",
                "local_url": "/media/ig_payment_reviews/b.jpg",
                "content_hash": "6966aafb2ab4821d23624e6f910a007c27ccd55ee9b18bcea14d078c1fdeace4",
                "role": "product",
                "intent": "purchase_candidate",
                "actionable": True,
                "catalog_match_allowed": True,
            },
        ]

        matches = _catalog_matches_for_media(media)

        self.assertEqual(matches[0]["product_id"], 11)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(hydrate.call_args.args[2], [0, 1])

    def test_two_catalog_matches_are_bound_to_two_draft_lines(self):
        from management.services.ig_payment_review import _apply_catalog_matches_to_draft

        draft = {
            "items": [
                {"title": "Футболка 1", "fit": "classic", "source_message_id": 101},
                {"title": "Футболка 2", "fit": "oversize", "source_message_id": 102},
            ],
            "uncertainty_reasons": ["catalog_product_not_identified"],
        }
        matches = [
            {"status": "matched", "product_id": 11, "title": "Харків", "url": "https://twocomms.shop/p/kharkiv/", "source_message_ids": [101]},
            {"status": "matched", "product_id": 22, "title": "Київ", "url": "https://twocomms.shop/p/kyiv/", "source_message_ids": [102]},
        ]
        _apply_catalog_matches_to_draft(draft, matches)
        self.assertEqual([item["product_id"] for item in draft["items"]], [11, 22])
        self.assertEqual(draft["items"][0]["catalog"]["url"], "https://twocomms.shop/p/kharkiv/")
        self.assertNotIn("catalog_product_not_identified", draft["uncertainty_reasons"])

    def test_one_catalog_product_can_bind_classic_and_oversize_lines_from_same_message(self):
        from management.services.ig_payment_review import _apply_catalog_matches_to_draft

        draft = {
            "items": [
                {"title": "Базова футболка", "fit": "classic", "size": "S", "source_message_id": 233},
                {"title": "Оверсайз", "fit": "oversize", "size": "XS", "source_message_id": 233},
            ],
            "uncertainty_reasons": ["catalog_product_not_identified"],
        }
        matches = [{
            "status": "matched",
            "product_id": 111,
            "title": "Футболка «Харків Вокзальна»",
            "url": "https://twocomms.shop/product/futbolka-kharkiv-vokzalna/",
            "source_message_ids": [233],
        }]

        _apply_catalog_matches_to_draft(draft, matches)

        self.assertEqual([item["product_id"] for item in draft["items"]], [111, 111])
        self.assertNotIn("catalog_product_not_identified", draft["uncertainty_reasons"])

    def test_two_matches_from_one_purchase_screenshot_create_two_draft_lines(self):
        from management.services.ig_payment_review import _apply_catalog_matches_to_draft

        draft = {"items": [], "uncertainty_reasons": ["catalog_product_not_identified"]}
        matches = [
            {"status": "matched", "product_id": 11, "title": "Харків", "url": "https://twocomms.shop/p/kharkiv/"},
            {"status": "matched", "product_id": 22, "title": "Київ", "url": "https://twocomms.shop/p/kyiv/"},
        ]
        _apply_catalog_matches_to_draft(draft, matches)
        self.assertEqual(len(draft["items"]), 2)
        self.assertEqual([item["product_id"] for item in draft["items"]], [11, 22])
        self.assertEqual([item["qty"] for item in draft["items"]], [1, 1])
        self.assertNotIn("catalog_product_not_identified", draft["uncertainty_reasons"])
