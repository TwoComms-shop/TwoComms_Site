import json
import inspect
from io import StringIO
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

from django.test import SimpleTestCase, TestCase
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationAnalysisJob,
    IgConversationAnalysisSnapshot,
    IgDeal,
    IgFollowUpTask,
    IgPaymentConfirmationReview,
    IgPaymentProjection,
    IgPaymentReviewDecision,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import bot_conversation_analysis as analysis


class ConversationAnalysisLeasePolicyTests(SimpleTestCase):
    def test_analysis_module_has_no_direct_operational_writer_calls(self):
        source = inspect.getsource(analysis)

        for forbidden_call in (
            "start_repeat_episode(",
            "ensure_rule_classification(",
            "project_observed_stage(",
            "create_payment_review(",
        ):
            with self.subTest(forbidden_call=forbidden_call):
                self.assertNotIn(forbidden_call, source)

    def test_commercial_episode_lock_is_reentrant_per_client(self):
        from management.services import ig_commercial_episodes

        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        with patch.object(ig_commercial_episodes, "connection") as connection:
            connection.vendor = "mysql"
            connection.cursor.return_value = cursor_context

            with ig_commercial_episodes.commercial_episode_client_lock(42):
                with ig_commercial_episodes.commercial_episode_client_lock(42):
                    pass

        self.assertEqual(
            cursor.execute.call_args_list,
            [
                call("SELECT GET_LOCK(%s, 15)", ["twocomms:ig-episode:42"]),
                call("SELECT RELEASE_LOCK(%s)", ["twocomms:ig-episode:42"]),
            ],
        )

    def test_latest_truth_change_ignores_unrelated_deal_updates(self):
        old_payment_truth = timezone.now() - timedelta(days=2)
        order_truth = timezone.now() - timedelta(days=1)
        unrelated_deal_update = timezone.now()
        unrelated_order_update = timezone.now()
        client = Mock()
        client.deals.aggregate.return_value = {
            "deal_updated_at": unrelated_deal_update,
            "order_updated_at": unrelated_order_update,
            "payment_truth_updated_at": old_payment_truth,
            "order_truth_updated_at": order_truth,
        }
        client.payment_projections.aggregate.return_value = {"updated_at": None}
        client.payment_review_decisions.aggregate.return_value = {"created_at": None}
        client.commercial_episodes.aggregate.return_value = {"updated_at": None}

        changed_at = analysis._latest_truth_change(client)

        self.assertEqual(changed_at, order_truth)
        aggregate_fields = client.deals.aggregate.call_args.kwargs
        self.assertNotIn("deal_updated_at", aggregate_fields)
        self.assertNotIn("order_updated_at", aggregate_fields)

    def test_order_truth_change_filter_is_field_specific_and_update_fields_safe(self):
        from management.services.ig_order_truth import order_truth_changed

        previous = {
            "status": "new",
            "payment_status": "unpaid",
            "tracking_number": "",
            "shipment_status": "",
        }
        current = SimpleNamespace(
            status="ship",
            payment_status="unpaid",
            tracking_number="",
            shipment_status="",
            manager_comment="unrelated",
        )

        self.assertTrue(
            order_truth_changed(previous, current, update_fields={"status"})
        )
        self.assertFalse(
            order_truth_changed(
                previous,
                current,
                update_fields={"manager_comment"},
            )
        )

    @patch("management.models.IgDeal")
    def test_order_delete_is_blocked_while_a_deal_owns_it(self, deal_model):
        from management.services.ig_order_truth import publish_order_truth_unlink

        deal_model.objects.using.return_value.filter.return_value.exists.return_value = True
        with self.assertRaisesMessage(ValueError, "не можна видалити"):
            publish_order_truth_unlink(
                sender=Mock(),
                instance=SimpleNamespace(pk=17),
            )

        deal_model.objects.using.assert_called_once_with("default")
        deal_model.objects.using.return_value.filter.assert_called_once_with(order_id=17)
        deal_model.objects.using.return_value.filter.return_value.update.assert_not_called()

    @patch("management.services.gemini_keys.key_project_groups")
    def test_historical_backfill_requires_explicit_flag_and_complete_mapping(self, groups):
        from management.services.gemini_keys import ALL_KEYS

        settings_obj = SimpleNamespace(analysis_backfill_enabled=False)
        groups.return_value = {alias: "project-a" for alias in ALL_KEYS}
        self.assertFalse(analysis._historical_backfill_allowed(settings_obj))

        settings_obj.analysis_backfill_enabled = True
        groups.return_value = {alias: "project-a" for alias in ALL_KEYS[:-1]}
        self.assertFalse(analysis._historical_backfill_allowed(settings_obj))

        groups.return_value = {alias: "project-a" for alias in ALL_KEYS}
        self.assertTrue(analysis._historical_backfill_allowed(settings_obj))

    def test_reconcile_cutoff_excludes_history_but_keeps_post_rollout_events(self):
        cutoff = timezone.now()
        old = cutoff - timedelta(days=1)
        recent = cutoff + timedelta(seconds=1)
        eligible = getattr(
            analysis,
            "_reconcile_candidate_is_eligible",
            lambda **_kwargs: True,
        )

        self.assertFalse(
            eligible(
                cutoff=cutoff,
                latest_message_at=old,
                job_created_at=old,
                truth_changed_at=old,
                include_history=False,
            )
        )
        for override in (
            {"latest_message_at": recent},
            {"truth_changed_at": recent},
            {"include_history": True},
        ):
            values = {
                "cutoff": cutoff,
                "latest_message_at": old,
                "job_created_at": old,
                "truth_changed_at": old,
                "include_history": False,
                **override,
            }
            with self.subTest(override=override):
                self.assertTrue(eligible(**values))

        self.assertFalse(
            eligible(
                cutoff=cutoff,
                latest_message_at=old,
                job_created_at=recent,
                truth_changed_at=old,
                include_history=False,
            )
        )

    def test_claim_ownership_requires_matching_unexpired_processing_lease(self):
        now = timezone.now()
        claim = SimpleNamespace(
            status=IgConversationAnalysisJob.Status.PROCESSING,
            lease_token="owner-a",
            lease_until=now + timedelta(seconds=30),
            watermark_message_id=11,
            revision=4,
            claimed_watermark_message_id=11,
            claimed_revision=4,
        )

        self.assertTrue(
            analysis._claim_is_current(
                claim,
                token="owner-a",
                claimed_watermark=11,
                claimed_revision=4,
                now=now,
            )
        )
        for override in (
            {"status": IgConversationAnalysisJob.Status.PENDING},
            {"lease_token": "owner-b"},
            {"lease_until": now},
            {"watermark_message_id": 10},
            {"watermark_message_id": 12},
            {"revision": 3},
            {"revision": 5},
            {"claimed_watermark_message_id": 10},
            {"claimed_revision": 3},
        ):
            stale = SimpleNamespace(**{**claim.__dict__, **override})
            self.assertFalse(
                analysis._claim_is_current(
                    stale,
                    token="owner-a",
                    claimed_watermark=11,
                    claimed_revision=4,
                    now=now,
                ),
                override,
            )

    def test_append_only_snapshot_has_complete_analysis_provenance(self):
        field_names = {
            field.name for field in IgConversationAnalysisSnapshot._meta.get_fields()
        }
        self.assertTrue(
            {
                "key_alias",
                "reasoning_level",
                "reasoning_policy_version",
                "thoughts_tokens",
                "candidates_tokens",
            }.issubset(field_names)
        )

    def test_pending_or_processing_new_revision_covers_reconciliation_state(self):
        base = {
            "watermark_message_id": 11,
            "revision": 4,
            "analyzed_revision": 3,
            "required_state_fingerprint": "state-a",
        }
        for status in (
            IgConversationAnalysisJob.Status.PENDING,
            IgConversationAnalysisJob.Status.PROCESSING,
        ):
            self.assertTrue(
                analysis._job_covers_required_analysis(
                    SimpleNamespace(**{**base, "status": status}),
                    watermark=11,
                    required_state_fingerprint="state-a",
                )
            )
        self.assertTrue(
            analysis._job_covers_required_analysis(
                SimpleNamespace(
                    **{
                        **base,
                        "status": IgConversationAnalysisJob.Status.FAILED,
                    }
                ),
                watermark=11,
                required_state_fingerprint="state-a",
            )
        )
        for override in (
            {"status": IgConversationAnalysisJob.Status.DONE},
            {"watermark_message_id": 10},
            {"revision": 3},
            {"required_state_fingerprint": "state-b"},
        ):
            self.assertFalse(
                analysis._job_covers_required_analysis(
                    SimpleNamespace(
                        **{
                            **base,
                            "status": IgConversationAnalysisJob.Status.PENDING,
                            **override,
                        }
                    ),
                    watermark=11,
                    required_state_fingerprint="state-a",
                ),
                override,
            )

    def test_exact_state_scheduler_coverage_handles_every_job_lifecycle(self):
        base = {
            "watermark_message_id": 11,
            "analyzed_watermark_message_id": 0,
            "revision": 4,
            "analyzed_revision": 3,
            "required_state_fingerprint": "state-a",
        }
        for status in (
            IgConversationAnalysisJob.Status.PENDING,
            IgConversationAnalysisJob.Status.PROCESSING,
            IgConversationAnalysisJob.Status.FAILED,
        ):
            self.assertTrue(
                analysis._job_covers_exact_state(
                    SimpleNamespace(**{**base, "status": status}),
                    watermark=11,
                    required_state_fingerprint="state-a",
                )
            )
        for status in (
            IgConversationAnalysisJob.Status.DONE,
            IgConversationAnalysisJob.Status.SKIPPED,
        ):
            current = SimpleNamespace(
                **{
                    **base,
                    "status": status,
                    "analyzed_watermark_message_id": 11,
                    "analyzed_revision": 4,
                }
            )
            self.assertTrue(
                analysis._job_covers_exact_state(
                    current,
                    watermark=11,
                    required_state_fingerprint="state-a",
                )
            )
            self.assertFalse(
                analysis._job_covers_exact_state(
                    current,
                    watermark=12,
                    required_state_fingerprint="state-a",
                )
            )

    @patch("management.services.bot_conversation_analysis._finish_failure")
    @patch(
        "management.services.bot_conversation_analysis._customer_reply_work_waiting",
        return_value=False,
    )
    @patch(
        "management.services.bot_conversation_analysis._process_claim",
        side_effect=["done", RuntimeError("provider failed")],
    )
    @patch("management.services.bot_conversation_analysis._reclaim_stale")
    @patch("management.services.bot_conversation_analysis._claim_due")
    @patch("management.services.bot_conversation_analysis.timezone.now")
    def test_batch_uses_fresh_clock_for_each_claim_and_failure(
        self, now_mock, claim_due, reclaim, process, _reply_waiting, finish_failure
    ):
        base = timezone.datetime(
            2026, 7, 24, 12, 0, tzinfo=timezone.get_current_timezone()
        )
        times = [base + timedelta(seconds=offset) for offset in range(3)]
        now_mock.side_effect = times
        first = SimpleNamespace(pk=1)
        second = SimpleNamespace(pk=2)
        claim_due.side_effect = [
            (first, 11, 1, "token-a"),
            (second, 12, 1, "token-b"),
        ]

        result = analysis.process_due_analysis(limit=2)

        self.assertEqual(result["done"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(claim_due.call_args_list, [call(times[0]), call(times[1])])
        self.assertEqual(reclaim.call_args_list, [call(times[0]), call(times[1])])
        self.assertEqual(finish_failure.call_args.args[-1], times[2])


class ConversationAnalysisProviderPolicyTests(SimpleTestCase):
    @patch("management.services.call_ai_analysis._run_with_pool")
    def test_management_json_uses_bounded_text_timeout_and_deadline(self, run):
        from management.services import call_ai_analysis

        run.return_value = {"parsed": {}}
        call_ai_analysis.gemini_generate_json("system", "user")

        self.assertEqual(
            run.call_args.kwargs["timeout"],
            call_ai_analysis.MANAGEMENT_TEXT_TIMEOUT,
        )
        self.assertEqual(
            run.call_args.kwargs["deadline_seconds"],
            call_ai_analysis.MANAGEMENT_TEXT_DEADLINE_SECONDS,
        )

    @patch("management.services.call_ai_analysis._run_with_pool")
    def test_inline_images_are_adjacent_to_explicit_message_bindings(self, run):
        from management.services import call_ai_analysis

        run.return_value = {"parsed": {}}
        call_ai_analysis.gemini_generate_json(
            "system",
            "user",
            images=[("image/jpeg", b"first"), ("image/png", b"second")],
            image_labels=[
                {"inline_image_index": 0, "original_index": 3},
                {"inline_image_index": 1, "original_index": 7},
            ],
        )

        parts = run.call_args.args[1]["contents"][0]["parts"]
        self.assertEqual(parts[1]["text"], "INLINE_IMAGE index=0 original_index=3")
        self.assertEqual(parts[2]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(parts[3]["text"], "INLINE_IMAGE index=1 original_index=7")
        self.assertEqual(parts[4]["inline_data"]["mime_type"], "image/png")


class ConversationAnalysisJobTests(TestCase):
    def setUp(self):
        self.client = IgClient.objects.create(igsid="analysis-job-client")
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = timezone.now() - timedelta(days=1)
        settings.analysis_backfill_enabled = False
        settings.save(update_fields=[
            "analysis_reconcile_after", "analysis_backfill_enabled",
        ])

    def message(
        self,
        text,
        *,
        role=InstagramBotMessage.Role.USER,
        source="webhook",
        attachments="",
        attachment_media=None,
    ):
        return InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=role,
            text=text,
            status=InstagramBotMessage.Status.DONE,
            source=source,
            attachments=attachments,
            attachment_media=attachment_media or [],
        )

    def operational_digest(self):
        from management.ig_bot_models import (
            IgCommerceSelectionSession,
            IgCommercialEpisode,
            IgCommercialEpisodeEvent,
        )

        self.client.refresh_from_db()
        return {
            "client": (self.client.stage, self.client.current_commercial_episode_id),
            "episodes": list(
                IgCommercialEpisode.objects.filter(client=self.client)
                .order_by("id")
                .values_list(
                    "id",
                    "state",
                    "open_slot",
                    "opened_watermark_message_id",
                    "repeat_kind",
                )
            ),
            "episode_events": list(
                IgCommercialEpisodeEvent.objects.filter(episode__client=self.client)
                .order_by("id")
                .values_list("id", "episode_id", "event_type")
            ),
            "sessions": list(
                IgCommerceSelectionSession.objects.filter(client=self.client)
                .order_by("id")
                .values_list(
                    "id", "state", "open_slot", "generation", "lines"
                )
            ),
            "deals": list(
                IgDeal.objects.filter(client=self.client)
                .order_by("id")
                .values_list(
                    "id", "status", "payment_status", "order_id", "amount"
                )
            ),
            "payments": list(
                IgPaymentProjection.objects.filter(client=self.client)
                .order_by("id")
                .values_list("id", "deal_id", "truth", "gross_amount")
            ),
        }

    def pending_repeat_event(
        self,
        *,
        text="Мені сподобалось, хочу ще одну",
        confidence="0.9500",
        repeat_kind="explicit_more",
        publish=True,
    ):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.ig_analysis_events import publish_repeat_episode_event

        deal = IgDeal.objects.create(client=self.client, amount=Decimal("790.00"))
        IgPaymentProjection.objects.create(
            deal=deal,
            client=self.client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("790.00"),
            paid_at=timezone.now(),
        )
        message = self.message(text)
        fingerprint = analysis._required_state_fingerprint(self.client, message.pk)
        repeat_intent = {
            "kind": repeat_kind,
            "confidence": confidence,
            "evidence_message_ids": [message.pk],
        }
        snapshot = IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            last_analyzed_message=message,
            dedupe_key=f"test-repeat:{self.client.pk}:{message.pk}:{repeat_kind}",
            score_band=IgConversationAnalysisSnapshot.Band.PAID,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.HIGH_INTENT,
            purchase_probability=Decimal("0.8600"),
            confidence=Decimal("0.9200"),
            repeat_intent=repeat_intent,
            analysis_model="gemini-test",
            analysis_prompt_version=analysis.ANALYSIS_PROMPT_VERSION,
            required_state_fingerprint=fingerprint,
        )
        if not publish:
            return message, snapshot
        event = publish_repeat_episode_event(
            client=self.client,
            snapshot=snapshot,
            repeat_intent=repeat_intent,
            analysis_model=snapshot.analysis_model,
            analysis_prompt_version=snapshot.analysis_prompt_version,
            required_state_fingerprint=fingerprint,
        )
        return message, event

    def test_repeat_normalization_accepts_mixed_exchange_and_new_purchase_only(self):
        mixed = {
            11: {
                "role": "user",
                "text": "Хочу обміняти розмір і ще одну футболку замовити",
            }
        }
        exchange_only = {
            12: {
                "role": "user",
                "text": "Хочу обміняти цю футболку на ще одну розміру L",
            }
        }
        parsed = {
            "repeat_intent": {
                "kind": "explicit_more",
                "confidence": 0.95,
                "evidence_message_ids": [11],
            }
        }

        self.assertEqual(
            analysis._normalize(parsed, mixed, verified_payment=True)["repeat_intent"][
                "evidence_message_ids"
            ],
            [11],
        )
        parsed["repeat_intent"]["evidence_message_ids"] = [12]
        self.assertEqual(
            analysis._normalize(parsed, exchange_only, verified_payment=True)[
                "repeat_intent"
            ],
            {},
        )

    def test_schedule_coalesces_to_latest_watermark_and_moves_due_time(self):
        first = self.message("Підкажіть розмір")
        second = self.message("І ціну чорної")
        now = timezone.now()

        analysis.schedule_analysis(self.client, first, now=now)
        analysis.schedule_analysis(self.client, second, now=now + timedelta(seconds=5))

        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.watermark_message_id, second.id)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.due_at, now + timedelta(seconds=5 + analysis.DEBOUNCE_SECONDS))

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    @patch("management.services.instagram_bot.download_image")
    def test_historical_media_is_metadata_only_before_provider_analysis(
        self,
        download,
        generate,
    ):
        url = "https://lookaside.example/expired-analysis.jpg"
        message = self.message(
            "Що це за модель?",
            source="manual_refresh",
            attachments=json.dumps([url]),
        )
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )

        def provider_result(*_args, **_kwargs):
            job = IgConversationAnalysisJob.objects.get(client=self.client)
            self.assertEqual(job.media_phase, "metadata_only")
            self.assertEqual(job.media_error_kind, "")
            self.assertIsNotNone(job.media_started_at)
            self.assertIsNotNone(job.media_completed_at)
            return {
                "parsed": {
                    "interaction_type": "product_interest",
                    "score_band": "exploring",
                    "purchase_probability": 0.4,
                    "confidence": 0.7,
                },
                "model": "gemini-test",
                "meta": {},
            }

        generate.side_effect = provider_result

        self.assertEqual(
            analysis.process_due_analysis(limit=1),
            {"done": 1, "failed": 0, "skipped": 0, "superseded": 0},
        )
        download.assert_not_called()

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    @patch(
        "management.services.bot_conversation_analysis._conversation",
        side_effect=RuntimeError("media decoding failed"),
    )
    def test_media_failure_is_typed_before_provider_is_called(self, _conversation, generate):
        message = self.message("Ось фото")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )

        self.assertEqual(
            analysis.process_due_analysis(limit=1),
            {"done": 0, "failed": 1, "skipped": 0, "superseded": 0},
        )

        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.media_phase, "failed")
        self.assertEqual(job.media_error_kind, "media_exception")
        self.assertIsNotNone(job.media_started_at)
        self.assertIsNotNone(job.media_completed_at)
        generate.assert_not_called()

    def test_stale_lease_during_media_keeps_an_incomplete_acquiring_phase(self):
        message = self.message("Ось фото")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            message,
            now=now - timedelta(minutes=1),
        )
        job, _watermark, _revision, token = analysis._claim_due(now)
        self.assertTrue(analysis._record_media_phase(
            job.pk,
            token,
            phase=IgConversationAnalysisJob.MediaPhase.ACQUIRING,
            started_at=now,
        ))
        IgConversationAnalysisJob.objects.filter(pk=job.pk).update(
            lease_until=now - timedelta(seconds=1),
        )

        self.assertEqual(analysis._reclaim_stale(now), 1)

        job.refresh_from_db()
        self.assertEqual(job.last_error, "stale_lease_recovered")
        self.assertEqual(job.media_phase, "acquiring")
        self.assertEqual(job.media_started_at, now)
        self.assertIsNone(job.media_completed_at)

    @patch(
        "management.services.bot_conversation_analysis.gemini_generate_json",
        side_effect=RuntimeError("provider timeout"),
    )
    def test_provider_failure_happens_after_media_phase_completed(self, generate):
        message = self.message("Підкажіть ціну")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )

        self.assertEqual(
            analysis.process_due_analysis(limit=1),
            {"done": 0, "failed": 1, "skipped": 0, "superseded": 0},
        )

        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.media_phase, "ready")
        self.assertEqual(job.media_error_kind, "")
        self.assertIsNotNone(job.media_started_at)
        self.assertIsNotNone(job.media_completed_at)
        self.assertIn("provider timeout", job.last_error)
        generate.assert_called_once()

    def test_media_phase_heartbeat_extends_processing_lease(self):
        message = self.message("Ось фото")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            message,
            now=now - timedelta(minutes=1),
        )
        job, _watermark, _revision, token = analysis._claim_due(now)
        before = job.lease_until

        self.assertTrue(analysis._record_media_phase(
            job.pk,
            token,
            phase=IgConversationAnalysisJob.MediaPhase.ACQUIRING,
            started_at=now,
        ))

        job.refresh_from_db()
        self.assertGreater(job.lease_until, before)

    @patch("management.services.instagram_bot.download_image", return_value=None)
    @patch(
        "management.services.ig_payment_review._augment_messages_with_raw_media",
        side_effect=RuntimeError("raw media unavailable"),
    )
    def test_media_recovery_error_is_exposed_before_provider_phase(self, _augment, _download):
        message = self.message(
            "Ось фото",
            source="webhook",
            attachments=json.dumps(["https://lookaside.example/error.jpg"]),
        )

        _transcript, _by_id, media_sources = analysis._conversation(
            self.client.pk,
            message.pk,
        )

        self.assertTrue(any(
            item.get("media_error_kind") == "media_recovery_exception"
            for item in media_sources
        ))

    def test_generic_turn_intelligence_does_not_claim_media_was_inspected(self):
        url = "https://lookaside.example/already-analyzed.jpg"
        message = self.message(
            "Ось фото",
            source="webhook",
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "owned",
                "storage_name": "ig/already-analyzed.jpg",
                "mime": "image/jpeg",
            }],
        )
        message.turn_intelligence_artifact = {
            "schema_version": 1,
            "catalog_candidates": [{
                "product_id": 7,
                "title": "Validated",
                "confidence": 0.97,
            }],
            "transcript": "",
            "intent": "product_match",
            "confidence": 0.97,
            "catalog_resolution": "auto_select",
            "auto_product_id": 7,
        }
        message.save(update_fields=["turn_intelligence_artifact"])

        transcript, by_id, media_sources = analysis._conversation(
            self.client.pk,
            message.pk,
        )

        self.assertEqual(
            by_id[message.pk]["turn_intelligence"]["auto_product_id"],
            7,
        )
        self.assertTrue(any(item["message_id"] == message.pk for item in transcript))
        self.assertTrue(any(item.get("url") == url for item in media_sources))

    def test_hash_bound_per_part_inspection_avoids_second_media_pass(self):
        from management.services.ig_media_manifest import normalize_attachment_media

        url = "https://lookaside.example/bound-inspection.jpg"
        content_hash = "a" * 64
        media = normalize_attachment_media([{
            "url": url,
            "provenance": "live_webhook",
            "status": "owned",
            "storage_name": "ig/bound-inspection.jpg",
            "mime": "image/jpeg",
            "content_hash": content_hash,
        }], message_scope="analysis-bound-inspection", identity_origin="ingress")
        media[0]["inspection"] = {
            "state": "inspected",
            "source_part_id": media[0]["source_part_id"],
            "outcome": "understood",
            "content_hash": content_hash,
            "request_id": "request-bound",
            "provider_model": "gemini-actual",
        }
        message = self.message(
            "Ось фото",
            source="webhook",
            attachments=json.dumps([url]),
            attachment_media=media,
        )
        message.turn_intelligence_artifact = {
            "schema_version": 1,
            "catalog_candidates": [],
            "transcript": "",
            "intent": "media_review",
            "confidence": 0.8,
            "image_observations": [{
                "source_part_id": media[0]["source_part_id"],
                "content_hash": content_hash,
                "outcome": "understood",
            }],
        }
        message.save(update_fields=["turn_intelligence_artifact"])

        _transcript, _by_id, media_sources = analysis._conversation(
            self.client.pk,
            message.pk,
        )

        self.assertFalse(any(item.get("url") == url for item in media_sources))

    @patch("management.services.instagram_bot.download_image", return_value=None)
    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_live_media_temporary_failure_defers_analysis_until_capture_retry(
        self,
        generate,
        download,
    ):
        url = "https://lookaside.example/live-failed.jpg"
        message = self.message(
            "Ось фото",
            source="webhook",
            attachments=json.dumps([url]),
            attachment_media=[{
                "url": url,
                "provenance": "live_webhook",
                "status": "pending",
            }],
        )
        message.media_capture_eligible = True
        message.save(update_fields=["media_capture_eligible"])
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )

        IgConversationAnalysisJob.objects.filter(client=self.client).update(attempts=1)

        self.assertEqual(
            analysis.process_due_analysis(limit=1),
            {"done": 0, "failed": 0, "skipped": 0, "superseded": 0},
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        message.refresh_from_db()
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.last_error, "deferred_for_media_retry")
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.media_phase, "acquiring")
        self.assertIsNone(job.media_completed_at)
        self.assertTrue(message.attachment_media[0]["capture_retryable"])
        self.assertEqual(download.call_args.args, (url,))
        generate.assert_not_called()

    def test_duplicate_schedule_keeps_revision_due_backoff_and_processing_token(self):
        message = self.message("Підкажіть розмір")
        now = timezone.now()
        first = analysis.schedule_analysis(self.client, message, now=now)
        first_revision = first.revision
        first_due = first.due_at
        first.next_attempt_at = now + timedelta(minutes=10)
        first.attempts = 2
        first.save(update_fields=["next_attempt_at", "attempts", "updated_at"])

        duplicate = analysis.schedule_analysis(
            self.client,
            message,
            trigger="payment_truth",
            now=now + timedelta(minutes=1),
            delay_seconds=0,
        )

        duplicate.refresh_from_db()
        self.assertEqual(duplicate.revision, first_revision)
        self.assertEqual(duplicate.due_at, first_due)
        self.assertEqual(duplicate.next_attempt_at, now + timedelta(minutes=10))
        self.assertEqual(duplicate.attempts, 2)

        duplicate.next_attempt_at = now
        duplicate.due_at = now
        duplicate.save(update_fields=["next_attempt_at", "due_at", "updated_at"])
        claimed, _watermark, _revision, token = analysis._claim_due(now)
        processing_revision = claimed.revision

        processing_duplicate = analysis.schedule_analysis(
            self.client,
            message,
            trigger="order_truth",
            now=now + timedelta(minutes=2),
            delay_seconds=0,
        )

        processing_duplicate.refresh_from_db()
        self.assertEqual(processing_duplicate.revision, processing_revision)
        self.assertEqual(processing_duplicate.status, IgConversationAnalysisJob.Status.PROCESSING)
        self.assertEqual(processing_duplicate.lease_token, token)
        self.assertEqual(processing_duplicate.attempts, claimed.attempts)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_due_job_uses_high_reasoning_and_persists_grounded_snapshot(self, generate):
        message = self.message("Хочу чорну футболку M, але дорого")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "price_objection",
                "score_band": "qualified",
                "purchase_probability": 0.61,
                "confidence": 0.87,
                "evidence": [{
                    "message_id": message.id,
                    "quote": "але дорого",
                    "claim": "Явне заперечення щодо ціни",
                }],
                "uncertainties": ["Невідомий точний товар"],
            },
            "model": "gemini-3.5-flash",
            "meta": {
                "reasoning_task": "conversation_reanalysis",
                "reasoning_policy_version": "2026-07-23.v1",
                "key": "GEMINI_API3",
                "reasoning_level": "high",
                "latency_ms": 42,
            },
        }

        self.assertEqual(
            analysis.process_due_analysis(limit=1),
            {"done": 1, "failed": 0, "skipped": 0, "superseded": 0},
        )

        snapshot = IgConversationAnalysisSnapshot.objects.filter(
            client=self.client, analysis_model="gemini-3.5-flash"
        ).get()
        self.assertEqual(snapshot.last_analyzed_message_id, message.id)
        self.assertEqual(snapshot.reasoning_task, "conversation_reanalysis")
        self.assertEqual(snapshot.key_alias, "GEMINI_API3")
        self.assertEqual(snapshot.reasoning_level, "high")
        self.assertEqual(
            snapshot.reasoning_policy_version,
            "2026-07-23.v1",
        )
        self.assertTrue(snapshot.required_state_fingerprint)
        self.assertEqual(snapshot.evidence[0]["source_role"], "user")
        self.assertEqual(snapshot.evidence[0]["message_id"], message.id)
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.analysis_model, "gemini-3.5-flash")
        self.assertEqual(job.analysis_prompt_version, analysis.ANALYSIS_PROMPT_VERSION)
        self.assertEqual(job.key_alias, "GEMINI_API3")
        self.assertEqual(job.reasoning_level, "high")
        self.assertIsNotNone(job.analyzed_at)
        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["role"], "management")
        self.assertEqual(generate.call_args.kwargs["reasoning_task"], "conversation_reanalysis")
        provider_payload = json.loads(generate.call_args.args[1])
        self.assertIn("truth_state", provider_payload)
        self.assertFalse(provider_payload["truth_state"]["verified_payment"])
        self.assertEqual(provider_payload["truth_state"]["order_truth"], [])

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_model_cannot_claim_paid_without_verified_payment(self, generate):
        message = self.message("Оплачу завтра")
        analysis.schedule_analysis(self.client, message, now=timezone.now() - timedelta(minutes=1))
        generate.return_value = {
            "parsed": {
                "interaction_type": "paid_order_waiting",
                "score_band": "paid",
                "purchase_probability": 1,
                "confidence": 1,
                "evidence": [{"message_id": message.id, "quote": "Оплачу завтра"}],
            },
            "model": "gemini-3.5-flash",
            "meta": {},
        }

        analysis.process_due_analysis(limit=1)

        snapshot = IgConversationAnalysisSnapshot.objects.filter(
            client=self.client, analysis_model="gemini-3.5-flash"
        ).get()
        self.assertNotEqual(snapshot.score_band, IgConversationAnalysisSnapshot.Band.PAID)
        self.assertNotEqual(snapshot.interaction_type, "paid_order_waiting")
        self.assertLess(snapshot.purchase_probability, 1)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_unverifiable_evidence_is_dropped(self, generate):
        message = self.message("Цікавить розмір M")
        analysis.schedule_analysis(self.client, message, now=timezone.now() - timedelta(minutes=1))
        generate.return_value = {
            "parsed": {
                "interaction_type": "size_fit_question",
                "score_band": "qualified",
                "purchase_probability": 0.5,
                "confidence": 0.8,
                "evidence": [
                    {"message_id": message.id, "quote": "Я вже оплатив"},
                    {"message_id": 999999999, "quote": "Цікавить"},
                ],
            },
            "model": "gemini-3.5-flash",
            "meta": {},
        }

        analysis.process_due_analysis(limit=1)

        snapshot = IgConversationAnalysisSnapshot.objects.filter(
            client=self.client, analysis_model="gemini-3.5-flash"
        ).get()
        self.assertEqual(snapshot.evidence, [])
        self.assertIn("evidence_unverified", snapshot.uncertainties)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_paused_client_is_analyzed_but_hidden_client_is_not_scheduled(self, generate):
        self.client.bot_paused = True
        self.client.save(update_fields=["bot_paused", "updated_at"])
        paused_message = self.message("Цікавить футболка")
        analysis.schedule_analysis(self.client, paused_message, now=timezone.now() - timedelta(minutes=1))
        generate.return_value = {
            "parsed": {"interaction_type": "product_interest", "score_band": "exploring"},
            "model": "gemini-3.5-flash",
            "meta": {},
        }
        analysis.process_due_analysis(limit=1)
        generate.assert_called_once()

        self.client.hidden_at = timezone.now()
        self.client.save(update_fields=["hidden_at", "updated_at"])
        hidden_message = self.message("Ще одне повідомлення")
        hidden_job = analysis.schedule_analysis(
            self.client,
            hidden_message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.reset_mock()

        result = analysis.process_due_analysis(limit=1)

        self.assertIsNone(hidden_job)
        self.assertEqual(result["skipped"], 0)
        generate.assert_not_called()
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.DONE)
        self.assertLess(job.watermark_message_id, hidden_message.pk)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_due_job_for_client_that_becomes_spam_is_terminally_skipped(self, generate):
        message = self.message("Потрібен розмір M")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        self.client.stage = IgClient.Stage.SPAM
        self.client.save(update_fields=["stage", "updated_at"])

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result, {"done": 0, "failed": 0, "skipped": 1, "superseded": 0})
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.SKIPPED)
        self.assertEqual(job.skip_reason, "spam_or_blocked")
        generate.assert_not_called()

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_substantive_message_before_reaction_keeps_burst_eligible(self, generate):
        substantive = self.message("Чи є чорна футболка розміру M?")
        reaction = self.message("🔥")
        for message, interaction_type in (
            (substantive, IgConversationAnalysisSnapshot.InteractionType.PRODUCT_INTEREST),
            (reaction, IgConversationAnalysisSnapshot.InteractionType.REACTION_ONLY),
        ):
            IgConversationAnalysisSnapshot.objects.create(
                client=self.client,
                last_analyzed_message=message,
                dedupe_key=f"rules-window:{message.pk}",
                score_band=IgConversationAnalysisSnapshot.Band.EXPLORING,
                interaction_type=interaction_type,
                analysis_model="rules",
            )
        analysis.schedule_analysis(
            self.client,
            substantive,
            now=timezone.now() - timedelta(minutes=1),
        )
        analysis.schedule_analysis(
            self.client,
            reaction,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "size_fit_question",
                "score_band": "qualified",
            },
            "model": "gemini-3.6-flash",
            "meta": {},
        }

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["done"], 1)
        generate.assert_called_once()

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_reaction_only_changed_window_skips_gemini(self, generate):
        reaction = self.message("❤️")
        IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            last_analyzed_message=reaction,
            dedupe_key=f"rules-window:{reaction.pk}",
            score_band=IgConversationAnalysisSnapshot.Band.COLD,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.REACTION_ONLY,
            analysis_model="rules",
        )
        analysis.schedule_analysis(
            self.client,
            reaction,
            now=timezone.now() - timedelta(minutes=1),
        )
        before = self.operational_digest()

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.operational_digest(), before)
        generate.assert_not_called()

    @patch(
        "management.services.gemini_accounting_runtime.reconcile_expired_request_graphs",
        return_value=2,
    )
    def test_analysis_reconcile_also_closes_expired_request_graphs(self, reconcile_graphs):
        now = timezone.now()

        result = analysis.reconcile_analysis_jobs(limit=25, now=now)

        reconcile_graphs.assert_called_once_with(now=now, limit=25)
        self.assertEqual(result["request_graphs_reconciled"], 2)

    @patch(
        "management.services.gemini_accounting_runtime.reconcile_expired_request_graphs",
        side_effect=RuntimeError("corrupt graph fixture"),
    )
    def test_request_graph_housekeeping_failure_does_not_block_analysis_reconcile(
        self,
        reconcile_graphs,
    ):
        now = timezone.now()

        with patch.object(analysis.logger, "exception") as log_exception:
            result = analysis.reconcile_analysis_jobs(limit=25, now=now)

        reconcile_graphs.assert_called_once_with(now=now, limit=25)
        log_exception.assert_called_once_with(
            "expired Gemini request graph reconciliation failed"
        )
        self.assertEqual(result["request_graphs_reconciled"], 0)

    def test_skipped_job_is_reconciled_when_verified_payment_truth_changes(self):
        reaction = self.message("❤️")
        IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            last_analyzed_message=reaction,
            dedupe_key=f"rules-window:{reaction.pk}",
            score_band=IgConversationAnalysisSnapshot.Band.COLD,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.REACTION_ONLY,
            analysis_model="rules",
        )
        analysis.schedule_analysis(
            self.client,
            reaction,
            now=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(analysis.process_due_analysis(limit=1)["skipped"], 1)
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        revision = job.revision
        skipped_fingerprint = job.required_state_fingerprint

        deal = IgDeal.objects.create(client=self.client, amount=Decimal("1000.00"))
        IgPaymentProjection.objects.create(
            deal=deal,
            client=self.client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("1000.00"),
            paid_at=timezone.now(),
        )
        recovered = analysis.reconcile_analysis_jobs(now=timezone.now())

        job.refresh_from_db()
        self.assertEqual(recovered["queued"], 1)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.revision, revision + 1)
        self.assertNotEqual(job.required_state_fingerprint, skipped_fingerprint)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_failure_retries_without_losing_newer_watermark(self, generate):
        first = self.message("Перше")
        analysis.schedule_analysis(self.client, first, now=timezone.now() - timedelta(minutes=1))
        generate.side_effect = RuntimeError("provider down")
        before = self.operational_digest()

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.operational_digest(), before)
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.watermark_message_id, first.id)
        self.assertGreater(job.next_attempt_at, timezone.now())

    def test_analysis_job_relations_are_cross_engine_safe(self):
        self.assertFalse(IgConversationAnalysisJob._meta.get_field("client").db_constraint)

    @patch("management.services.instagram_bot.send_text")
    @patch("management.services.instagram_bot.gemini_generate")
    def test_opt_out_then_neutral_inbound_stays_observed_and_cancels_followup(
        self, generate, send_text
    ):
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "ai_enabled", "allowed_senders"])
        IgFollowUpTask.objects.create(
            client=self.client,
            due_at=timezone.now() + timedelta(hours=1),
        )

        self.assertTrue(instagram_bot.enqueue_inbound(
            settings,
            sender_id=self.client.igsid,
            text="Не пишите мне",
            mid="opt-out-mid",
        ))
        self.assertTrue(instagram_bot.enqueue_inbound(
            settings,
            sender_id=self.client.igsid,
            text="Добре",
            mid="after-opt-out-mid",
        ))
        instagram_bot.process_pending(settings)

        rows = InstagramBotMessage.objects.filter(client=self.client).order_by("id")
        self.assertEqual(list(rows.values_list("status", flat=True)), ["done", "done"])
        self.assertFalse(
            IgFollowUpTask.objects.filter(
                client=self.client, status=IgFollowUpTask.Status.PENDING
            ).exists()
        )
        generate.assert_not_called()
        send_text.assert_not_called()

    @patch("management.services.instagram_bot.send_text")
    @patch("management.services.instagram_bot.gemini_generate")
    @patch(
        "management.services.bot_sales_classifier.classify_message",
        side_effect=RuntimeError("classifier unavailable"),
    )
    def test_opt_out_ingress_fails_closed_when_classifier_raises(
        self, _classify, generate, send_text
    ):
        from management.services import instagram_bot

        settings = InstagramBotSettings.load()
        settings.is_enabled = True
        settings.ai_enabled = True
        settings.allowed_senders = ""
        settings.save(update_fields=["is_enabled", "ai_enabled", "allowed_senders"])
        task = IgFollowUpTask.objects.create(
            client=self.client,
            due_at=timezone.now() + timedelta(hours=1),
        )

        self.assertTrue(instagram_bot.enqueue_inbound(
            settings,
            sender_id=self.client.igsid,
            text="Мені не потрібно більше писати",
            mid="opt-out-classifier-failure",
        ))
        self.assertEqual(instagram_bot.process_pending(settings), 0)

        message = InstagramBotMessage.objects.get(mid="opt-out-classifier-failure")
        self.client.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(message.status, InstagramBotMessage.Status.DONE)
        self.assertIsNotNone(message.processed_at)
        self.assertIsNotNone(self.client.opted_out_at)
        self.assertEqual(self.client.opt_out_message_id, message.pk)
        self.assertTrue(self.client.bot_paused)
        self.assertEqual(self.client.paused_reason, "opt_out")
        self.assertEqual(task.status, IgFollowUpTask.Status.CANCELLED)
        generate.assert_not_called()
        send_text.assert_not_called()

    def test_reconciliation_queues_changed_conversation_without_operational_effects(self):
        message = self.message("Новий діалог")
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = message.created_at - timedelta(seconds=1)
        settings.save(update_fields=["analysis_reconcile_after"])

        with (
            patch("management.services.bot_conversation_analysis.gemini_generate_json") as generate,
            patch("management.services.bot_sales_classifier.ensure_rule_classification") as classify,
        ):
            result = analysis.reconcile_analysis_jobs(now=timezone.now())

        self.assertEqual(result["queued"], 1)
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.watermark_message_id, message.id)
        self.assertEqual(job.trigger, "reconcile")
        generate.assert_not_called()
        classify.assert_not_called()

    def test_reconciliation_does_not_queue_or_project_restricted_sender(self):
        message = self.message("Новий діалог від обмеженого відправника")
        settings = InstagramBotSettings.load()
        settings.allowed_senders = "another-sender"
        settings.analysis_reconcile_after = message.created_at - timedelta(seconds=1)
        settings.save(update_fields=["allowed_senders", "analysis_reconcile_after"])

        with (
            patch("management.services.bot_conversation_analysis.schedule_analysis") as schedule,
            patch(
                "management.services.bot_sales_classifier.reconcile_rules_projection"
            ) as project_rules,
        ):
            result = analysis.reconcile_analysis_jobs(now=timezone.now())

        self.assertEqual(result["queued"], 0)
        self.assertFalse(IgConversationAnalysisJob.objects.filter(client=self.client).exists())
        schedule.assert_not_called()
        project_rules.assert_not_called()

    def test_direct_schedule_does_not_create_job_for_restricted_sender(self):
        message = self.message("Імпортований діалог від обмеженого відправника")
        settings = InstagramBotSettings.load()
        settings.allowed_senders = "another-sender"
        settings.save(update_fields=["allowed_senders"])

        job = analysis.schedule_analysis(
            self.client,
            message,
            trigger="poll_backfill",
            now=timezone.now(),
        )

        self.assertIsNone(job)
        self.assertFalse(
            IgConversationAnalysisJob.objects.filter(client=self.client).exists()
        )

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    @patch("management.services.bot_conversation_analysis._conversation")
    def test_due_job_is_skipped_when_sender_becomes_restricted_before_provider_io(
        self,
        conversation,
        generate,
    ):
        message = self.message("Підкажіть, будь ласка, розмір")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        settings = InstagramBotSettings.load()
        settings.allowed_senders = "another-sender"
        settings.save(update_fields=["allowed_senders"])
        conversation.return_value = (
            [{"message_id": message.pk, "role": "user", "text": message.text}],
            {
                message.pk: {
                    "message_id": message.pk,
                    "role": "user",
                    "text": message.text,
                },
            },
            [],
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "product_interest",
                "score_band": "exploring",
            },
            "model": "gemini-test",
            "meta": {},
        }

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(
            result,
            {"done": 0, "failed": 0, "skipped": 1, "superseded": 0},
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.SKIPPED)
        self.assertEqual(job.skip_reason, "sender_not_allowed")
        self.assertFalse(IgConversationAnalysisSnapshot.objects.filter(client=self.client).exists())
        conversation.assert_not_called()
        generate.assert_not_called()

    @patch("management.services.ig_payment_review.create_payment_review")
    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_completed_ai_analysis_never_creates_payment_review(self, generate, create_review):
        message = self.message("Я оплатила, чек у вкладенні")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {"interaction_type": "payment_pending", "score_band": "checkout"},
            "model": "gemini-3.6-flash",
            "meta": {},
        }

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["done"], 1)
        create_review.assert_not_called()

    def test_reconciliation_does_not_queue_pre_cutoff_history(self):
        message = self.message("Старий діалог")
        cutoff = timezone.now()
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            created_at=cutoff - timedelta(days=1)
        )
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = cutoff
        settings.analysis_backfill_enabled = False
        settings.save(update_fields=[
            "analysis_reconcile_after", "analysis_backfill_enabled",
        ])

        result = analysis.reconcile_analysis_jobs(now=cutoff)

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["historical_blocked"], 1)
        self.assertFalse(IgConversationAnalysisJob.objects.filter(client=self.client).exists())

    def test_reconciliation_does_not_reopen_recent_failed_job_for_old_history(self):
        message = self.message("Старий діалог з невдалою фоновою спробою")
        cutoff = timezone.now()
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            created_at=cutoff - timedelta(days=30)
        )
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = cutoff
        settings.analysis_backfill_enabled = False
        settings.save(update_fields=[
            "analysis_reconcile_after", "analysis_backfill_enabled",
        ])
        job = IgConversationAnalysisJob.objects.create(
            client=self.client,
            watermark_message_id=message.pk,
            revision=1,
            analyzed_revision=0,
            status=IgConversationAnalysisJob.Status.FAILED,
            due_at=cutoff,
            next_attempt_at=cutoff,
            trigger="reconcile",
            required_state_fingerprint="stale-fingerprint",
            last_error="old quota failure",
        )
        self.assertGreaterEqual(job.created_at, cutoff)

        result = analysis.reconcile_analysis_jobs(now=cutoff)

        job.refresh_from_db()
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["historical_blocked"], 1)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.FAILED)
        self.assertEqual(job.last_error, "old quota failure")

    def test_unrelated_deal_update_does_not_unlock_historical_reconciliation(self):
        message = self.message("Старий діалог")
        cutoff = timezone.now()
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            created_at=cutoff - timedelta(days=1)
        )
        IgDeal.objects.create(client=self.client, amount=Decimal("999.00"))
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = cutoff
        settings.analysis_backfill_enabled = False
        settings.save(update_fields=[
            "analysis_reconcile_after", "analysis_backfill_enabled",
        ])

        result = analysis.reconcile_analysis_jobs(now=cutoff)

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["historical_blocked"], 1)
        self.assertFalse(IgConversationAnalysisJob.objects.filter(client=self.client).exists())

    def test_unrelated_order_update_does_not_unlock_historical_reconciliation(self):
        from orders.models import Order

        message = self.message("Старий діалог")
        order = Order.objects.create(
            full_name="IG fixture",
            phone="0000000000",
            city="Kyiv",
            np_office="1",
            total_sum=Decimal("1000.00"),
        )
        deal = IgDeal.objects.create(
            client=self.client,
            order=order,
            amount=Decimal("1000.00"),
        )
        cutoff = timezone.now()
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            created_at=cutoff - timedelta(days=1)
        )
        IgDeal.objects.filter(pk=deal.pk).update(order_truth_updated_at=None)
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = cutoff
        settings.analysis_backfill_enabled = False
        settings.save(update_fields=[
            "analysis_reconcile_after", "analysis_backfill_enabled",
        ])

        order.manager_comment = "Не впливає на CRM truth"
        order.save()
        result = analysis.reconcile_analysis_jobs(now=timezone.now())

        deal.refresh_from_db()
        self.assertIsNone(deal.order_truth_updated_at)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["historical_blocked"], 1)

    def test_relevant_order_update_fields_unlocks_missed_event_reconciliation(self):
        from orders.models import Order

        message = self.message("Старий діалог")
        order = Order.objects.create(
            full_name="IG fixture",
            phone="0000000000",
            city="Kyiv",
            np_office="1",
            total_sum=Decimal("1000.00"),
        )
        deal = IgDeal.objects.create(
            client=self.client,
            order=order,
            amount=Decimal("1000.00"),
        )
        cutoff = timezone.now()
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            created_at=cutoff - timedelta(days=1)
        )
        IgDeal.objects.filter(pk=deal.pk).update(order_truth_updated_at=None)
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = cutoff
        settings.analysis_backfill_enabled = False
        settings.save(update_fields=[
            "analysis_reconcile_after", "analysis_backfill_enabled",
        ])

        order.status = "ship"
        order.save(update_fields=["status"])
        result = analysis.reconcile_analysis_jobs(now=timezone.now())

        deal.refresh_from_db()
        self.assertGreaterEqual(deal.order_truth_updated_at, cutoff)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["historical_blocked"], 0)

    def test_instagram_linked_order_delete_is_blocked_without_losing_truth(self):
        from orders.models import Order

        message = self.message("Старий діалог")
        order = Order.objects.create(
            full_name="IG fixture",
            phone="0000000000",
            city="Kyiv",
            np_office="1",
            total_sum=Decimal("1000.00"),
        )
        deal = IgDeal.objects.create(
            client=self.client,
            order=order,
            amount=Decimal("1000.00"),
        )
        cutoff = timezone.now()
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            created_at=cutoff - timedelta(days=1)
        )
        IgDeal.objects.filter(pk=deal.pk).update(order_truth_updated_at=None)
        settings = InstagramBotSettings.load()
        settings.analysis_reconcile_after = cutoff
        settings.analysis_backfill_enabled = False
        settings.save(update_fields=[
            "analysis_reconcile_after", "analysis_backfill_enabled",
        ])

        from django.db import transaction

        with self.assertRaisesMessage(ValueError, "не можна видалити"):
            with transaction.atomic():
                order.delete()

        deal.refresh_from_db()
        self.assertEqual(deal.order_id, order.pk)
        self.assertIsNone(deal.order_truth_updated_at)

    def test_shipped_notification_update_fields_advances_order_truth_clock(self):
        from django.utils import timezone as django_timezone

        deal = IgDeal.objects.create(client=self.client, amount=Decimal("100.00"))
        deal.status = IgDeal.Status.QUOTED
        deal.save(update_fields=["status"])
        deal.refresh_from_db()
        self.assertIsNone(deal.order_truth_updated_at)

        notified_at = django_timezone.now()

        deal.shipped_notified_at = notified_at
        deal.save(update_fields=["shipped_notified_at"])

        deal.refresh_from_db()
        self.assertIsNotNone(deal.order_truth_updated_at)
        self.assertGreaterEqual(deal.order_truth_updated_at, notified_at)

    def test_reconciliation_cursor_reaches_clients_after_first_page(self):
        clients = []
        for index in range(3):
            client = IgClient.objects.create(igsid=f"reconcile-page-{index}")
            InstagramBotMessage.objects.create(
                client=client,
                sender_id=client.igsid,
                role=InstagramBotMessage.Role.USER,
                text=f"Повідомлення {index}",
                status=InstagramBotMessage.Status.DONE,
            )
            clients.append(client)

        first = analysis.reconcile_analysis_jobs(limit=2, now=timezone.now())
        second = analysis.reconcile_analysis_jobs(limit=2, now=timezone.now())

        self.assertEqual(first["scanned"], 2)
        self.assertGreater(first["cursor_next"], 0)
        self.assertEqual(second["scanned"], 1)
        self.assertEqual(second["cursor_next"], 0)
        self.assertEqual(
            set(IgConversationAnalysisJob.objects.filter(client__in=clients).values_list("client_id", flat=True)),
            {client.id for client in clients},
        )

    def test_terminal_failure_is_not_reopened_until_required_truth_changes(self):
        message = self.message("Оплачу після уточнення розміру")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        job.status = IgConversationAnalysisJob.Status.FAILED
        job.attempts = analysis.MAX_ATTEMPTS
        job.last_error = "provider exhausted"
        job.save(update_fields=["status", "attempts", "last_error", "updated_at"])
        revision = job.revision
        fingerprint = job.required_state_fingerprint

        first = analysis.reconcile_analysis_jobs(now=timezone.now())
        second = analysis.reconcile_analysis_jobs(now=timezone.now())

        job.refresh_from_db()
        self.assertEqual(first["unchanged"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.FAILED)
        self.assertEqual(job.attempts, analysis.MAX_ATTEMPTS)
        self.assertEqual(job.revision, revision)
        self.assertEqual(job.required_state_fingerprint, fingerprint)

        deal = IgDeal.objects.create(client=self.client, amount=Decimal("1000.00"))
        IgPaymentProjection.objects.create(
            deal=deal,
            client=self.client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("1000.00"),
            paid_at=timezone.now(),
        )
        recovered = analysis.reconcile_analysis_jobs(now=timezone.now())

        job.refresh_from_db()
        self.assertEqual(recovered["queued"], 1)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.revision, revision + 1)
        self.assertNotEqual(job.required_state_fingerprint, fingerprint)

    def test_reconciliation_preserves_pending_retry_backoff_for_same_state(self):
        message = self.message("Підкажіть ціну")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        retry_at = timezone.now() + timedelta(minutes=10)
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        job.attempts = 2
        job.next_attempt_at = retry_at
        job.last_error = "temporary failure"
        job.save(update_fields=[
            "attempts", "next_attempt_at", "last_error", "updated_at",
        ])
        revision = job.revision

        result = analysis.reconcile_analysis_jobs(now=timezone.now())

        job.refresh_from_db()
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(job.revision, revision)
        self.assertEqual(job.attempts, 2)
        self.assertEqual(job.next_attempt_at, retry_at)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_order_truth_change_at_same_watermark_queues_new_revision(self, generate):
        from orders.models import Order

        message = self.message("Оформлюйте замовлення")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "high_intent",
                "score_band": "high_intent",
            },
            "model": "gemini-3.6-flash",
            "meta": {},
        }
        analysis.process_due_analysis(limit=1)
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        revision = job.revision
        fingerprint = job.required_state_fingerprint
        order = Order.objects.create(
            full_name="IG fixture",
            phone="0000000000",
            city="Kyiv",
            np_office="1",
            total_sum=Decimal("1000.00"),
        )
        IgDeal.objects.create(
            client=self.client,
            amount=Decimal("1000.00"),
            order=order,
            status=IgDeal.Status.ORDER_CREATED,
        )

        result = analysis.reconcile_analysis_jobs(now=timezone.now())

        job.refresh_from_db()
        self.assertEqual(result["queued"], 1)
        self.assertEqual(job.revision, revision + 1)
        self.assertNotEqual(job.required_state_fingerprint, fingerprint)

    def test_required_truth_state_exposes_source_qualified_dynamic_amounts(self):
        deal = IgDeal.objects.create(
            client=self.client,
            amount=Decimal("2375.00"),
            pay_type=IgDeal.PayType.PREPAYMENT,
            requested_payment_amount=Decimal("750.00"),
            requested_payment_evidence_ids=[31],
        )
        review = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            deal=deal,
            dedupe_key="analysis-dynamic-amounts",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            evidence={"order_draft": {"quoted_total": "2375.00"}},
        )
        IgPaymentReviewDecision.objects.create(
            review=review,
            client=self.client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            verification_scope=IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
            confirmed_amount=Decimal("700.00"),
            currency="UAH",
            amount_source="manager_input",
            amount_evidence_message_ids=[32],
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id="manager:17",
        )
        IgPaymentProjection.objects.create(
            deal=deal,
            client=self.client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("750.00"),
            paid_at=timezone.now(),
        )

        truth = analysis._required_truth_state(self.client)

        current = truth["current_payment_truth"]
        self.assertEqual(current["order_total"], "2375.00")
        self.assertEqual(current["order_total_source"], "deal_negotiated_total")
        self.assertEqual(current["requested_payment_amount"], "750.00")
        self.assertEqual(current["requested_payment_source"], "deal_payment_request")
        self.assertEqual(current["provider_confirmed_amount"], "750.00")
        self.assertEqual(current["manager_confirmed_amount"], "700.00")
        self.assertEqual(current["manager_amount_source"], "manager_input")
        self.assertEqual(current["confirmed_paid_amount"], "")
        self.assertEqual(current["remaining_amount"], "")
        self.assertTrue(current["needs_reconciliation"])
        self.assertEqual(current["reconciliation_state"], "amount_conflict")

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_gemini_payload_receives_dynamic_payment_truth(self, generate):
        message = self.message("Сплатила 640 грн за посиланням")
        deal = IgDeal.objects.create(
            client=self.client,
            amount=Decimal("1840.00"),
            pay_type=IgDeal.PayType.PREPAYMENT,
            requested_payment_amount=Decimal("640.00"),
            requested_payment_evidence_ids=[message.pk],
        )
        IgPaymentProjection.objects.create(
            deal=deal,
            client=self.client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("640.00"),
            paid_at=timezone.now(),
        )
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "payment_pending",
                "score_band": "checkout",
                "purchase_probability": 0.9,
                "confidence": 0.9,
            },
            "model": "gemini-3.6-flash",
            "meta": {},
        }

        analysis.process_due_analysis(limit=1)

        payload = json.loads(generate.call_args.args[1])
        current = payload["truth_state"]["current_payment_truth"]
        self.assertEqual(current["order_total"], "1840.00")
        self.assertEqual(current["requested_payment_amount"], "640.00")
        self.assertEqual(current["provider_confirmed_amount"], "640.00")
        self.assertEqual(current["manager_confirmed_amount"], "0.00")
        self.assertEqual(current["confirmed_paid_amount"], "640.00")
        self.assertEqual(current["remaining_amount"], "1200.00")
        self.assertEqual(current["reconciliation_state"], "provider_verified")

    def test_manager_amount_change_at_same_watermark_queues_new_revision(self):
        message = self.message("Переказала частину суми")
        deal = IgDeal.objects.create(
            client=self.client,
            amount=Decimal("1900.00"),
            pay_type=IgDeal.PayType.PREPAYMENT,
            requested_payment_amount=Decimal("600.00"),
        )
        review = IgPaymentConfirmationReview.objects.create(
            client=self.client,
            deal=deal,
            dedupe_key="analysis-manager-amount-revision",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        )
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        revision = job.revision
        fingerprint = job.required_state_fingerprint

        IgPaymentReviewDecision.objects.create(
            review=review,
            client=self.client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            verification_scope=IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
            confirmed_amount=Decimal("600.00"),
            currency="UAH",
            amount_source="receipt",
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id="manager:18",
        )

        result = analysis.reconcile_analysis_jobs(now=timezone.now())

        job.refresh_from_db()
        self.assertEqual(result["queued"], 1)
        self.assertEqual(job.revision, revision + 1)
        self.assertNotEqual(job.required_state_fingerprint, fingerprint)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_payment_amount_change_during_gemini_requeues_without_stale_snapshot(
        self, generate
    ):
        message = self.message("Перевірте, будь ласка, платіж")
        deal = IgDeal.objects.create(
            client=self.client,
            amount=Decimal("1750.00"),
            pay_type=IgDeal.PayType.PREPAYMENT,
            requested_payment_amount=Decimal("500.00"),
        )
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )

        def confirm_during_analysis(*_args, **_kwargs):
            IgPaymentProjection.objects.create(
                deal=deal,
                client=self.client,
                truth=IgDeal.PaymentTruth.CONFIRMED,
                gross_amount=Decimal("500.00"),
                paid_at=timezone.now(),
            )
            return {
                "parsed": {
                    "interaction_type": "payment_pending",
                    "score_band": "checkout",
                },
                "model": "gemini-3.6-flash",
                "meta": {},
            }

        generate.side_effect = confirm_during_analysis

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["superseded"], 1)
        self.assertFalse(
            IgConversationAnalysisSnapshot.objects.filter(
                client=self.client,
                analysis_model="gemini-3.6-flash",
            ).exists()
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.trigger, "payment_truth")

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_unconfirmed_repeat_intent_does_not_materialize_a_new_episode(self, generate):
        from management.ig_bot_models import IgCommercialEpisode

        message = self.message("Мені сподобалось, хочу ще одну")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "high_intent",
                "score_band": "high_intent",
                "purchase_probability": 0.86,
                "confidence": 0.92,
                "evidence": [
                    {"message_id": message.pk, "quote": message.text, "claim": "repeat"},
                ],
                "repeat_intent": {
                    "kind": "explicit_more",
                    "confidence": 0.95,
                    "evidence_message_ids": [message.pk],
                },
            },
            "model": "gemini-3.6-flash",
            "meta": {},
        }

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["done"], 1, result)
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_confirmed_repeat_analysis_publishes_intent_without_mutating_operations(
        self, generate
    ):
        from management.ig_bot_models import (
            IgCommerceSelectionSession,
            IgCommercialEpisode,
        )
        from management.services.ig_commerce_projection import authoritative_session_for

        deal = IgDeal.objects.create(client=self.client, amount=Decimal("790.00"))
        IgPaymentProjection.objects.create(
            deal=deal,
            client=self.client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("790.00"),
            paid_at=timezone.now(),
        )
        previous_session = authoritative_session_for(self.client)

        message = self.message("Мені сподобалось, хочу ще одну")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "high_intent",
                "score_band": "high_intent",
                "purchase_probability": 0.86,
                "confidence": 0.92,
                "evidence": [
                    {"message_id": message.pk, "quote": message.text, "claim": "repeat"},
                ],
                "repeat_intent": {
                    "kind": "explicit_more",
                    "confidence": 0.95,
                    "evidence_message_ids": [message.pk],
                },
            },
            "model": "gemini-3.6-flash",
            "meta": {},
        }

        first_result = analysis.process_due_analysis(limit=1)
        first_job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(
            first_result["done"],
            1,
            {**first_result, "last_error": first_job.last_error},
        )
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())
        self.assertEqual(
            IgCommerceSelectionSession.objects.filter(client=self.client).count(),
            1,
        )
        current_session = IgCommerceSelectionSession.objects.get(client=self.client)
        self.assertEqual(current_session.pk, previous_session.pk)
        self.assertEqual(current_session.generation, previous_session.generation)
        previous_session.refresh_from_db()
        self.assertNotEqual(previous_session.state, IgCommerceSelectionSession.State.CLOSED)
        self.assertEqual(previous_session.open_slot, 1)
        snapshot_count = IgConversationAnalysisSnapshot.objects.filter(
            client=self.client
        ).count()

        analysis.schedule_analysis(
            self.client,
            message,
            trigger="retry",
            now=timezone.now() - timedelta(minutes=1),
            delay_seconds=0,
        )
        analysis.process_due_analysis(limit=1)

        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())
        self.assertEqual(
            IgConversationAnalysisSnapshot.objects.filter(client=self.client).count(),
            snapshot_count,
        )

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_repeat_event_consumer_materializes_once_and_replay_is_idempotent(self, generate):
        from management.ig_bot_models import (
            IgCommerceSelectionSession,
            IgCommercialEpisode,
            IgConversationAnalysisEvent,
        )
        from management.services.ig_analysis_events import process_due_analysis_events

        deal = IgDeal.objects.create(client=self.client, amount=Decimal("790.00"))
        IgPaymentProjection.objects.create(
            deal=deal,
            client=self.client,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount=Decimal("790.00"),
            paid_at=timezone.now(),
        )
        previous_session = __import__(
            "management.services.ig_commerce_projection",
            fromlist=["authoritative_session_for"],
        ).authoritative_session_for(self.client)
        message = self.message("Мені сподобалось, хочу ще одну")
        analysis.schedule_analysis(
            self.client, message, now=timezone.now() - timedelta(minutes=1)
        )
        generate.return_value = {
            "parsed": {
                "interaction_type": "high_intent",
                "score_band": "high_intent",
                "purchase_probability": 0.86,
                "confidence": 0.92,
                "evidence": [{"message_id": message.pk, "claim": "repeat"}],
                "repeat_intent": {
                    "kind": "explicit_more",
                    "confidence": 0.95,
                    "evidence_message_ids": [message.pk],
                },
            },
            "model": "gemini-3.6-flash",
            "meta": {},
        }

        analysis.process_due_analysis(limit=1)

        action = IgConversationAnalysisEvent.objects.get(client=self.client)
        self.assertEqual(action.status, IgConversationAnalysisEvent.Status.PENDING)
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

        first = process_due_analysis_events(limit=1)
        action.refresh_from_db()
        self.assertEqual(first["applied"], 1, {**first, "error": action.last_error})
        episode = IgCommercialEpisode.objects.get(client=self.client)
        session = IgCommerceSelectionSession.objects.get(client=self.client, open_slot=1)
        self.assertEqual(session.commercial_episode_id, episode.pk)
        self.assertEqual(session.generation, previous_session.generation + 1)

        second = process_due_analysis_events(limit=1)
        self.assertEqual(sum(second.values()), 0, second)
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.client).count(), 1)
        action.refresh_from_db()
        self.assertEqual(action.status, IgConversationAnalysisEvent.Status.APPLIED)
        self.assertEqual(action.applied_episode_id, episode.pk)
        self.client.refresh_from_db()
        self.assertEqual(self.client.stage, IgClient.Stage.NEW)

    def test_repeat_event_rejects_stale_conversation_and_tampered_evidence(self):
        from management.ig_bot_models import (
            IgCommercialEpisode,
            IgConversationAnalysisEvent,
        )
        from management.services.ig_analysis_events import process_due_analysis_events

        message, event = self.pending_repeat_event()
        self.message("Нове уточнення після аналізу")

        result = process_due_analysis_events(limit=1)

        event.refresh_from_db()
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(event.rejected_reason, "conversation_advanced")
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

        other = IgClient.objects.create(igsid="analysis-event-tampered")
        self.client = other
        message, event = self.pending_repeat_event()
        InstagramBotMessage.objects.filter(pk=message.pk).update(
            text="Хочу лише обміняти розмір"
        )

        result = process_due_analysis_events(limit=1)

        event.refresh_from_db()
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(event.rejected_reason, "repeat_evidence_missing")
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

    def test_same_customer_evidence_with_different_model_kind_materializes_once(self):
        from management.ig_bot_models import (
            IgCommercialEpisode,
            IgConversationAnalysisEvent,
            IgConversationAnalysisSnapshot,
        )
        from management.services.ig_analysis_events import (
            process_due_analysis_events,
            publish_repeat_episode_event,
        )

        message, first_event = self.pending_repeat_event(repeat_kind="explicit_more")
        fingerprint = analysis._required_state_fingerprint(self.client, message.pk)
        second_intent = {
            "kind": "reorder",
            "confidence": "0.9400",
            "evidence_message_ids": [message.pk],
        }
        second_snapshot = IgConversationAnalysisSnapshot.objects.create(
            client=self.client,
            last_analyzed_message=message,
            dedupe_key=f"test-repeat-revision:{self.client.pk}:{message.pk}",
            score_band=IgConversationAnalysisSnapshot.Band.PAID,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.HIGH_INTENT,
            purchase_probability=Decimal("0.8600"),
            confidence=Decimal("0.9200"),
            repeat_intent=second_intent,
            analysis_model="gemini-test-revision",
            analysis_prompt_version=analysis.ANALYSIS_PROMPT_VERSION,
            required_state_fingerprint=fingerprint,
        )
        second_event = publish_repeat_episode_event(
            client=self.client,
            snapshot=second_snapshot,
            repeat_intent=second_intent,
            analysis_model=second_snapshot.analysis_model,
            analysis_prompt_version=second_snapshot.analysis_prompt_version,
            required_state_fingerprint=fingerprint,
        )

        result = process_due_analysis_events(limit=2)

        self.assertEqual(result["applied"], 1, result)
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.client).count(), 1)
        first_event.refresh_from_db()
        second_event.refresh_from_db()
        self.assertEqual(first_event.status, IgConversationAnalysisEvent.Status.APPLIED)
        self.assertEqual(second_event.rejected_reason, "already_materialized")

    def test_repeat_event_failure_rolls_back_and_retry_applies_once(self):
        from management.ig_bot_models import (
            IgCommerceSelectionSession,
            IgCommercialEpisode,
            IgConversationAnalysisEvent,
        )
        from management.services import ig_commercial_episodes
        from management.services.ig_analysis_events import process_due_analysis_events

        _message, event = self.pending_repeat_event()
        previous_session = IgCommerceSelectionSession.objects.create(
            client=self.client,
            generation=1,
            open_slot=1,
        )
        real_start = ig_commercial_episodes.start_repeat_episode

        def fail_after_write(*args, **kwargs):
            real_start(*args, **kwargs)
            raise RuntimeError("forced rollback")

        failed_at = timezone.now()
        with patch(
            "management.services.ig_commercial_episodes.start_repeat_episode",
            side_effect=fail_after_write,
        ):
            failed = process_due_analysis_events(limit=1, now=failed_at)

        event.refresh_from_db()
        previous_session.refresh_from_db()
        self.assertEqual(failed["retry_scheduled"], 1, failed)
        self.assertEqual(failed["failed"], 0, failed)
        self.assertEqual(event.status, IgConversationAnalysisEvent.Status.PENDING)
        self.assertEqual(event.attempts, 1)
        self.assertIn("forced rollback", event.last_error)
        self.assertGreater(event.next_attempt_at, failed_at)
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())
        self.assertEqual(previous_session.open_slot, 1)

        self.assertEqual(
            sum(process_due_analysis_events(limit=1, now=failed_at).values()),
            0,
        )
        applied = process_due_analysis_events(
            limit=1,
            now=event.next_attempt_at + timedelta(seconds=1),
        )

        event.refresh_from_db()
        self.assertEqual(applied["applied"], 1, applied)
        self.assertEqual(event.status, IgConversationAnalysisEvent.Status.APPLIED)
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.client).count(), 1)

    def test_repeat_event_stops_retrying_after_bounded_failures(self):
        from management.ig_bot_models import IgConversationAnalysisEvent
        from management.services.ig_analysis_events import (
            MAX_EVENT_ATTEMPTS,
            process_due_analysis_events,
        )

        _message, event = self.pending_repeat_event()
        now = timezone.now()
        with patch(
            "management.services.ig_commercial_episodes.start_repeat_episode",
            side_effect=RuntimeError("permanent failure"),
        ):
            for _attempt in range(MAX_EVENT_ATTEMPTS):
                result = process_due_analysis_events(limit=1, now=now)
                self.assertEqual(
                    result["failed"],
                    int(_attempt + 1 == MAX_EVENT_ATTEMPTS),
                    result,
                )
                self.assertEqual(
                    result["retry_scheduled"],
                    int(_attempt + 1 < MAX_EVENT_ATTEMPTS),
                    result,
                )
                event.refresh_from_db()
                if _attempt + 1 < MAX_EVENT_ATTEMPTS:
                    now = event.next_attempt_at + timedelta(seconds=1)

        event.refresh_from_db()
        self.assertEqual(event.status, IgConversationAnalysisEvent.Status.FAILED)
        self.assertEqual(event.attempts, MAX_EVENT_ATTEMPTS)
        self.assertIsNone(event.next_attempt_at)
        self.assertEqual(sum(process_due_analysis_events(limit=1, now=now).values()), 0)

    def test_repeat_event_publisher_rejects_confidence_below_operational_threshold(self):
        from management.ig_bot_models import IgConversationAnalysisEvent
        from management.services.ig_analysis_events import publish_repeat_episode_event

        message, snapshot = self.pending_repeat_event(
            confidence="0.6999",
            publish=False,
        )

        with self.assertRaisesRegex(ValueError, "confidence"):
            publish_repeat_episode_event(
                client=self.client,
                snapshot=snapshot,
                repeat_intent=snapshot.repeat_intent,
                analysis_model=snapshot.analysis_model,
                analysis_prompt_version=snapshot.analysis_prompt_version,
                required_state_fingerprint=snapshot.required_state_fingerprint,
            )

        self.assertFalse(
            IgConversationAnalysisEvent.objects.filter(
                snapshot=snapshot,
            ).exists()
        )

    def test_repeat_event_consumer_rejects_untrusted_low_confidence_proposal(self):
        from management.ig_bot_models import (
            IgCommercialEpisode,
            IgConversationAnalysisEvent,
        )
        from management.services.ig_analysis_events import (
            _event_key,
            _snapshot_source_digest,
            process_due_analysis_events,
        )

        message, snapshot = self.pending_repeat_event(
            confidence="0.6999",
            publish=False,
        )
        payload = {
            "repeat_kind": "explicit_more",
            "evidence_message_ids": [message.pk],
            "confidence": "0.6999",
            "analysis_model": snapshot.analysis_model,
            "analysis_prompt_version": snapshot.analysis_prompt_version,
        }
        event = IgConversationAnalysisEvent.objects.create(
            event_key=_event_key(snapshot.dedupe_key, payload),
            client=self.client,
            snapshot=snapshot,
            event_type=IgConversationAnalysisEvent.EventType.REPEAT_EPISODE,
            payload=payload,
            required_state_fingerprint=snapshot.required_state_fingerprint,
            source_digest=_snapshot_source_digest(snapshot),
        )

        result = process_due_analysis_events(limit=1)

        event.refresh_from_db()
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(event.rejected_reason, "invalid_repeat_intent")
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

    def test_repeat_event_rejects_mutated_snapshot_source_before_operational_write(self):
        from management.ig_bot_models import (
            IgCommercialEpisode,
            IgConversationAnalysisSnapshot,
        )
        from management.services.ig_analysis_events import process_due_analysis_events

        _message, event = self.pending_repeat_event()
        IgConversationAnalysisSnapshot.objects.filter(pk=event.snapshot_id).update(
            score_band=IgConversationAnalysisSnapshot.Band.COLD,
        )

        result = process_due_analysis_events(limit=1)

        event.refresh_from_db()
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(event.rejected_reason, "snapshot_digest_mismatch")
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

    def test_analysis_event_identity_cannot_be_mutated_through_queryset_update(self):
        from management.ig_bot_models import IgConversationAnalysisEvent

        _message, event = self.pending_repeat_event()

        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            IgConversationAnalysisEvent.objects.filter(pk=event.pk).update(
                payload={"repeat_kind": "explicit_more"},
            )

    def test_analysis_event_identity_cannot_be_deleted(self):
        from management.ig_bot_models import IgConversationAnalysisEvent

        _message, event = self.pending_repeat_event()

        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            event.delete()
        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            IgConversationAnalysisEvent.objects.filter(pk=event.pk).delete()

        self.assertTrue(IgConversationAnalysisEvent.objects.filter(pk=event.pk).exists())

    def test_repeat_event_rejects_hidden_client_before_operational_write(self):
        from management.ig_bot_models import IgCommercialEpisode
        from management.services.ig_analysis_events import process_due_analysis_events

        _message, event = self.pending_repeat_event()
        IgClient.objects.filter(pk=self.client.pk).update(hidden_at=timezone.now())

        result = process_due_analysis_events(limit=1)

        event.refresh_from_db()
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(event.rejected_reason, "client_hidden")
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

    def test_repeat_event_rejects_blocked_client_before_operational_write(self):
        from management.ig_bot_models import IgCommercialEpisode
        from management.services.ig_analysis_events import process_due_analysis_events

        _message, event = self.pending_repeat_event()
        IgClient.objects.filter(pk=self.client.pk).update(is_blocked=True)

        result = process_due_analysis_events(limit=1)

        event.refresh_from_db()
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(event.rejected_reason, "client_blocked")
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())

    def test_repeat_event_rejects_active_opt_out_before_operational_write(self):
        from management.ig_bot_models import IgCommercialEpisode
        from management.services.ig_analysis_events import process_due_analysis_events

        _message, event = self.pending_repeat_event()
        IgClient.objects.filter(pk=self.client.pk).update(
            opted_out_at=timezone.now(),
            opted_in_at=None,
        )

        result = process_due_analysis_events(limit=1)

        event.refresh_from_db()
        self.assertEqual(result["rejected"], 1, result)
        self.assertEqual(event.rejected_reason, "client_opted_out")
        self.assertFalse(IgCommercialEpisode.objects.filter(client=self.client).exists())


    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_opt_out_during_gemini_is_rechecked_before_snapshot(self, generate):
        message = self.message("Розкажіть про футболку")
        analysis.schedule_analysis(self.client, message, now=timezone.now() - timedelta(minutes=1))

        def opt_out_while_running(*_args, **_kwargs):
            now = timezone.now()
            IgClient.objects.filter(pk=self.client.pk).update(
                opted_out_at=now,
                bot_paused=True,
                paused_reason="opt_out",
            )
            return {
                "parsed": {"score_band": "qualified", "interaction_type": "product_interest"},
                "model": "gemini-3.5-flash",
                "meta": {},
            }

        generate.side_effect = opt_out_while_running

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["skipped"], 1)
        self.assertFalse(
            IgConversationAnalysisSnapshot.objects.filter(
                client=self.client, analysis_model="gemini-3.5-flash"
            ).exists()
        )
        self.assertEqual(IgConversationAnalysisJob.objects.get(client=self.client).skip_reason, "opt_out")

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_payment_confirmed_during_gemini_discards_stale_model_result(self, generate):
        message = self.message("Оплачу пізніше")
        deal = IgDeal.objects.create(client=self.client, amount=Decimal("1000.00"))
        analysis.schedule_analysis(self.client, message, now=timezone.now() - timedelta(minutes=1))

        def pay_while_running(*_args, **_kwargs):
            IgPaymentProjection.objects.create(
                deal=deal,
                client=self.client,
                truth=IgDeal.PaymentTruth.CONFIRMED,
                gross_amount=Decimal("1000.00"),
                paid_at=timezone.now(),
            )
            return {
                "parsed": {
                    "score_band": "exploring",
                    "interaction_type": "information_only",
                    "purchase_probability": 0.2,
                    "confidence": 0.8,
                },
                "model": "gemini-3.5-flash",
                "meta": {},
            }

        generate.side_effect = pay_while_running

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["superseded"], 1)
        self.assertFalse(
            IgConversationAnalysisSnapshot.objects.filter(
                client=self.client,
                analysis_model="gemini-3.5-flash",
            ).exists()
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        current_fingerprint = analysis._required_state_fingerprint(
            self.client,
            message.pk,
        )
        self.assertEqual(job.required_state_fingerprint, current_fingerprint)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.trigger, "payment_truth")

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_order_truth_change_during_gemini_requeues_without_stale_snapshot(self, generate):
        from orders.models import Order

        message = self.message("Коли відправите замовлення?")
        deal = IgDeal.objects.create(client=self.client, amount=Decimal("1000.00"))
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        original_revision = IgConversationAnalysisJob.objects.get(
            client=self.client
        ).revision

        def materialize_order_without_scheduler(*_args, **_kwargs):
            order = Order.objects.create(
                full_name="IG fixture",
                phone="0000000000",
                city="Kyiv",
                np_office="1",
                total_sum=Decimal("1000.00"),
                status="new",
                payment_status="paid",
            )
            IgDeal.objects.filter(pk=deal.pk).update(
                order=order,
                status=IgDeal.Status.ORDER_CREATED,
            )
            return {
                "parsed": {
                    "score_band": "high_intent",
                    "interaction_type": "high_intent",
                },
                "model": "gemini-3.6-flash",
                "meta": {},
            }

        generate.side_effect = materialize_order_without_scheduler

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["superseded"], 1)
        self.assertFalse(
            IgConversationAnalysisSnapshot.objects.exclude(
                analysis_model="rules"
            ).filter(client=self.client).exists()
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.revision, original_revision + 1)
        self.assertEqual(job.trigger, "order_truth")
        self.assertEqual(
            job.required_state_fingerprint,
            analysis._required_state_fingerprint(self.client, message.pk),
        )

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_reclaimed_worker_is_only_owner_allowed_to_publish_snapshot(self, generate):
        started_at = timezone.now()
        message = self.message("Потрібна чорна футболка M")
        analysis.schedule_analysis(
            self.client,
            message,
            now=started_at - timedelta(minutes=1),
        )
        replacement = {}

        def reclaim_during_gemini(*_args, **_kwargs):
            job = IgConversationAnalysisJob.objects.get(client=self.client)
            IgConversationAnalysisJob.objects.filter(pk=job.pk).update(
                lease_until=started_at - timedelta(seconds=1)
            )
            analysis._reclaim_stale(started_at)
            replacement["claim"] = analysis._claim_due(started_at)
            return {
                "parsed": {
                    "score_band": "qualified",
                    "interaction_type": "product_interest",
                    "purchase_probability": 0.7,
                    "confidence": 0.8,
                },
                "model": "gemini-3.6-flash",
                "meta": {"reasoning_level": "high"},
            }

        generate.side_effect = reclaim_during_gemini

        result = analysis.process_due_analysis(limit=1, now=started_at)

        self.assertEqual(result["superseded"], 1)
        self.assertFalse(
            IgConversationAnalysisSnapshot.objects.filter(client=self.client).exists()
        )
        replacement_job, watermark, revision, replacement_token = replacement["claim"]
        current = IgConversationAnalysisJob.objects.get(pk=replacement_job.pk)
        self.assertEqual(current.status, IgConversationAnalysisJob.Status.PROCESSING)
        self.assertEqual(current.lease_token, replacement_token)

        generate.side_effect = None
        generate.return_value = {
            "parsed": {
                "score_band": "qualified",
                "interaction_type": "product_interest",
                "purchase_probability": 0.7,
                "confidence": 0.8,
            },
            "model": "gemini-3.6-flash",
            "meta": {"reasoning_level": "high"},
        }
        self.assertEqual(
            analysis._process_claim(
                replacement_job,
                watermark,
                revision,
                replacement_token,
                started_at,
            ),
            "done",
        )
        self.assertEqual(
            IgConversationAnalysisSnapshot.objects.filter(client=self.client).count(),
            1,
        )

    def test_expired_skip_owner_cannot_finalize_job(self):
        message = self.message("🔥")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            message,
            now=now - timedelta(minutes=1),
        )
        job, watermark, revision, token = analysis._claim_due(now)
        IgConversationAnalysisJob.objects.filter(pk=job.pk).update(
            lease_until=now - timedelta(seconds=1)
        )

        outcome = analysis._finish_skip(
            job.pk,
            token,
            watermark,
            revision,
            "reaction_only",
            now,
        )

        job.refresh_from_db()
        self.assertEqual(outcome, "superseded")
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PROCESSING)
        self.assertEqual(job.lease_token, token)
        self.assertEqual(job.analyzed_revision, 0)
        self.assertEqual(job.skip_reason, "")

    def test_expired_failure_owner_cannot_change_retry_state(self):
        message = self.message("Підкажіть ціну")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            message,
            now=now - timedelta(minutes=1),
        )
        job, watermark, revision, token = analysis._claim_due(now)
        IgConversationAnalysisJob.objects.filter(pk=job.pk).update(
            lease_until=now - timedelta(seconds=1)
        )

        analysis._finish_failure(
            job,
            token,
            watermark,
            revision,
            RuntimeError("late provider failure"),
            now,
        )

        job.refresh_from_db()
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PROCESSING)
        self.assertEqual(job.lease_token, token)
        self.assertEqual(job.last_error, "")
        self.assertEqual(job.attempts, 1)

    def test_live_skip_owner_releases_superseded_revision_without_analyzing_it(self):
        first = self.message("🔥")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            first,
            now=now - timedelta(minutes=1),
        )
        job, watermark, revision, token = analysis._claim_due(now)
        second = self.message("Потрібен розмір M")
        analysis.schedule_analysis(
            self.client,
            second,
            now=now - timedelta(minutes=1),
        )

        outcome = analysis._finish_skip(
            job.pk,
            token,
            watermark,
            revision,
            "reaction_only",
            now,
        )

        job.refresh_from_db()
        self.assertEqual(outcome, "superseded")
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.watermark_message_id, second.pk)
        self.assertEqual(job.analyzed_revision, 0)
        self.assertEqual(job.lease_token, "")

    def test_stale_reclaim_resets_attempts_for_revision_created_during_old_claim(self):
        first = self.message("Підкажіть ціну")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            first,
            now=now - timedelta(minutes=1),
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        job.attempts = analysis.MAX_ATTEMPTS - 1
        job.save(update_fields=["attempts", "updated_at"])
        claimed, _watermark, claimed_revision, _token = analysis._claim_due(now)
        self.assertEqual(claimed.attempts, analysis.MAX_ATTEMPTS)
        self.assertEqual(claimed.claimed_revision, claimed_revision)

        second = self.message("І ще потрібен розмір M")
        analysis.schedule_analysis(
            self.client,
            second,
            now=now - timedelta(minutes=1),
        )
        IgConversationAnalysisJob.objects.filter(pk=job.pk).update(
            lease_until=now - timedelta(seconds=1)
        )

        self.assertEqual(analysis._reclaim_stale(now), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.watermark_message_id, second.pk)
        self.assertGreater(job.revision, claimed_revision)
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.claimed_watermark_message_id, 0)
        self.assertEqual(job.claimed_revision, 0)

    def test_stale_reclaim_terminalizes_unchanged_fifth_attempt(self):
        message = self.message("Підкажіть ціну")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            message,
            now=now - timedelta(minutes=1),
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        job.attempts = analysis.MAX_ATTEMPTS - 1
        job.save(update_fields=["attempts", "updated_at"])
        claimed, watermark, revision, token = analysis._claim_due(now)
        self.assertEqual(claimed.attempts, analysis.MAX_ATTEMPTS)
        IgConversationAnalysisJob.objects.filter(pk=job.pk).update(
            lease_until=now - timedelta(seconds=1)
        )

        self.assertEqual(analysis._reclaim_stale(now), 1)

        job.refresh_from_db()
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.FAILED)
        self.assertEqual(job.attempts, analysis.MAX_ATTEMPTS)
        self.assertEqual(job.watermark_message_id, watermark)
        self.assertEqual(job.revision, revision)
        self.assertEqual(job.lease_token, "")
        self.assertIsNone(job.lease_until)
        self.assertEqual(job.claimed_watermark_message_id, 0)
        self.assertEqual(job.claimed_revision, 0)
        self.assertEqual(job.last_error, "stale_lease_retry_exhausted")
        self.assertIsNone(analysis._claim_due(now + timedelta(minutes=1)))

    def test_pending_job_at_retry_cap_is_not_claimable(self):
        message = self.message("Потрібен розмір M")
        now = timezone.now()
        analysis.schedule_analysis(
            self.client,
            message,
            now=now - timedelta(minutes=1),
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        job.attempts = analysis.MAX_ATTEMPTS
        job.save(update_fields=["attempts", "updated_at"])

        self.assertIsNone(analysis._claim_due(now))

        job.refresh_from_db()
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.attempts, analysis.MAX_ATTEMPTS)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_new_message_during_gemini_supersedes_old_transcript(self, generate):
        first = self.message("Цікавить чорна футболка")
        analysis.schedule_analysis(
            self.client,
            first,
            now=timezone.now() - timedelta(minutes=1),
        )
        created = {}

        def enqueue_new_revision(*_args, **_kwargs):
            second = self.message("Потрібен розмір M")
            created["message"] = second
            analysis.schedule_analysis(
                self.client,
                second,
                now=timezone.now() - timedelta(minutes=1),
            )
            return {
                "parsed": {
                    "interaction_type": "product_interest",
                    "score_band": "qualified",
                },
                "model": "gemini-3.6-flash",
                "meta": {},
            }

        generate.side_effect = enqueue_new_revision
        before = self.operational_digest()

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["superseded"], 1)
        self.assertEqual(self.operational_digest(), before)
        self.assertFalse(
            IgConversationAnalysisSnapshot.objects.exclude(
                analysis_model="rules"
            ).filter(client=self.client).exists()
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.PENDING)
        self.assertEqual(job.watermark_message_id, created["message"].pk)
        self.assertEqual(job.lease_token, "")
        self.assertEqual(job.attempts, 0)

    @patch("management.services.bot_conversation_analysis.gemini_generate_json")
    def test_snapshot_and_job_completion_have_no_crash_gap(self, generate):
        message = self.message("Хочу замовити чорну футболку")
        analysis.schedule_analysis(
            self.client,
            message,
            now=timezone.now() - timedelta(minutes=1),
        )
        generate.return_value = {
            "parsed": {
                "score_band": "high_intent",
                "interaction_type": "high_intent",
                "purchase_probability": 0.85,
                "confidence": 0.9,
            },
            "model": "gemini-3.6-flash",
            "meta": {"reasoning_level": "high"},
        }

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["done"], 1)
        self.assertTrue(
            IgConversationAnalysisSnapshot.objects.filter(client=self.client).exists()
        )
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.status, IgConversationAnalysisJob.Status.DONE)
        self.assertEqual(job.lease_token, "")


class ConversationAnalysisCommandTests(SimpleTestCase):
    @patch(
        "management.management.commands.reconcile_ig_analysis_jobs.process_due_analysis_events"
    )
    @patch(
        "management.management.commands.reconcile_ig_analysis_jobs.process_due_analysis",
        return_value={"done": 1},
    )
    @patch(
        "management.management.commands.reconcile_ig_analysis_jobs.reconcile_analysis_jobs",
        return_value={"queued": 1},
    )
    def test_run_due_defers_daemon_owned_consumers(
        self, reconcile, process_analysis, process_events
    ):
        stdout = StringIO()

        call_command("reconcile_ig_analysis_jobs", "--run-due", stdout=stdout)

        reconcile.assert_not_called()
        process_analysis.assert_not_called()
        process_events.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["processed"], {"deferred": "daemon_owner"})
        self.assertEqual(payload["processed_events"], {"deferred": "daemon_owner"})


class ConversationAnalysisFailureDryRunTests(TestCase):
    def test_dry_run_reports_failed_jobs_and_never_mutates_them(self):
        exhausted_client = IgClient.objects.create(igsid="failed-report-exhausted")
        retry_client = IgClient.objects.create(igsid="failed-report-retry")
        exhausted = IgConversationAnalysisJob.objects.create(
            client=exhausted_client,
            status=IgConversationAnalysisJob.Status.FAILED,
            attempts=5,
            last_error="provider timeout",
        )
        retryable = IgConversationAnalysisJob.objects.create(
            client=retry_client,
            status=IgConversationAnalysisJob.Status.FAILED,
            attempts=2,
            last_error="quota response",
        )
        before = list(
            IgConversationAnalysisJob.objects.order_by("id").values_list(
                "id", "status", "attempts", "last_error", "updated_at"
            )
        )
        stdout = StringIO()

        call_command(
            "reconcile_ig_analysis_jobs",
            "--dry-run",
            "--quota-budget",
            "1",
            stdout=stdout,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["failed"], 2)
        self.assertEqual(payload["quota_budget"], 1)
        self.assertEqual(payload["retry_ids"], [exhausted.pk])
        self.assertEqual(
            {row["id"] for row in payload["jobs"]},
            {exhausted.pk, retryable.pk},
        )
        retry_row = next(row for row in payload["jobs"] if row["id"] == retryable.pk)
        self.assertEqual(retry_row["reason"], "quota response")
        self.assertEqual(retry_row["last_error"], "quota response")
        self.assertEqual(retry_row["attempts"], 2)
        self.assertEqual(retry_row["disposition"], "retry_candidate")
        exhausted_row = next(row for row in payload["jobs"] if row["id"] == exhausted.pk)
        self.assertEqual(exhausted_row["disposition"], "attempts_exhausted")
        self.assertTrue(exhausted_row["selected_for_retry"])
        after = list(
            IgConversationAnalysisJob.objects.order_by("id").values_list(
                "id", "status", "attempts", "last_error", "updated_at"
            )
        )
        self.assertEqual(after, before)

    def test_dry_run_cannot_be_combined_with_live_processing(self):
        with self.assertRaises(CommandError):
            call_command(
                "reconcile_ig_analysis_jobs",
                "--dry-run",
                "--run-due",
                stdout=StringIO(),
            )

    def test_quota_budget_cannot_select_mutating_reconcile_path(self):
        with self.assertRaises(CommandError):
            call_command(
                "reconcile_ig_analysis_jobs",
                "--quota-budget",
                "1",
                stdout=StringIO(),
            )
