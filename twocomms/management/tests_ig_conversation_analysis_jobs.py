import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import call, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from management.models import (
    IgClient,
    IgConversationAnalysisJob,
    IgConversationAnalysisSnapshot,
    IgDeal,
    IgFollowUpTask,
    IgPaymentProjection,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services import bot_conversation_analysis as analysis


class ConversationAnalysisLeasePolicyTests(SimpleTestCase):
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
        "management.services.bot_conversation_analysis._process_claim",
        side_effect=["done", RuntimeError("provider failed")],
    )
    @patch("management.services.bot_conversation_analysis._reclaim_stale")
    @patch("management.services.bot_conversation_analysis._claim_due")
    @patch("management.services.bot_conversation_analysis.timezone.now")
    def test_batch_uses_fresh_clock_for_each_claim_and_failure(
        self, now_mock, claim_due, reclaim, process, finish_failure
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


class ConversationAnalysisJobTests(TestCase):
    def setUp(self):
        self.client = IgClient.objects.create(igsid="analysis-job-client")

    def message(self, text, *, role=InstagramBotMessage.Role.USER):
        return InstagramBotMessage.objects.create(
            client=self.client,
            sender_id=self.client.igsid,
            role=role,
            text=text,
            status=InstagramBotMessage.Status.DONE,
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
    def test_paused_client_is_analyzed_but_hidden_client_is_skipped(self, generate):
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
        analysis.schedule_analysis(self.client, hidden_message, now=timezone.now() - timedelta(minutes=1))
        generate.reset_mock()

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["skipped"], 1)
        generate.assert_not_called()
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.skip_reason, "hidden")

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

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["skipped"], 1)
        generate.assert_not_called()

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

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["failed"], 1)
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

    def test_reconciliation_queues_changed_conversation_without_gemini(self):
        message = self.message("Новий діалог")

        with patch("management.services.bot_conversation_analysis.gemini_generate_json") as generate:
            result = analysis.reconcile_analysis_jobs(now=timezone.now())

        self.assertEqual(result["queued"], 1)
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        self.assertEqual(job.watermark_message_id, message.id)
        self.assertEqual(job.trigger, "reconcile")
        generate.assert_not_called()

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
    def test_payment_confirmed_during_gemini_overrides_model_intent(self, generate):
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

        analysis.process_due_analysis(limit=1)

        snapshot = IgConversationAnalysisSnapshot.objects.filter(
            client=self.client, analysis_model="gemini-3.5-flash"
        ).get()
        self.assertEqual(snapshot.score_band, IgConversationAnalysisSnapshot.Band.PAID)
        self.assertEqual(snapshot.purchase_probability, Decimal("1.0000"))
        job = IgConversationAnalysisJob.objects.get(client=self.client)
        current_fingerprint = analysis._required_state_fingerprint(
            self.client,
            message.pk,
        )
        self.assertEqual(snapshot.required_state_fingerprint, current_fingerprint)
        self.assertEqual(job.required_state_fingerprint, current_fingerprint)
        self.assertEqual(analysis.reconcile_analysis_jobs()["unchanged"], 1)

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

        result = analysis.process_due_analysis(limit=1)

        self.assertEqual(result["superseded"], 1)
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
