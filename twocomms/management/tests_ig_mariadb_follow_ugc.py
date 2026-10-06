"""MariaDB-only row-lock gates for follow CTA and UGC rewards."""

import hashlib
from importlib import import_module
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.apps import apps
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image

from management.models import (
    IgClient,
    IgCommercialEpisode,
    IgConversationAnalysisSnapshot,
    IgFollowCapabilityState,
    IgFollowCtaDecision,
    IgFollowObservation,
    IgFollowState,
    IgUgcEvidenceAssessment,
    IgUgcReward,
    IgUgcRewardDelivery,
    IgUgcRewardLifetime,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import ig_follow_cta, ig_follow_state
from management.services.ig_follow_state import configuration_fingerprint
from management.services.ig_order_assignments import link_order_to_client
from orders.models import Order
from storefront.models import PromoCode, PromoCodeGuestUsage


class PrivateMediaMariaDbConcurrencyTests(TransactionTestCase):
    reset_sequences = False
    def test_inflight_capture_cannot_leave_blob_after_privacy_success(self):
        from management.bot_views import _delete_direct_bot_records
        from management.services import instagram_bot
        from management.services.ig_private_media import private_media_storage

        client = IgClient.objects.create(
            igsid="mariadb-private-capture",
            username="mariadb_private_capture",
        )
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            mid="mariadb-private-capture-mid",
            media_capture_eligible=True,
            attachment_media=[{
                "url": "https://lookaside.invalid/capture.jpg",
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )
        download_started = Event()
        finish_download = Event()
        errors = []
        storage = private_media_storage()
        before_files = {
            path.relative_to(storage.location)
            for path in Path(storage.location).rglob("*")
            if path.is_file()
        }

        def delayed_download(_url, *, failure_context=None, **_limits):
            download_started.set()
            if not finish_download.wait(timeout=10):
                raise RuntimeError("capture test timeout")
            return "image/jpeg", b"capture-race"

        def capture():
            close_old_connections()
            try:
                instagram_bot._capture_message_media(
                    InstagramBotMessage.objects.get(pk=row.pk)
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        with patch.object(
            instagram_bot,
            "download_image",
            side_effect=delayed_download,
        ):
            thread = Thread(target=capture, daemon=True)
            thread.start()
            try:
                self.assertTrue(download_started.wait(timeout=10),
                    f"capture did not reach download; thread errors: {errors!r}")
                with self.assertRaises(RuntimeError):
                    _delete_direct_bot_records(client.username)
            finally:
                finish_download.set()
                thread.join(timeout=15)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        _delete_direct_bot_records(client.username)
        self.assertFalse(IgClient.objects.filter(pk=client.pk).exists())
        self.assertFalse(InstagramBotMessage.objects.filter(pk=row.pk).exists())
        after_files = {
            path.relative_to(storage.location)
            for path in Path(storage.location).rglob("*")
            if path.is_file()
        }
        self.assertEqual(after_files, before_files)

    def test_purge_skips_real_reader_lease_until_release(self):
        from management.services.ig_private_media import (
            acquire_blob_use,
            private_media_storage,
            purge_due,
            release_blob_use,
        )

        client = IgClient.objects.create(igsid="mariadb-private-reader")
        storage = private_media_storage()
        name = storage.save("ig_message_media/read.ogg", ContentFile(b"read-race"))
        row = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            private_media_state="active",
            private_media_delete_after=timezone.now() + timedelta(minutes=2),
            attachment_media=[{
                "status": "owned",
                "private_storage": True,
                "storage_name": name,
                "mime": "audio/ogg",
            }],
        )
        acquired = Event()
        release = Event()
        tokens = []

        def reader():
            close_old_connections()
            token = acquire_blob_use(row.pk, seconds=120)
            tokens.append(token)
            acquired.set()
            release.wait(timeout=10)
            release_blob_use(row.pk, token)
            close_old_connections()

        thread = Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(acquired.wait(timeout=10))
        self.assertTrue(tokens[0], "fixture must acquire the reader lease before expiry")
        InstagramBotMessage.objects.filter(pk=row.pk).update(
            private_media_delete_after=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(purge_due(now=timezone.now(), limit=1), 0)
        self.assertTrue(storage.exists(name))
        release.set()
        thread.join(timeout=10)
        self.assertEqual(purge_due(now=timezone.now(), limit=1), 1)
        self.assertFalse(storage.exists(name))


class InstagramFollowUgcMariaDbContractTests(SimpleTestCase):
    databases = {"default"}

    def test_database_is_disposable_mariadb_11_4(self):
        self.assertEqual(connection.vendor, "mysql")
        self.assertRegex(
            connection.settings_dict["NAME"],
            r"^test_twocomms_ig_[a-f0-9]{12}$",
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION(), @@version_comment")
            version, comment = cursor.fetchone()
        self.assertTrue(str(version).startswith("11.4"))
        self.assertIn("mariadb", f"{version} {comment}".lower())

    def test_0177_rehearses_partial_ddl_resume_on_real_mariadb(self):
        migration = import_module(
            "management.migrations.0177_gemini_adaptive_routing"
        )
        field = IgClient._meta.get_field("privacy_erasure_started_at")
        table = IgClient._meta.db_table
        with connection.schema_editor(atomic=False) as schema_editor:
            schema_editor.remove_field(IgClient, field)
            migration.ensure_0177_schema(apps, schema_editor)
            migration.ensure_0177_schema(apps, schema_editor)
        with connection.cursor() as cursor:
            columns = {
                item.name
                for item in connection.introspection.get_table_description(
                    cursor,
                    table,
                )
            }
        self.assertIn(field.column, columns)


class _MariaDbConcurrencyCase(TransactionTestCase):
    # Resetting hundreds of unrelated AUTO_INCREMENT counters adds minutes per
    # test on MariaDB and provides no evidence for these row-lock assertions.
    reset_sequences = False
    cleanup_tables = (
        "management_igugcrewarddelivery",
        "management_igugcrewardlifetime",
        "storefront_promocodeguestusage",
        "management_igugcreward",
        "management_igugcevidenceassessment",
        "management_igorderassignmentevent",
        "management_igorderassignment",
        "management_igfollowctadecision",
        "management_igfollowobservation",
        "management_igfollowrefreshjob",
        "management_igfollowstate",
        "management_igfollowcapabilitystate",
        "management_igconversationanalysissnapshot",
        "management_instagrambotmessage",
        "management_igcommercialepisode",
        "management_igclient",
        "management_instagrambotsettings",
        "storefront_promocode",
        "orders_order",
        "auth_user",
    )

    def _fixture_teardown(self):
        """Truncate only suite-owned fixtures; DELETE triggers are intentional."""
        database = str(connection.settings_dict.get("NAME") or "")
        if connection.vendor != "mysql" or not database.startswith("test_twocomms_ig_"):
            raise RuntimeError("MariaDB concurrency cleanup requires a disposable database")
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            try:
                for table in self.cleanup_tables:
                    cursor.execute(f"TRUNCATE TABLE {quote(table)}")
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

    def _run_workers(self, *workers):
        barrier = Barrier(len(workers))
        result_lock = Lock()
        results = []
        errors = []

        def run(index, worker):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                value = worker()
                with result_lock:
                    results.append((index, value))
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            Thread(target=run, args=(index, worker), daemon=True)
            for index, worker in enumerate(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return [value for _index, value in sorted(results)]


class FollowCtaMariaDbConcurrencyTests(_MariaDbConcurrencyCase):
    candidate_payment = (
        "Якщо вам близький наш підхід, будемо раді бачити вас серед підписників."
    )
    candidate_hesitation = (
        "Можливо, вам буде цікаво залишатися поруч із TwoComms та стежити "
        "за новими історіями бренду."
    )

    def _client_fixture(self, suffix, *, current_episode=True):
        now = timezone.now().replace(microsecond=0)
        client = IgClient.objects.create(
            igsid=f"mariadb-follow-{suffix}",
            language="uk",
            stage=IgClient.Stage.PAYMENT_PENDING,
            first_contact_at=now - timedelta(hours=1),
            last_message_at=now,
        )
        episode = IgCommercialEpisode.objects.create(
            client=client,
            sequence=1,
            materialization_key=f"mariadb-follow:{suffix}:episode:1",
            opened_watermark_message_id=0,
        )
        if current_episode:
            client.current_commercial_episode = episode
            client.save(update_fields=["current_commercial_episode", "updated_at"])
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            mid=f"mariadb-follow-{suffix}-mid",
            text="Думаю над замовленням",
            status=InstagramBotMessage.Status.DONE,
            provider_created_at=now,
        )
        settings_obj = InstagramBotSettings.load()
        IgFollowState.objects.create(
            client=client,
            state=IgFollowState.State.NOT_FOLLOWING,
            revision=1,
            source="instagram_login",
            config_fingerprint=configuration_fingerprint(settings_obj),
            observed_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            last_result=IgFollowState.CheckResult.KNOWN,
        )
        return now, client, episode, message

    def _opportunity(self, *, client, episode, message, opportunity, now, base_text):
        return ig_follow_cta.evaluate_follow_opportunity(
            client=client,
            opportunity=opportunity,
            episode=episode,
            source_message=message,
            base_text=base_text,
            now=now,
        )

    def _authorize_pair(self, first, second, *, now):
        return self._run_workers(
            lambda: ig_follow_cta.authorize_follow_cta(
                first.pk,
                current_base_text=first.base_text,
                now=now,
            ),
            lambda: ig_follow_cta.authorize_follow_cta(
                second.pk,
                current_base_text=second.base_text,
                now=now,
            ),
        )

    def test_payment_beats_hesitation_for_one_episode_under_real_row_locks(self):
        now, client, episode, message = self._client_fixture("same-episode")
        IgConversationAnalysisSnapshot.objects.create(
            client=client,
            last_analyzed_message=message,
            dedupe_key="mariadb-follow-same-episode-analysis",
            score_band=IgConversationAnalysisSnapshot.Band.QUALIFIED,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.PRICE_OBJECTION,
            confidence=Decimal("0.91"),
            purchase_probability=Decimal("0.85"),
            commercial_episode=episode,
            analyzed_at=now,
        )
        payment = self._opportunity(
            client=client,
            episode=episode,
            message=message,
            opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
            now=now,
            base_text="Оплату отримали, дякуємо.",
        )
        hesitation = self._opportunity(
            client=client,
            episode=episode,
            message=message,
            opportunity=IgFollowCtaDecision.Opportunity.HESITATION,
            now=now,
            base_text="Можу підказати з розміром.",
        )
        self.assertTrue(payment.allowed)
        self.assertTrue(hesitation.allowed)
        # Persist hesitation first so the winner is policy-driven, not PK-driven.
        hesitation_decision = ig_follow_cta.prepare_follow_decision(
            hesitation,
            candidate_text=self.candidate_hesitation,
        )
        payment_decision = ig_follow_cta.prepare_follow_decision(
            payment,
            candidate_text=self.candidate_payment,
        )

        authorized = self._authorize_pair(
            payment_decision,
            hesitation_decision,
            now=now,
        )

        self.assertEqual(sum(result is not None for result in authorized), 1)
        payment_decision.refresh_from_db()
        hesitation_decision.refresh_from_db()
        self.assertEqual(payment_decision.state, IgFollowCtaDecision.State.RESERVED)
        self.assertEqual(hesitation_decision.state, IgFollowCtaDecision.State.PREPARED)
        self.assertEqual(
            IgFollowCtaDecision.objects.filter(
                commercial_episode=episode,
                state=IgFollowCtaDecision.State.RESERVED,
            ).count(),
            1,
        )

    def test_two_episodes_share_one_global_active_reservation(self):
        now, client, first_episode, message = self._client_fixture(
            "cross-episode",
            current_episode=False,
        )
        second_episode = IgCommercialEpisode.objects.create(
            client=client,
            sequence=2,
            materialization_key="mariadb-follow:cross-episode:episode:2",
            opened_watermark_message_id=0,
            open_slot=None,
        )
        first = self._opportunity(
            client=client,
            episode=first_episode,
            message=message,
            opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
            now=now,
            base_text="Оплату отримали, дякуємо.",
        )
        second = self._opportunity(
            client=client,
            episode=second_episode,
            message=message,
            opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
            now=now,
            base_text="Платіж підтверджено, дякуємо.",
        )
        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        first_decision = ig_follow_cta.prepare_follow_decision(
            first,
            candidate_text=self.candidate_payment,
        )
        second_decision = ig_follow_cta.prepare_follow_decision(
            second,
            candidate_text=self.candidate_hesitation,
        )

        authorized = self._authorize_pair(first_decision, second_decision, now=now)

        self.assertEqual(sum(result is not None for result in authorized), 1)
        self.assertEqual(
            IgFollowCtaDecision.objects.filter(
                client=client,
                state=IgFollowCtaDecision.State.RESERVED,
            ).count(),
            1,
        )

    def test_stale_follow_refresh_lease_cannot_publish_after_reclaim(self):
        now, client, _episode, _message = self._client_fixture("stale-lease")
        job = ig_follow_state.request_follow_refresh(
            client,
            trigger="payment",
            now=now,
        )

        claims = self._run_workers(
            lambda: ig_follow_state._claim_job(job.pk, now=now),
            lambda: ig_follow_state._claim_job(job.pk, now=now),
        )
        tokens = [token for claimed, _settings, token in claims if claimed is not None]
        self.assertEqual(len(tokens), 1)
        stale_token = tokens[0]

        IgFollowState.objects.filter(client=client).update(
            refresh_lease_token="",
            refresh_lease_expires_at=None,
        )
        type(job).objects.filter(pk=job.pk).update(
            lease_expires_at=now - timedelta(seconds=1),
        )
        reclaimed, _settings, current_token = ig_follow_state._claim_job(
            job.pk,
            now=now + timedelta(seconds=1),
        )
        self.assertIsNotNone(reclaimed)
        self.assertNotEqual(current_token, stale_token)
        outcome = ig_follow_state._publish_lookup(
            job.pk,
            stale_token,
            ig_follow_state._LookupResult(kind="known", value=True, http_code=200),
            now=now + timedelta(seconds=2),
        )

        self.assertEqual(outcome, "lease_lost")
        job.refresh_from_db()
        self.assertEqual(job.lease_token, current_token)
        self.assertEqual(
            IgFollowObservation.objects.filter(client=client).count(),
            0,
        )
        self.assertEqual(
            IgFollowCapabilityState.objects.get(singleton_key=1).status,
            IgFollowCapabilityState.Status.UNKNOWN,
        )


@override_settings(
    IG_UGC_AUTO_AWARD_MODE="auto",
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    },
)
class UgcMariaDbConcurrencyTests(_MariaDbConcurrencyCase):
    def _assessment(self, client):
        from management.services.ig_ugc_assessment import assess_ugc_evidence

        buffer = BytesIO()
        Image.new("RGB", (16, 16), color=(32, 64, 96)).save(buffer, format="JPEG")
        raw = buffer.getvalue()
        storage_name = default_storage.save(
            "ig/owned/mariadb-cross-path.jpg",
            ContentFile(raw),
        )
        self.addCleanup(default_storage.delete, storage_name)
        source_id = "mariadb-ugc-cross-path-mid"
        provider_key = "story:mariadb-ugc-cross-path"
        message = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            mid=source_id,
            text="Відмітила TwoComms",
            media_capture_eligible=True,
            attachment_media=[{
                "url": "https://lookaside.fbsbx.com/media/mariadb-cross-path.jpg",
                "provider_object_key": provider_key,
                "provider_media_id": "media:mariadb-cross-path",
                "provider_event_id": source_id,
                "media_type": "story_mention",
                "target_username": "twocomms",
                "provider_native_mention": True,
                "provenance": "live_webhook",
                "status": "owned",
                "storage_name": storage_name,
                "mime": "image/jpeg",
                "bytes": len(raw),
                "content_hash": hashlib.sha256(raw).hexdigest(),
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
                "brand_match_confidence": Decimal("0.99"),
                "catalog_matches": [{"product_id": 42, "confidence": Decimal("0.99")}],
                "risk_flags": [],
                "people_count": 1,
                "garment_count": 1,
            },
        )
        self.assertEqual(
            assessment.decision,
            IgUgcEvidenceAssessment.Decision.QUALIFIED_AUTO,
        )
        return assessment

    def test_external_and_delivered_paths_consume_one_lifetime_slot(self):
        from management.services.ig_ugc_rewards import (
            award_external_ugc_reward,
            award_ugc_reward,
        )

        now = timezone.now().replace(microsecond=0)
        client = IgClient.get_or_create_for_sender("mariadb-ugc-cross-path")
        client.last_message_at = now
        client.save(update_fields=["last_message_at", "updated_at"])
        manager = get_user_model().objects.create_user(
            username="mariadb-ugc-manager",
            password="test-password",
            is_staff=True,
        )
        order = Order.objects.create(
            order_number="TWC-MARIADB-UGC-RACE",
            full_name="MariaDB UGC buyer",
            phone="380501112233",
            city="Kyiv",
            np_office="Branch 1",
            total_sum=Decimal("1000.00"),
            payment_status="paid",
            status="done",
            tracking_number="20450000000005",
            tracking_status_code=9,
            tracking_terminal_at=now,
        )
        link_order_to_client(order, client=client, actor=manager)
        evidence = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="Відмітила TwoComms після отримання",
            provider_created_at=now + timedelta(seconds=1),
        )
        assessment = self._assessment(client)

        results = self._run_workers(
            lambda: award_external_ugc_reward(
                client=client,
                assessment=assessment,
                actor=manager,
                review_note="Manager-approved external UGC evidence.",
            ),
            lambda: award_ugc_reward(
                client=client,
                order=order,
                actor=manager,
                evidence_message_id=evidence.pk,
                review_note="Післяпокупкове фото з видимим одягом TwoComms.",
            ),
        )

        reward_ids = [reward.pk for reward, _created in results]
        self.assertEqual(len(set(reward_ids)), 1)
        self.assertEqual(sum(bool(created) for _reward, created in results), 1)
        self.assertEqual(IgUgcReward.objects.count(), 1)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 1)
        self.assertEqual(IgUgcRewardDelivery.objects.count(), 1)
        self.assertEqual(PromoCode.objects.count(), 1)
        lifetime = IgUgcRewardLifetime.objects.get()
        self.assertEqual(lifetime.reward_id, reward_ids[0])
        self.assertIsNotNone(lifetime.consumed_at)

    def test_concurrent_guest_reservations_consume_exactly_one_capacity(self):
        from orders.promo_reservations import (
            PromoReservationError,
            reserve_promo_for_checkout,
        )

        now = timezone.now()
        client = IgClient.get_or_create_for_sender("mariadb-guest-promo")
        assessment = IgUgcEvidenceAssessment.objects.create(
            client=client,
            source_message_id="mariadb-guest-promo-story",
            provider_object_key="story:mariadb-guest-promo",
            provider_object_digest="a" * 64,
            provider_event_id="mariadb-guest-promo-story",
            target_username="twocomms",
            evidence_fingerprint="mariadb-guest-promo-assessment",
            decision=IgUgcEvidenceAssessment.Decision.QUALIFIED_AUTO,
            decision_source="auto",
            policy_version="ugc-v1",
            reward_owner_client_id=client.pk,
        )
        promo = PromoCode.objects.create(
            code="UGCMARIADB1",
            discount_type="percentage",
            discount_value=Decimal("10.00"),
            max_uses=1,
            one_time_per_user=False,
            guest_redeemable=True,
            promo_type="regular",
            valid_from=now,
            valid_until=now + timedelta(days=90),
            is_active=True,
        )
        reward = IgUgcReward.objects.create(
            client=client,
            evidence_type=IgUgcReward.EvidenceType.STORY_MENTION,
            evidence_fingerprint="mariadb-guest-promo-reward",
            promo_code=promo,
            reward_path="external_ugc",
            decision_source="auto",
            assessment=assessment,
            lifetime_slot_key="mariadb-guest-promo-slot",
        )
        IgUgcRewardLifetime.objects.create(
            client=client,
            identity_digest="mariadb-guest-promo-lifetime",
            reward=reward,
            consumed_at=now,
        )

        def reserve():
            try:
                reservation = reserve_promo_for_checkout(
                    code=promo.code,
                    user=None,
                    total_amount=Decimal("1000.00"),
                )
                return "reserved", reservation.discount
            except PromoReservationError as exc:
                return "rejected", exc.reason

        results = self._run_workers(reserve, reserve)

        self.assertEqual([state for state, _value in results].count("reserved"), 1)
        self.assertEqual([state for state, _value in results].count("rejected"), 1)
        promo.refresh_from_db()
        self.assertEqual(promo.current_uses, 1)
        self.assertEqual(PromoCodeGuestUsage.objects.count(), 1)
        self.assertEqual(
            PromoCodeGuestUsage.objects.get().state,
            PromoCodeGuestUsage.State.RESERVED,
        )
