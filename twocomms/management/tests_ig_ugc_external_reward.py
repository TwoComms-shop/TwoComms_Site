import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.urls import reverse
from PIL import Image

from management.models import IgClient, InstagramBotMessage
from storefront.models import PromoCode


@override_settings(
    IG_UGC_AUTO_AWARD_MODE="auto",
    ROOT_URLCONF="twocomms.urls_management",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
)
class ExternalUGCRewardTests(TransactionTestCase):
    def _owned_image(self, storage_name, *, pattern_key=""):
        buffer = BytesIO()
        digest = hashlib.sha256(str(pattern_key or storage_name).encode("utf-8")).digest()
        image = Image.new("RGB", (16, 16))
        for y in range(16):
            for x in range(16):
                offset = (x * 5 + y * 7) % len(digest)
                image.putpixel(
                    (x, y),
                    (
                        digest[offset],
                        digest[(offset + 9) % len(digest)],
                        digest[(offset + 17) % len(digest)],
                    ),
                )
        image.save(buffer, format="JPEG")
        raw = buffer.getvalue()
        if default_storage.exists(storage_name):
            default_storage.delete(storage_name)
        saved_name = default_storage.save(storage_name, ContentFile(raw))
        self.addCleanup(default_storage.delete, saved_name)
        return {
            "storage_name": saved_name,
            "mime": "image/jpeg",
            "bytes": len(raw),
            "content_hash": hashlib.sha256(raw).hexdigest(),
        }

    def _create_assessment(
        self,
        suffix,
        *,
        client=None,
        confidence=Decimal("0.99"),
        generation=1,
    ):
        from management.services.ig_ugc_assessment import assess_ugc_evidence

        client = client or self.client
        mid = f"story-mid-{suffix}"
        provider_key = f"story:{suffix}-object"
        owned = self._owned_image(f"ig/owned/{suffix}.jpg", pattern_key=suffix)
        message = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            text="Відмітила TwoComms",
            source="webhook",
            mid=mid,
            media_capture_eligible=True,
            attachment_media=[{
                "url": f"https://lookaside.fbsbx.com/media/{suffix}.jpg",
                "provider_object_key": provider_key,
                "provider_media_id": f"media:{suffix}",
                "provider_event_id": mid,
                "media_type": "story_mention",
                "target_username": "twocomms",
                "provider_native_mention": True,
                "provenance": "live_webhook",
                "status": "owned",
                **owned,
            }],
        )
        assessment = assess_ugc_evidence(
            message=message,
            facts={
                "provider_native_mention": True,
                "target_username": "twocomms",
                "owned_media": True,
                "personal_worn_apparel": True,
                "customer_created_content": True,
                "customer_content_confidence": Decimal("0.99"),
                "brand_match_confidence": confidence,
                "catalog_matches": [{"product_id": 42, "confidence": confidence}],
                "risk_flags": [],
                "people_count": 2,
                "garment_count": 1,
            },
        )
        if generation != assessment.generation:
            assessment.generation = generation
            assessment.save(update_fields=["generation", "updated_at"])
        return assessment

    def setUp(self):
        self.client = IgClient.get_or_create_for_sender("external-ugc-client")
        self.client.last_message_at = timezone.now()
        self.client.save(update_fields=["last_message_at", "updated_at"])
        self.actor = get_user_model().objects.create_user(
            username="ugc-manager",
            password="test-password",
            is_staff=True,
        )
        # B02.4 requires explicit capabilities; staff alone grants no bot access.
        from django.contrib.auth.models import Permission

        self.actor.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="management",
            codename__in={"operate_ig_bot", "view_ig_conversation_pii", "manage_ig_payments"},
        ))
        from management.models import InstagramBotSettings

        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        self.assessment = self._create_assessment("external")

    def test_reward_legacy_boundaries_are_orm_only(self):
        """Mixed production engines must not receive cross-table FK DDL."""
        from management.ig_bot_models import IgUgcReward

        for field_name in ("client", "order", "assignment", "evidence_message", "promo_code", "reviewed_by"):
            with self.subTest(field=field_name):
                self.assertFalse(IgUgcReward._meta.get_field(field_name).db_constraint)

    def test_reward_path_and_decision_source_constraints_fail_closed(self):
        from management.ig_bot_models import IgUgcReward

        for overrides in (
            {
                "reward_path": "delivered_order",
                "decision_source": "manager",
                "reviewed_by": None,
            },
            {
                "reward_path": "external_ugc",
                "decision_source": "auto",
                "reviewed_by": self.actor,
            },
        ):
            with self.subTest(overrides=overrides), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    IgUgcReward.objects.create(
                        client=self.client,
                        order=None,
                        assignment=None,
                        assignment_version=0,
                        evidence_type=IgUgcReward.EvidenceType.STORY_MENTION,
                        evidence_fingerprint=f"invalid-{overrides['reward_path']}-{overrides['decision_source']}",
                        review_note="",
                        promo_code=PromoCode.objects.create(
                            code=f"BAD{overrides['reward_path'][:3].upper()}{overrides['decision_source'][:3].upper()}",
                            discount_type="percentage",
                            discount_value=Decimal("10.00"),
                            max_uses=1,
                        ),
                        **overrides,
                    )

    def test_qualified_auto_cannot_mint_without_authenticated_actor(self):
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
        )

        with self.assertRaisesMessage(
            UgcRewardConflict,
            "Для підтвердження UGC потрібен авторизований менеджер.",
        ):
            award_external_ugc_reward(
                client=self.client,
                assessment=self.assessment,
            )

    def test_reward_freezes_assessment_evidence_snapshot_at_issuance(self):
        from management.services.ig_ugc_rewards import award_external_ugc_reward

        expected_generation = self.assessment.generation
        expected_policy = self.assessment.policy_version
        expected_provider_digest = self.assessment.provider_object_digest
        expected_catalog = list(self.assessment.catalog_candidates)

        reward, created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        self.assertTrue(created)

        self.assessment.generation += 1
        self.assessment.policy_version = "future-policy"
        self.assessment.provider_object_digest = hashlib.sha256(b"changed").hexdigest()
        self.assessment.catalog_candidates = [{"product_id": 999, "confidence": 1.0}]
        self.assessment.save(update_fields=[
            "generation",
            "policy_version",
            "provider_object_digest",
            "catalog_candidates",
            "updated_at",
        ])
        reward.refresh_from_db()

        self.assertEqual(reward.assessment_generation_snapshot, expected_generation)
        self.assertEqual(reward.policy_version_snapshot, expected_policy)
        self.assertEqual(reward.provider_object_digest_snapshot, expected_provider_digest)
        self.assertEqual(reward.catalog_candidates_snapshot, expected_catalog)

    def test_qualified_assessment_without_provider_gates_cannot_mint_code(self):
        from management.services.ig_ugc_rewards import UgcRewardConflict, award_external_ugc_reward

        self.assessment.target_username = ""
        self.assessment.provider_object_key = ""
        self.assessment.save(update_fields=["target_username", "provider_object_key"])

        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(client=self.client, assessment=self.assessment, actor=self.actor, review_note="Test manager approval")

        from management.ig_bot_models import IgUgcReward
        self.assertFalse(IgUgcReward.objects.exists())

    def test_reward_rejects_stale_assessment_policy(self):
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
        )

        self.assessment.policy_version = "ugc-v0"
        self.assessment.save(update_fields=["policy_version", "updated_at"])

        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(
                client=self.client,
                assessment=self.assessment,
                actor=self.actor,
                review_note="Test manager approval",
            )

    def test_reward_reloads_original_user_webhook_message(self):
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
        )

        source = InstagramBotMessage.objects.get(mid=self.assessment.source_message_id)
        source.source = "poll"
        source.save(update_fields=["source"])

        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(
                client=self.client,
                assessment=self.assessment,
                actor=self.actor,
                review_note="Test manager approval",
            )

    def test_reward_reloads_original_owned_attachment(self):
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
        )

        source = InstagramBotMessage.objects.get(mid=self.assessment.source_message_id)
        source.attachment_media = []
        source.save(update_fields=["attachment_media"])

        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(
                client=self.client,
                assessment=self.assessment,
                actor=self.actor,
                review_note="Test manager approval",
            )

    def test_reward_and_delivery_outbox_commit_together(self):
        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import award_external_ugc_reward

        reward, created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )

        self.assertTrue(created)
        self.assertTrue(IgUgcRewardDelivery.objects.filter(reward=reward).exists())

    def test_delivery_outbox_failure_rolls_back_reward_promo_and_lifetime(self):
        from unittest.mock import patch

        from management.ig_bot_models import (
            IgUgcReward,
            IgUgcRewardDelivery,
            IgUgcRewardLifetime,
        )
        from management.services.ig_ugc_rewards import award_external_ugc_reward

        with patch.object(
            IgUgcRewardDelivery.objects,
            "create",
            side_effect=RuntimeError("forced outbox failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced outbox failure"):
            award_external_ugc_reward(
                client=self.client,
                assessment=self.assessment,
                actor=self.actor,
                review_note="Test manager approval",
            )

        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(IgUgcRewardLifetime.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_second_assessment_cannot_reissue_lifetime_reward(self):
        from management.ig_bot_models import IgUgcReward
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
        )

        first, created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        self.assertTrue(created)
        second_assessment = self._create_assessment("external-2")
        self.assertEqual(second_assessment.decision, "rejected")
        self.assertIn("already_rewarded", second_assessment.reason_codes)

        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(
                client=self.client,
                assessment=second_assessment,
            )

        self.assertEqual(IgUgcReward.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertEqual(IgUgcReward.objects.get().pk, first.pk)

    def test_idempotent_replay_reuses_reward_promo_and_delivery_snapshot(self):
        from management.ig_bot_models import IgUgcReward, IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            queue_external_ugc_reward_delivery,
        )
        from storefront.models import PromoCode

        first, first_created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        first_delivery = queue_external_ugc_reward_delivery(first)

        replay, replay_created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        replay_delivery = queue_external_ugc_reward_delivery(replay)

        self.assertTrue(first_created)
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(replay.promo_code_id, first.promo_code_id)
        self.assertEqual(replay_delivery.pk, first_delivery.pk)
        self.assertEqual(
            replay_delivery.message_snapshot,
            first_delivery.message_snapshot,
        )
        self.assertEqual(IgUgcReward.objects.count(), 1)
        self.assertEqual(IgUgcRewardDelivery.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)

    def test_legacy_reward_without_slot_is_bound_before_replay(self):
        from management.ig_bot_models import IgUgcRewardLifetime
        from management.services.ig_ugc_rewards import award_external_ugc_reward

        first, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        IgUgcRewardLifetime.objects.all().delete()

        replay, created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )

        self.assertFalse(created)
        self.assertEqual(replay.pk, first.pk)
        lifetime = IgUgcRewardLifetime.objects.get()
        self.assertEqual(lifetime.reward_id, first.pk)
        self.assertEqual(lifetime.client_id, self.client.pk)
        self.assertIsNotNone(lifetime.consumed_at)

    def test_delivered_order_and_external_paths_share_one_lifetime_slot(self):
        from management.services.ig_ugc_rewards import award_external_ugc_reward, award_ugc_reward
        from management.models import InstagramBotMessage
        from management.services.ig_order_assignments import link_order_to_client
        from orders.models import Order

        manager = self.actor
        order = Order.objects.create(
            order_number="TWC-CROSS-PATH",
            full_name="Cross path buyer",
            phone="380501112233",
            city="Kyiv",
            np_office="Branch 1",
            total_sum=Decimal("1000.00"),
            payment_status="paid",
            status="done",
            tracking_number="20450000000002",
            tracking_status_code=9,
            tracking_terminal_at=timezone.now(),
        )
        link_order_to_client(order, client=self.client, actor=manager)
        evidence = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text="Відмітила TwoComms",
            provider_created_at=timezone.now(),
        )

        delivered, delivered_created = award_ugc_reward(
            client=self.client,
            order=order,
            actor=manager,
            evidence_message_id=evidence.pk,
            review_note="Перевірено фото та відповідність замовленню",
        )
        external, external_created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )

        self.assertTrue(delivered_created)
        self.assertFalse(external_created)
        self.assertEqual(external.pk, delivered.pk)

    def test_external_path_blocks_later_delivered_order_path(self):
        from management.services.ig_ugc_rewards import award_external_ugc_reward, award_ugc_reward
        from management.models import InstagramBotMessage
        from management.services.ig_order_assignments import link_order_to_client
        from orders.models import Order

        external, external_created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        order = Order.objects.create(
            order_number="TWC-CROSS-PATH-2",
            full_name="Cross path buyer",
            phone="380501112244",
            city="Kyiv",
            np_office="Branch 2",
            total_sum=Decimal("1000.00"),
            payment_status="paid",
            status="done",
            tracking_number="20450000000003",
            tracking_status_code=9,
            tracking_terminal_at=timezone.now(),
        )
        link_order_to_client(order, client=self.client, actor=self.actor)
        evidence = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text="Відмітила TwoComms",
            provider_created_at=timezone.now(),
        )

        delivered, delivered_created = award_ugc_reward(
            client=self.client,
            order=order,
            actor=self.actor,
            evidence_message_id=evidence.pk,
            review_note="Перевірено фото та відповідність замовленню",
        )

        self.assertTrue(external_created)
        self.assertFalse(delivered_created)
        self.assertEqual(delivered.pk, external.pk)

    def test_manager_approval_requires_authenticated_actor(self):
        from management.services.ig_ugc_rewards import award_external_ugc_reward, UgcRewardConflict

        review_assessment = self._create_assessment(
            "review",
            confidence=Decimal("0.80"),
        )
        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(
                client=self.client,
                assessment=review_assessment,
            )
        reward, created = award_external_ugc_reward(
            client=self.client,
            assessment=review_assessment,
            actor=self.actor,
            review_note="  Звірено provider provenance та фото  ",
        )
        self.assertTrue(created)
        self.assertEqual(reward.reviewed_by_id, self.actor.pk)
        self.assertEqual(reward.decision_source, "manager")
        self.assertEqual(reward.review_note, "Звірено provider provenance та фото")

    @patch.dict(
        "os.environ",
        {
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "777",
            "MANAGEMENT_TG_BOT_TOKEN": "ugc-token",
        },
        clear=False,
    )
    @patch("management.views._tg_edit_message")
    @patch("management.views._tg_answer_callback")
    def test_telegram_five_percent_requires_manager_click_before_reward(
        self,
        answer_callback,
        edit_message,
    ):
        from accounts.models import UserProfile
        from management.ig_bot_models import IgUgcEvidenceAssessment, IgUgcReward
        from management.models import IgBotNotification
        from management.services.ig_ugc_assessment import queue_ugc_manager_review
        from management.views import management_bot_webhook

        profile = UserProfile.objects.get(user=self.actor)
        profile.tg_manager_chat_id = 777
        profile.is_manager = True
        profile.save(update_fields=["tg_manager_chat_id", "is_manager"])
        assessment = self._create_assessment("telegram-five")
        assessment.decision = IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW
        assessment.decision_source = "policy"
        assessment.generation += 1
        assessment.save(update_fields=[
            "decision", "decision_source", "generation", "updated_at",
        ])
        self.assertTrue(queue_ugc_manager_review(assessment))
        self.assertFalse(IgUgcReward.objects.filter(assessment=assessment).exists())
        notification = IgBotNotification.objects.get(
            dedupe_key=f"ugc_review:{assessment.pk}:{assessment.generation}"
        )
        notification.status = IgBotNotification.Status.SENT
        notification.telegram_message_id = "901"
        notification.payload = {
            **notification.payload,
            "main_delivery_message_id": "901",
        }
        notification.save(update_fields=[
            "status", "telegram_message_id", "payload", "updated_at",
        ])
        request = RequestFactory().post(
            "/management/tg-manager/webhook/ugc-token/",
            data=json.dumps({
                "callback_query": {
                    "id": "ugc-callback-5",
                    "data": f"igugc:5:{assessment.pk}:{assessment.generation}",
                    "from": {"id": 777, "username": "ugc-manager"},
                    "message": {
                        "chat": {"id": 777, "type": "private"},
                        "message_id": 901,
                        "text": "UGC review",
                    },
                },
            }),
            content_type="application/json",
        )

        response = management_bot_webhook(request, "ugc-token")

        self.assertEqual(response.status_code, 200)
        assessment.refresh_from_db()
        notification.refresh_from_db()
        reward = IgUgcReward.objects.get(assessment=assessment)
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.MANAGER_APPROVED,
        )
        self.assertEqual(reward.discount_percent, 5)
        self.assertEqual(reward.promo_code.discount_value, Decimal("5.00"))
        self.assertTrue(reward.promo_code.is_guest_ugc_capability())
        self.assertIn("5%", reward.delivery.message_snapshot)
        self.assertNotIn("10%", reward.delivery.message_snapshot)
        self.assertEqual(notification.status, IgBotNotification.Status.RESOLVED)
        answer_callback.assert_called_once()
        edit_message.assert_called_once()

    @patch.dict(
        "os.environ",
        {
            "MANAGEMENT_TG_ADMIN_CHAT_ID": "777,888",
            "MANAGEMENT_TG_BOT_TOKEN": "ugc-token",
        },
        clear=False,
    )
    @patch("management.views._tg_edit_message")
    @patch("management.views._tg_answer_callback")
    def test_ugc_callback_rejects_same_message_id_from_another_admin_chat(
        self,
        answer_callback,
        edit_message,
    ):
        from accounts.models import UserProfile
        from management.ig_bot_models import IgUgcEvidenceAssessment, IgUgcReward
        from management.models import IgBotNotification
        from management.services.ig_ugc_assessment import queue_ugc_manager_review
        from management.views import management_bot_webhook

        profile = UserProfile.objects.get(user=self.actor)
        profile.tg_manager_chat_id = 777
        profile.is_manager = True
        profile.save(update_fields=["tg_manager_chat_id", "is_manager"])
        assessment = self._create_assessment("telegram-cross-chat")
        assessment.decision = IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW
        assessment.decision_source = "policy"
        assessment.generation += 1
        assessment.save(update_fields=[
            "decision", "decision_source", "generation", "updated_at",
        ])
        self.assertTrue(queue_ugc_manager_review(assessment))
        notification = IgBotNotification.objects.get(
            dedupe_key=f"ugc_review:{assessment.pk}:{assessment.generation}"
        )
        notification.status = IgBotNotification.Status.SENT
        notification.telegram_message_id = "901"
        notification.payload = {
            **notification.payload,
            "chat_id": "777",
            "main_delivery_message_id": "901",
        }
        notification.save(update_fields=[
            "status", "telegram_message_id", "payload", "updated_at",
        ])
        request = RequestFactory().post(
            "/management/tg-manager/webhook/ugc-token/",
            data=json.dumps({
                "callback_query": {
                    "id": "ugc-cross-chat",
                    "data": f"igugc:10:{assessment.pk}:{assessment.generation}",
                    "from": {"id": 777},
                    "message": {
                        "chat": {"id": 888, "type": "group"},
                        "message_id": 901,
                        "text": "UGC review",
                    },
                },
            }),
            content_type="application/json",
        )

        response = management_bot_webhook(request, "ugc-token")

        self.assertEqual(response.status_code, 200)
        assessment.refresh_from_db()
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW,
        )
        self.assertFalse(IgUgcReward.objects.filter(assessment=assessment).exists())
        edit_message.assert_not_called()
        self.assertIn(
            "не належить",
            str(answer_callback.call_args.args[2]),
        )

    def test_direct_manager_approval_requires_a_non_blank_reason(self):
        from management.ig_bot_models import (
            IgUgcReward,
            IgUgcRewardDelivery,
            IgUgcRewardLifetime,
        )
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
        )

        review_assessment = self._create_assessment(
            "direct-review-reason",
            confidence=Decimal("0.80"),
        )

        with self.assertRaisesMessage(
            UgcRewardConflict,
            "Додайте причину підтвердження UGC.",
        ):
            award_external_ugc_reward(
                client=self.client,
                assessment=review_assessment,
                actor=self.actor,
                review_note=" \t\r\n\u00a0 ",
            )

        self.assertFalse(IgUgcReward.objects.filter(assessment=review_assessment).exists())
        self.assertFalse(
            IgUgcRewardDelivery.objects.filter(reward__assessment=review_assessment).exists()
        )
        self.assertFalse(IgUgcRewardLifetime.objects.filter(client=self.client).exists())

    def test_manager_review_api_requires_an_authenticated_manager(self):
        from django.test import Client

        from management.ig_bot_models import IgUgcEvidenceAssessment, IgUgcReward

        assessment = self._create_assessment(
            "api-auth",
            confidence=Decimal("0.88"),
        )
        url = reverse(
            "management_bot_client_ugc_assessment_review_api",
            args=[self.client.pk, assessment.pk],
        )
        payload = {
            "decision": "approve",
            "generation": str(assessment.generation),
            "note": "Звірено фото та provider provenance",
        }

        anonymous = Client()
        anonymous_response = anonymous.post(url, payload)
        self.assertEqual(anonymous_response.status_code, 302)

        ordinary_user = get_user_model().objects.create_user(
            username="ugc-ordinary-user",
            password="test-password",
        )
        authenticated_non_manager = Client()
        authenticated_non_manager.force_login(ordinary_user)
        forbidden_response = authenticated_non_manager.post(url, payload)
        self.assertEqual(forbidden_response.status_code, 403)
        self.assertFalse(forbidden_response.json()["success"])

        assessment.refresh_from_db()
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW,
        )
        self.assertIsNone(assessment.reviewed_by_id)
        self.assertIsNone(assessment.reviewed_at)
        self.assertFalse(IgUgcReward.objects.filter(assessment=assessment).exists())

    def test_manager_review_api_rejects_missing_blank_or_whitespace_approval_reason(self):
        from django.test import Client

        from management.ig_bot_models import (
            IgUgcEvidenceAssessment,
            IgUgcReward,
            IgUgcRewardDelivery,
            IgUgcRewardLifetime,
        )

        http = Client()
        http.force_login(self.actor)

        for label, note_payload in (
            ("missing", {}),
            ("blank", {"note": ""}),
            ("whitespace", {"note": " \t\r\n\u00a0 "}),
        ):
            with self.subTest(reason=label):
                case_client = IgClient.get_or_create_for_sender(f"ugc-reason-{label}")
                assessment = self._create_assessment(
                    f"api-reason-{label}",
                    client=case_client,
                    confidence=Decimal("0.88"),
                )
                url = reverse(
                    "management_bot_client_ugc_assessment_review_api",
                    args=[case_client.pk, assessment.pk],
                )
                response = http.post(
                    url,
                    {
                        "decision": "approve",
                        "generation": str(assessment.generation),
                        **note_payload,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["success"])
                self.assertEqual(
                    response.json()["error"],
                    "Додайте причину підтвердження UGC.",
                )

                assessment.refresh_from_db()
                self.assertEqual(
                    assessment.decision,
                    IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW,
                )
                self.assertIsNone(assessment.reviewed_by_id)
                self.assertIsNone(assessment.reviewed_at)
                self.assertFalse(IgUgcReward.objects.filter(assessment=assessment).exists())
                self.assertFalse(
                    IgUgcRewardDelivery.objects.filter(reward__assessment=assessment).exists()
                )
                self.assertFalse(
                    IgUgcRewardLifetime.objects.filter(client=case_client).exists()
                )

    def test_manager_review_api_is_generation_bound_and_queues_exact_snapshot(self):
        from django.test import Client

        assessment = self._create_assessment(
            "api",
            confidence=Decimal("0.88"),
            generation=3,
        )
        http = Client()
        http.force_login(self.actor)
        response = http.post(
            reverse(
                "management_bot_client_ugc_assessment_review_api",
                args=[self.client.pk, assessment.pk],
            ),
            {"decision": "approve", "generation": "2"},
        )
        self.assertEqual(response.status_code, 409)

        response = http.post(
            reverse(
                "management_bot_client_ugc_assessment_review_api",
                args=[self.client.pk, assessment.pk],
            ),
            {
                "decision": "approve",
                "generation": "3",
                "note": "  Звірено provider provenance та фото  ",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["reward"]["reward_path"], "external_ugc")
        self.assertFalse(payload["reward_eligible"])
        self.assertEqual(payload["eligibility_reason"], "already_rewarded")
        self.assertEqual(payload["delivery"]["state"], "pending")
        self.assertIn("90", payload["delivery"]["message_snapshot"])
        assessment.refresh_from_db()
        reward = assessment.rewards.get()
        self.assertEqual(assessment.reviewed_by_id, self.actor.pk)
        self.assertIsNotNone(assessment.reviewed_at)
        self.assertEqual(assessment.decision_source, "manager")
        self.assertEqual(reward.reviewed_by_id, self.actor.pk)
        self.assertEqual(reward.review_note, "Звірено provider provenance та фото")
        self.assertEqual(payload["reward"]["review_note"], reward.review_note)

    def test_manager_approval_is_terminal_and_replay_returns_the_existing_reward(self):
        from django.test import Client

        from management.ig_bot_models import IgUgcEvidenceAssessment

        assessment = self._create_assessment(
            "api-terminal",
            confidence=Decimal("0.88"),
            generation=3,
        )
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.NEEDS_MANAGER_REVIEW,
        )
        http = Client()
        http.force_login(self.actor)
        url = reverse(
            "management_bot_client_ugc_assessment_review_api",
            args=[self.client.pk, assessment.pk],
        )

        first = http.post(
            url,
            {
                "decision": "approve",
                "generation": str(assessment.generation),
                "note": "Первинна ручна перевірка",
            },
        )

        self.assertEqual(first.status_code, 200)
        assessment.refresh_from_db()
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.MANAGER_APPROVED,
        )
        approved_generation = assessment.generation

        replay = http.post(
            url,
            {
                "decision": "approve",
                "generation": str(approved_generation),
                "note": "Повтор запиту після збою відповіді",
            },
        )

        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.json()["created"])
        self.assertEqual(
            replay.json()["reward"]["promo_code"],
            first.json()["reward"]["promo_code"],
        )
        assessment.refresh_from_db()
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.MANAGER_APPROVED,
        )
        self.assertEqual(assessment.generation, approved_generation)
        reward = assessment.rewards.get()
        self.assertEqual(reward.review_note, "Первинна ручна перевірка")

    def test_manager_review_ui_collects_a_required_approval_reason(self):
        from django.test import Client

        http = Client()
        http.force_login(self.actor)

        response = http.get(reverse("management_bot"))

        self.assertContains(response, "Причина підтвердження UGC")
        self.assertContains(response, "Додайте причину підтвердження UGC.")
        self.assertContains(response, "body.append('note',reviewNote.value.trim())")

    def test_manager_detail_exposes_post_issuance_linked_order_lifecycle(self):
        from django.test import Client

        from management.ig_bot_models import IgUgcReward
        from management.services.ig_order_assignments import link_order_to_client
        from management.services.ig_ugc_rewards import award_ugc_reward
        from orders.models import Order

        delivered_at = timezone.now() - timedelta(minutes=5)
        order = Order.objects.create(
            order_number="TWC-UGC-DETAIL",
            full_name="UGC lifecycle buyer",
            phone="380501112299",
            city="Kyiv",
            np_office="Branch 9",
            total_sum=Decimal("1000.00"),
            payment_status="paid",
            status="done",
            tracking_number="20450000000009",
            tracking_status_code=9,
            tracking_terminal_at=delivered_at,
        )
        link_order_to_client(order, client=self.client, actor=self.actor)
        evidence = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text="Відмітила TwoComms після отримання",
            provider_created_at=delivered_at + timedelta(minutes=1),
        )
        reward, _created = award_ugc_reward(
            client=self.client,
            order=order,
            actor=self.actor,
            evidence_message_id=evidence.pk,
            review_note="Перевірено фото та відповідність замовленню",
        )
        lifecycle_updated_at = timezone.now().replace(microsecond=0)
        reward.lifecycle_state = IgUgcReward.LifecycleState.HELD
        reward.lifecycle_reason = "service_case_open"
        reward.lifecycle_updated_at = lifecycle_updated_at
        reward.save(update_fields=[
            "lifecycle_state",
            "lifecycle_reason",
            "lifecycle_updated_at",
        ])

        http = Client()
        http.force_login(self.actor)
        response = http.get(
            reverse("management_bot_client_detail_api", args=[self.client.pk])
        )

        self.assertEqual(response.status_code, 200)
        ugc = response.json()["ugc_rewards"]
        self.assertFalse(ugc["reward_eligible"])
        self.assertEqual(ugc["eligibility_reason"], "already_rewarded")
        item = ugc["items"][0]
        self.assertEqual(item["lifecycle_state"], "held")
        self.assertEqual(item["lifecycle_reason"], "service_case_open")
        self.assertEqual(
            item["lifecycle_updated_at"],
            lifecycle_updated_at.isoformat(),
        )
        self.assertFalse(item["reward_eligible"])

    def test_manager_ugc_ui_labels_lifecycle_and_suppresses_second_issuance(self):
        from django.test import Client

        http = Client()
        http.force_login(self.actor)

        response = http.get(reverse("management_bot"))

        self.assertContains(response, "Активна · -10%")
        self.assertContains(response, "Призупинена")
        self.assertContains(response, "Відкликана")
        self.assertContains(response, "Відкрите звернення клієнта")
        self.assertContains(response, "Замовлення-джерело скасовано")
        self.assertContains(response, "Повторна видача цієї довічної нагороди недоступна.")
        self.assertContains(
            response,
            "issued.length===0&&ugc.reward_eligible===true",
        )
        self.assertContains(response, "item.reward_eligible===true")

    def test_manager_linked_order_ui_requires_a_non_blank_review_reason(self):
        from django.test import Client

        http = Client()
        http.force_login(self.actor)

        response = http.get(reverse("management_bot"))

        self.assertContains(response, "note.required=true")
        self.assertContains(response, "if(!note.value.trim())")
        self.assertContains(response, "Додайте причину підтвердження UGC.")
        self.assertContains(response, "note.focus()")
        self.assertContains(
            response,
            "body.append('review_note',note.value.trim())",
        )

    def test_rejected_assessment_cannot_be_later_approved(self):
        from django.test import Client

        from management.ig_bot_models import IgUgcEvidenceAssessment, IgUgcReward

        assessment = self._create_assessment(
            "api-rejected",
            confidence=Decimal("0.88"),
        )
        http = Client()
        http.force_login(self.actor)
        url = reverse(
            "management_bot_client_ugc_assessment_review_api",
            args=[self.client.pk, assessment.pk],
        )

        rejected = http.post(
            url,
            {"decision": "reject", "generation": str(assessment.generation)},
        )
        self.assertEqual(rejected.status_code, 200)
        assessment.refresh_from_db()
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.REJECTED,
        )

        late_approval = http.post(
            url,
            {"decision": "approve", "generation": str(assessment.generation)},
        )

        self.assertEqual(late_approval.status_code, 409)
        self.assertFalse(IgUgcReward.objects.exists())

    def test_manager_approval_rolls_back_when_reward_outbox_creation_fails(self):
        from unittest.mock import patch

        from django.test import Client

        from management.ig_bot_models import (
            IgUgcReward,
            IgUgcRewardDelivery,
            IgUgcRewardLifetime,
        )

        assessment = self._create_assessment(
            "api-rollback",
            confidence=Decimal("0.88"),
            generation=4,
        )
        original = {
            "decision": assessment.decision,
            "decision_source": assessment.decision_source,
            "generation": assessment.generation,
            "reviewed_by_id": assessment.reviewed_by_id,
            "reviewed_at": assessment.reviewed_at,
        }
        http = Client()
        http.force_login(self.actor)

        with patch(
            "management.services.ig_ugc_rewards.queue_external_ugc_reward_delivery",
            side_effect=RuntimeError("forced manager outbox failure"),
        ), self.assertRaisesRegex(RuntimeError, "forced manager outbox failure"):
            http.post(
                reverse(
                    "management_bot_client_ugc_assessment_review_api",
                    args=[self.client.pk, assessment.pk],
                ),
                {
                    "decision": "approve",
                    "generation": str(assessment.generation),
                    "note": "Перевірено менеджером",
                },
            )

        assessment.refresh_from_db()
        self.assertEqual(
            {
                "decision": assessment.decision,
                "decision_source": assessment.decision_source,
                "generation": assessment.generation,
                "reviewed_by_id": assessment.reviewed_by_id,
                "reviewed_at": assessment.reviewed_at,
            },
            original,
        )
        self.assertFalse(IgUgcReward.objects.exists())
        self.assertFalse(IgUgcRewardLifetime.objects.exists())
        self.assertFalse(IgUgcRewardDelivery.objects.exists())
        self.assertFalse(PromoCode.objects.exists())

    def test_manager_reward_api_exposes_idempotent_eligibility_at_top_level(self):
        from django.test import Client
        from management.models import InstagramBotMessage
        from management.services.ig_order_assignments import link_order_to_client
        from orders.models import Order

        order = Order.objects.create(
            order_number="TWC-API-ELIGIBILITY",
            full_name="API eligibility buyer",
            phone="380501112255",
            city="Kyiv",
            np_office="Branch 3",
            total_sum=Decimal("1000.00"),
            payment_status="paid",
            status="done",
            tracking_number="20450000000004",
            tracking_status_code=9,
            tracking_terminal_at=timezone.now(),
        )
        link_order_to_client(order, client=self.client, actor=self.actor)
        evidence = InstagramBotMessage.objects.create(
            sender_id=self.client.igsid,
            client=self.client,
            role=InstagramBotMessage.Role.USER,
            text="Відмітила TwoComms",
            provider_created_at=timezone.now(),
        )
        http = Client()
        http.force_login(self.actor)
        url = reverse("management_bot_client_ugc_reward_api", args=[self.client.pk])
        payload = {
            "order_id": order.pk,
            "evidence_message_id": evidence.pk,
            "review_note": "Перевірено фото та відповідність замовленню",
        }

        first = http.post(url, payload).json()
        replay = http.post(url, payload).json()

        self.assertTrue(first["success"])
        self.assertFalse(first["reward_eligible"])
        self.assertEqual(first["eligibility_reason"], "already_rewarded")
        self.assertFalse(replay["reward_eligible"])
        self.assertEqual(replay["eligibility_reason"], "already_rewarded")
        self.assertEqual(
            first["reward"]["promo_code"],
            replay["reward"]["promo_code"],
        )

    def test_ambiguous_delivery_is_not_blindly_retried(self):
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
            queue_external_ugc_reward_delivery,
        )
        from management.ig_bot_models import IgUgcRewardDelivery
        from unittest.mock import patch

        reward, _ = award_external_ugc_reward(client=self.client, assessment=self.assessment, actor=self.actor, review_note="Test manager approval")
        delivery = queue_external_ugc_reward_delivery(reward)
        with patch(
            "management.services.instagram_bot.send_text",
            return_value=type("Receipt", (), {"ok": False, "kind": "unknown", "provider_message_ids": ()})(),
        ) as send:
            state = process_external_ugc_reward_delivery(delivery.pk)
            self.assertEqual(state, IgUgcRewardDelivery.State.AMBIGUOUS)
            self.assertEqual(process_external_ugc_reward_delivery(delivery.pk), IgUgcRewardDelivery.State.AMBIGUOUS)
            send.assert_called_once()

    def test_transient_delivery_is_terminal_ambiguous_without_resend_or_reissue(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcReward, IgUgcRewardDelivery
        from management.services.ig_follow_reconcile import _due_ugc_deliveries
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
        )

        reward, created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        self.assertTrue(created)
        receipt = type(
            "Receipt",
            (),
            {"ok": False, "kind": "transient", "provider_message_ids": ()},
        )()

        with patch(
            "management.services.instagram_bot.send_text",
            return_value=receipt,
        ) as send:
            first = process_external_ugc_reward_delivery(reward.delivery.pk)
            replay = process_external_ugc_reward_delivery(reward.delivery.pk)

        self.assertEqual(first, IgUgcRewardDelivery.State.AMBIGUOUS)
        self.assertEqual(replay, IgUgcRewardDelivery.State.AMBIGUOUS)
        send.assert_called_once()
        reward.delivery.refresh_from_db()
        self.assertEqual(reward.delivery.state, IgUgcRewardDelivery.State.AMBIGUOUS)
        self.assertEqual(reward.delivery.attempts, 1)
        self.assertIsNotNone(reward.delivery.completed_at)
        self.assertEqual(
            _due_ugc_deliveries(
                now=timezone.now() + timedelta(days=1),
                limit=10,
            ),
            [],
        )
        self.assertEqual(IgUgcReward.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)

    def test_expired_processing_delivery_becomes_ambiguous_without_resend(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
        )

        reward, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        delivery = reward.delivery
        delivery.state = IgUgcRewardDelivery.State.PROCESSING
        delivery.lease_token = "crashed-worker"
        delivery.lease_expires_at = timezone.now() - timedelta(seconds=1)
        delivery.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )

        with patch("management.services.instagram_bot.send_text") as send:
            state = process_external_ugc_reward_delivery(delivery.pk)

        self.assertEqual(state, IgUgcRewardDelivery.State.AMBIGUOUS)
        send.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.last_error, "stale_processing_provider_outcome_unknown")

    def test_reconciler_selects_expired_processing_delivery(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import award_external_ugc_reward

        reward, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        delivery = reward.delivery
        delivery.state = IgUgcRewardDelivery.State.PROCESSING
        delivery.lease_token = "crashed-worker"
        delivery.lease_expires_at = timezone.now() - timedelta(seconds=1)
        delivery.save(
            update_fields=["state", "lease_token", "lease_expires_at", "updated_at"]
        )

        output = StringIO()
        with patch(
            "management.services.ig_ugc_rewards.process_external_ugc_reward_delivery",
            return_value=IgUgcRewardDelivery.State.AMBIGUOUS,
        ) as process:
            call_command(
                "reconcile_ig_follow_intelligence",
                limit=10,
                stdout=output,
            )

        process.assert_called_once_with(delivery.pk)
        self.assertIn("ambiguous=1", output.getvalue())

    def test_known_meta_delivery_blocks_never_reach_send_api(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
        )

        reward, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        delivery = reward.delivery
        for delivery_status in (
            "window_closed",
            "advanced_access",
            "message_request_check",
            "send_blocked",
        ):
            with self.subTest(delivery_status=delivery_status):
                self.client.delivery_status = delivery_status
                self.client.save(update_fields=["delivery_status", "updated_at"])
                delivery.state = IgUgcRewardDelivery.State.PENDING
                delivery.due_at = timezone.now()
                delivery.lease_token = ""
                delivery.lease_expires_at = None
                delivery.save(
                    update_fields=[
                        "state",
                        "due_at",
                        "lease_token",
                        "lease_expires_at",
                        "updated_at",
                    ]
                )
                with patch("management.services.instagram_bot.send_text") as send:
                    state = process_external_ugc_reward_delivery(delivery.pk)

                self.assertEqual(state, IgUgcRewardDelivery.State.WAITING_WINDOW)
                send.assert_not_called()

    def test_lifetime_digest_is_stable_when_client_row_is_recreated(self):
        from types import SimpleNamespace

        from management.services.ig_ugc_rewards import _identity_digest

        first = _identity_digest(SimpleNamespace(pk=101, igsid="  IG-Identity-1 "))
        recreated = _identity_digest(SimpleNamespace(pk=808, igsid="ig-identity-1"))

        self.assertEqual(first, recreated)

    @override_settings(
        SECRET_KEY="rotated-django-secret",
        IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="v2",
        IG_UGC_IDENTITY_HMAC_KEYRING={
            "v1": "old-ugc-identity-key-0000000000000001",
            "v2": "new-ugc-identity-key-0000000000000002",
        },
    )
    def test_lifetime_digest_uses_versioned_keyring_not_django_secret(self):
        from types import SimpleNamespace

        from management.services.ig_ugc_rewards import (
            _identity_digest,
            _identity_digest_candidates,
        )

        identity = SimpleNamespace(igsid="stable-identity")
        active = _identity_digest(identity)
        candidates = _identity_digest_candidates(identity)

        self.assertTrue(active.startswith("v2:"))
        self.assertEqual(candidates[0], active)
        self.assertEqual({value.split(":", 1)[0] for value in candidates}, {"v1", "v2"})

    @override_settings(
        IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="v1",
        IG_UGC_IDENTITY_HMAC_KEYRING={
            "v1": "old-ugc-identity-key-0000000000000001",
            "v2": "new-ugc-identity-key-0000000000000002",
        },
    )
    def test_retained_key_prevents_second_reward_after_active_key_rotation(self):
        from management.services.ig_ugc_rewards import award_external_ugc_reward

        first, created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        self.assertTrue(created)
        self.assertTrue(first.lifetime_slot_key.startswith("v1:"))

        with self.settings(IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="v2"):
            replay, replay_created = award_external_ugc_reward(
                client=self.client,
                assessment=self.assessment,
                actor=self.actor,
                review_note="Test manager approval",
            )

        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, first.pk)

    def test_privacy_delete_then_recreate_same_igsid_cannot_mint_second_reward(self):
        from management.ig_bot_models import IgClient, IgUgcReward
        from management.services.ig_ugc_rewards import UgcRewardConflict, award_external_ugc_reward

        _first, created = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        self.assertTrue(created)

        from management.bot_views import _delete_direct_bot_records

        deletion = _delete_direct_bot_records(self.client.igsid)
        self.assertEqual(deletion["clients"], 1)
        recreated = IgClient.get_or_create_for_sender("external-ugc-client")
        assessment = self._create_assessment("recreated", client=recreated)

        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(client=recreated, assessment=assessment)
        self.assertEqual(IgUgcReward.objects.count(), 0)

    def test_versioned_migration_lifetime_slot_survives_privacy_delete_recreate(self):
        """A migration-created slot remains authoritative after its client is erased."""
        from importlib import import_module

        from management.ig_bot_models import IgClient, IgUgcRewardLifetime
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
        )
        from management.bot_views import _delete_direct_bot_records

        migration = import_module("management.migrations.0158_ig_ugc_intelligence")
        digest = migration._identity_digest_for_igsid(self.client.igsid)
        IgUgcRewardLifetime.objects.create(
            client=self.client,
            identity_digest=digest,
            consumed_at=timezone.now(),
        )

        deletion = _delete_direct_bot_records(self.client.igsid)
        self.assertEqual(deletion["clients"], 1)
        recreated = IgClient.get_or_create_for_sender("external-ugc-client")
        assessment = self._create_assessment("migration-recreated", client=recreated)

        with self.assertRaises(UgcRewardConflict):
            award_external_ugc_reward(client=recreated, assessment=assessment)

    def test_detail_api_exposes_ugc_eligibility_reason(self):
        from django.test import Client

        http = Client()
        http.force_login(self.actor)

        response = http.get(
            reverse("management_bot_client_detail_api", args=[self.client.pk])
        )

        self.assertEqual(response.status_code, 200)
        ugc = response.json()["ugc_rewards"]
        self.assertTrue(ugc["reward_eligible"])
        self.assertEqual(ugc["eligibility_reason"], "qualified_assessment")

    def test_latest_source_assessment_wins_over_older_higher_generation(self):
        """Per-source generation must not hide a newer qualifying mention."""
        from management.ig_bot_models import IgUgcEvidenceAssessment
        from management.services.ig_ugc_rewards import ugc_reward_eligibility

        self.assessment.decision = IgUgcEvidenceAssessment.Decision.REJECTED
        self.assessment.reason_codes = ["evidence_rejected"]
        self.assessment.generation = 9
        self.assessment.save(update_fields=["decision", "reason_codes", "generation", "updated_at"])
        latest = self._create_assessment("newer-source", generation=1)

        eligible, reason = ugc_reward_eligibility(self.client)

        self.assertTrue(eligible)
        self.assertEqual(reason, "qualified_assessment")
        self.assertEqual(latest.decision, IgUgcEvidenceAssessment.Decision.QUALIFIED_AUTO)

    def test_later_rejected_source_does_not_hide_earlier_qualifying_evidence(self):
        """One unrelated rejected repost cannot invalidate another valid mention."""
        from management.ig_bot_models import IgUgcEvidenceAssessment
        from management.services.ig_ugc_rewards import ugc_reward_eligibility

        rejected = self._create_assessment("later-rejected")
        rejected.decision = IgUgcEvidenceAssessment.Decision.REJECTED
        rejected.reason_codes = ["evidence_rejected"]
        rejected.save(update_fields=["decision", "reason_codes", "updated_at"])

        eligible, reason = ugc_reward_eligibility(self.client)

        self.assertTrue(eligible)
        self.assertEqual(reason, "qualified_assessment")

    def _open_service_case(self, *, status=None, source_suffix="service"):
        from management.ig_bot_models import IgPostSaleCase

        source = InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            mid=f"service-mid-{source_suffix}",
            text="Потрібна допомога із замовленням",
        )
        return IgPostSaleCase.objects.create(
            client=self.client,
            source_message=source,
            case_type=IgPostSaleCase.CaseType.RETURN,
            status=status or IgPostSaleCase.Status.OPEN,
        )

    def test_active_service_case_blocks_eligibility_and_external_issuance(self):
        from management.services.ig_ugc_rewards import (
            UgcRewardConflict,
            award_external_ugc_reward,
            ugc_reward_eligibility,
        )

        self._open_service_case()

        eligible, reason = ugc_reward_eligibility(self.client)
        self.assertFalse(eligible)
        self.assertEqual(reason, "service_case_open")
        with self.assertRaisesRegex(UgcRewardConflict, "service"):
            award_external_ugc_reward(
                client=self.client,
                assessment=self.assessment,
                actor=self.actor,
                review_note="Test manager approval",
            )

    def test_latest_support_complaint_blocks_ugc_without_case_row(self):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.ig_ugc_rewards import ugc_reward_eligibility

        IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            dedupe_key="ugc-active-support-complaint",
            score_band=IgConversationAnalysisSnapshot.Band.PAID,
            interaction_type=(
                IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT
            ),
        )

        eligible, reason = ugc_reward_eligibility(self.client)

        self.assertFalse(eligible)
        self.assertEqual(reason, "service_case_open")

    def test_completed_service_case_clears_older_complaint_snapshot(self):
        from management.ig_bot_models import (
            IgConversationAnalysisSnapshot,
            IgPostSaleCase,
        )
        from management.services.ig_ugc_rewards import ugc_reward_eligibility

        IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            dedupe_key="ugc-resolved-support-complaint",
            score_band=IgConversationAnalysisSnapshot.Band.PAID,
            interaction_type=(
                IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT
            ),
        )
        self._open_service_case(
            status=IgPostSaleCase.Status.COMPLETED,
            source_suffix="completed",
        )

        eligible, reason = ugc_reward_eligibility(self.client)

        self.assertTrue(eligible)
        self.assertEqual(reason, "qualified_assessment")

    def test_new_complaint_after_completed_service_case_blocks_again(self):
        from management.ig_bot_models import (
            IgConversationAnalysisSnapshot,
            IgPostSaleCase,
        )
        from management.services.ig_ugc_rewards import ugc_reward_eligibility

        terminal_at = timezone.now() - timedelta(minutes=5)
        case = self._open_service_case(
            status=IgPostSaleCase.Status.COMPLETED,
            source_suffix="completed-before-new-complaint",
        )
        IgPostSaleCase.objects.filter(pk=case.pk).update(
            resolved_at=terminal_at,
            updated_at=terminal_at + timedelta(minutes=10),
        )
        IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            dedupe_key="ugc-new-support-complaint-after-resolution",
            score_band=IgConversationAnalysisSnapshot.Band.PAID,
            interaction_type=(
                IgConversationAnalysisSnapshot.InteractionType.SUPPORT_COMPLAINT
            ),
            analyzed_at=terminal_at + timedelta(minutes=1),
        )

        eligible, reason = ugc_reward_eligibility(self.client)

        self.assertFalse(eligible)
        self.assertEqual(reason, "service_case_open")

    def test_active_service_case_blocks_assessment_auto_award(self):
        self._open_service_case(source_suffix="assessment")

        assessment = self._create_assessment("service-assessment")

        self.assertEqual(assessment.decision, "needs_manager_review")
        self.assertIn("service_case_open", assessment.reason_codes)

    def test_delivery_waits_when_service_case_opens_after_grant(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
        )

        reward, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        self._open_service_case(source_suffix="delivery")

        with patch("management.services.instagram_bot.send_text") as send:
            state = process_external_ugc_reward_delivery(reward.delivery.pk)

        self.assertEqual(state, IgUgcRewardDelivery.State.WAITING_WINDOW)
        reward.delivery.refresh_from_db()
        self.assertEqual(reward.delivery.last_error, "service_case_open")
        send.assert_not_called()

    def test_delivery_waits_when_response_window_is_closed(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
            queue_external_ugc_reward_delivery,
        )

        reward, _ = award_external_ugc_reward(client=self.client, assessment=self.assessment, actor=self.actor, review_note="Test manager approval")
        delivery = queue_external_ugc_reward_delivery(reward)
        self.client.last_message_at = timezone.now() - timedelta(hours=24)
        self.client.save(update_fields=["last_message_at", "updated_at"])

        with patch("management.services.instagram_bot.send_text") as send:
            state = process_external_ugc_reward_delivery(delivery.pk)

        self.assertEqual(state, IgUgcRewardDelivery.State.WAITING_WINDOW)
        send.assert_not_called()

    def test_waiting_delivery_resumes_with_same_snapshot_and_is_not_replayed(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
            queue_external_ugc_reward_delivery,
        )

        reward, _ = award_external_ugc_reward(client=self.client, assessment=self.assessment, actor=self.actor, review_note="Test manager approval")
        delivery = queue_external_ugc_reward_delivery(reward)
        original_snapshot = delivery.message_snapshot
        self.client.last_message_at = timezone.now() - timedelta(hours=24)
        self.client.save(update_fields=["last_message_at", "updated_at"])
        self.assertEqual(
            process_external_ugc_reward_delivery(delivery.pk),
            IgUgcRewardDelivery.State.WAITING_WINDOW,
        )

        self.client.last_message_at = timezone.now()
        self.client.save(update_fields=["last_message_at", "updated_at"])
        receipt = type(
            "Receipt",
            (),
            {"ok": True, "kind": "", "provider_message_ids": ("ig-mid-ugc-1",)},
        )()
        with patch(
            "management.services.instagram_bot.send_text",
            return_value=receipt,
        ) as send:
            first = process_external_ugc_reward_delivery(delivery.pk)
            replay = process_external_ugc_reward_delivery(delivery.pk)

        self.assertEqual(first, IgUgcRewardDelivery.State.SENT)
        self.assertEqual(replay, IgUgcRewardDelivery.State.SENT)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], original_snapshot)
        delivery.refresh_from_db()
        self.assertEqual(delivery.message_snapshot, original_snapshot)
        self.assertEqual(delivery.provider_message_ids, ["ig-mid-ugc-1"])

    def test_delivery_revalidates_opt_out_and_takeover_before_provider_io(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
            queue_external_ugc_reward_delivery,
        )

        for field, value in (
            ("opted_out_at", timezone.now()),
            ("manager_takeover", True),
        ):
            with self.subTest(field=field):
                reward, _ = award_external_ugc_reward(
                    client=self.client,
                    assessment=self.assessment,
                    actor=self.actor,
                    review_note="Test manager approval",
                )
                delivery = queue_external_ugc_reward_delivery(reward)
                setattr(self.client, field, value)
                self.client.save(update_fields=[field, "updated_at"])
                with patch("management.services.instagram_bot.send_text") as send:
                    state = process_external_ugc_reward_delivery(delivery.pk)
                self.assertEqual(state, IgUgcRewardDelivery.State.WAITING_WINDOW)
                send.assert_not_called()
                setattr(self.client, field, False if field == "manager_takeover" else None)
                self.client.save(update_fields=[field, "updated_at"])

    def test_expired_promo_is_not_sent_or_reissued(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
            queue_external_ugc_reward_delivery,
        )

        reward, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        reward.promo_code.valid_until = timezone.now() - timedelta(seconds=1)
        reward.promo_code.save(update_fields=["valid_until"])
        delivery = queue_external_ugc_reward_delivery(reward)

        with patch("management.services.instagram_bot.send_text") as send:
            state = process_external_ugc_reward_delivery(delivery.pk)

        self.assertEqual(state, IgUgcRewardDelivery.State.FAILED)
        send.assert_not_called()
        delivery.refresh_from_db()
        self.assertEqual(delivery.last_error, "promo_expired")
        self.assertIsNotNone(delivery.completed_at)

        from management.services.ig_follow_reconcile import _due_ugc_deliveries

        self.assertEqual(
            _due_ugc_deliveries(now=timezone.now() + timedelta(days=1), limit=10),
            [],
        )

    def test_reconcile_does_not_hot_loop_terminal_promo_failure(self):
        from unittest.mock import patch

        from management.services.ig_follow_reconcile import (
            reconcile_follow_intelligence_once,
        )
        from management.services.ig_ugc_rewards import award_external_ugc_reward

        reward, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        reward.promo_code.is_active = False
        reward.promo_code.save(update_fields=["is_active"])

        with patch("management.services.instagram_bot.send_text") as send:
            first = reconcile_follow_intelligence_once(limit=10)
            second = reconcile_follow_intelligence_once(limit=10)

        self.assertEqual(first["ugc_selected"], 1)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["ugc_selected"], 0)
        send.assert_not_called()

    def test_retryable_delivery_failure_uses_bounded_backoff(self):
        from unittest.mock import patch

        from management.ig_bot_models import IgUgcRewardDelivery
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            process_external_ugc_reward_delivery,
        )

        reward, _ = award_external_ugc_reward(
            client=self.client,
            assessment=self.assessment,
            actor=self.actor,
            review_note="Test manager approval",
        )
        now = timezone.now()
        retry_receipt = type(
            "Receipt",
            (),
            {"ok": False, "kind": "retryable", "provider_message_ids": ()},
        )()
        with patch(
            "management.services.instagram_bot.send_text",
            return_value=retry_receipt,
        ) as send:
            first = process_external_ugc_reward_delivery(reward.delivery.pk)

            self.assertEqual(first, IgUgcRewardDelivery.State.FAILED)
            reward.delivery.refresh_from_db()
            self.assertIsNone(reward.delivery.completed_at)
            self.assertGreater(reward.delivery.due_at, now)
            from management.services.ig_follow_reconcile import _due_ugc_deliveries

            self.assertEqual(_due_ugc_deliveries(now=now, limit=10), [])
            for attempt in (2, 3):
                reward.delivery.due_at = timezone.now() - timedelta(seconds=1)
                reward.delivery.save(update_fields=["due_at", "updated_at"])
                state = process_external_ugc_reward_delivery(reward.delivery.pk)
                self.assertEqual(state, IgUgcRewardDelivery.State.FAILED)
                reward.delivery.refresh_from_db()
                self.assertEqual(reward.delivery.attempts, attempt)

        self.assertIsNotNone(reward.delivery.completed_at)
        self.assertEqual(
            _due_ugc_deliveries(now=timezone.now() + timedelta(days=1), limit=10),
            [],
        )
        self.assertEqual(send.call_count, 3)
