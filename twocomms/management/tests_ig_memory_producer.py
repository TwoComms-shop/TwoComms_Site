"""Offline captured-memory contract; each test owns an isolated database."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch
from unittest import skipUnless

from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django.db import connection

from management.models import IgClient, IgCommercialEpisode, IgFunnelResetAudit, InstagramBotMessage
from management.services import bot_memory
from management.services import ig_memory_producer as producer
from management.services.ig_analysis_lane import owner_scope


@override_settings(GOOGLE_INDEXING_ENABLED=False, GEMINI_ACCOUNTING_V2_MODE="shadow",
    GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2020-01-01T08:00:00+00:00", GEMINI_NONLIVE_ADMISSION_MODE="enforce")
class CapturedMemoryProducerTests(TransactionTestCase):
    def setUp(self):
        from management.models import InstagramBotSettings
        InstagramBotSettings.objects.create(pk=1, is_enabled=True, allowed_senders="")
        self.client = IgClient.objects.create(igsid=f"memory-{self._testMethodName}"[:64])
        self.clock = timezone.now()
        self.owner_context = owner_scope(now=self.clock, lease_seconds=3600)
        self.owner = self.owner_context.__enter__()
        self.addCleanup(self.owner_context.__exit__, None, None, None)
        self.ordinal = 0

    def source(self, text="customer preference", *, at=None, role="user", source="webhook"):
        self.ordinal += 1
        return InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            role=role, source=source, text=text, mid=f"memory-{self._testMethodName}-{self.ordinal}"[:255],
            provider_namespace="instagram_login:memory-owner", provider_created_at=at or self.clock + timedelta(microseconds=self.ordinal),
            status="done")

    def claim(self):
        return producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))

    def head(self, summary="safe captured narrative"):
        source = self.source()
        self.assertTrue(producer.enqueue_memory_source(source.pk, now=self.clock).queued)
        claim = self.claim()
        self.assertIsNotNone(claim)
        result = producer.publish_memory_result(claim, summary, now=self.clock + timedelta(seconds=5))
        self.assertTrue(result.published, result.reason)
        return source, claim

    def test_twenty_events_coalesce_to_one_generation_and_receipt_replay_is_noop(self):
        for index in range(20):
            source = self.source(f"preference {index}")
            self.assertTrue(producer.enqueue_memory_source(source.pk, now=self.clock).queued)
        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True):
            generated = Mock(return_value={"parsed": "one captured summary"})
            result = producer.process_due_memory(limit=10, generate=generated, now=self.clock + timedelta(seconds=4))
            self.assertEqual(result["published"], 1, result)
            self.assertEqual(generated.call_count, 1)
            self.assertEqual(len(generated.call_args.args[0].capture["sources"]), 20)
            self.assertFalse(producer.enqueue_memory_source(source.pk).queued)
            self.assertEqual(producer.process_due_memory(generate=generated)["claimed"], 0)
            self.assertEqual(generated.call_count, 1)

    def test_seven_to_nine_events_do_not_depend_on_count_modulo(self):
        for index in range(7):
            source = self.source(str(index))
            producer.enqueue_memory_source(source.pk, now=self.clock)
        first = self.claim()
        self.assertEqual(len(first.capture["sources"]), 7)
        for index in range(2):
            source = self.source(f"new {index}")
            producer.enqueue_memory_source(source.pk, now=self.clock + timedelta(seconds=5))
        self.assertIsNone(producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=9)))
        self.assertEqual(producer.publish_memory_result(first, "old", now=self.clock + timedelta(seconds=6)).reason, "source_watermark_advanced")
        second = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=10))
        self.assertEqual(len(second.capture["sources"]), 9)
        self.assertTrue(producer.publish_memory_result(second, "current", now=self.clock + timedelta(seconds=11)).published)

    def test_expired_claim_reverse_completion_cannot_overwrite_new_head(self):
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        first = self.claim()
        second = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=45))
        self.assertIsNotNone(second)
        self.assertNotEqual(first.token, second.token)
        self.assertTrue(producer.publish_memory_result(second, "newer", now=self.clock + timedelta(seconds=46)).published)
        self.assertEqual(producer.publish_memory_result(first, "late old", now=self.clock + timedelta(seconds=47)).reason, "claim_replaced")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_summary, "newer")
        self.assertEqual(self.client.memory_version, 1)

    def test_reset_episode_line_recipient_and_erasure_fence_captured_work(self):
        from management.models import IgCommerceSelectionSession
        for boundary in ("reset", "episode", "recipient", "erasure"):
            with self.subTest(boundary=boundary):
                self.client = IgClient.objects.create(igsid=f"memory-scope-{boundary}")
                source = self.source()
                producer.enqueue_memory_source(source.pk, now=self.clock)
                claim = self.claim()
                if boundary == "reset":
                    IgFunnelResetAudit.objects.create(client=self.client, reset_after_message_id=source.pk, reason="memory reset")
                elif boundary == "episode":
                    episode = IgCommercialEpisode.objects.create(client=self.client, sequence=1, materialization_key="memory-episode")
                    self.client.current_commercial_episode = episode
                    self.client.save(update_fields=["current_commercial_episode"])
                elif boundary == "recipient":
                    IgCommerceSelectionSession.objects.create(client=self.client, generation=1, open_slot=1,
                        lines=[{"line_id": "new-line", "recipient_id": "friend"}])
                else:
                    self.client.privacy_erasure_started_at = timezone.now()
                    self.client.save(update_fields=["privacy_erasure_started_at"])
                result = producer.publish_memory_result(claim, "must not publish", now=self.clock + timedelta(seconds=5))
                self.assertFalse(result.published)
                self.assertEqual(result.reason, "client_erasing" if boundary == "erasure" else "scope_changed")
                self.client.refresh_from_db()
                self.assertFalse(self.client.memory_summary)

    def test_sources_freshness_is_separate_from_generated_timestamp(self):
        source, claim = self.head()
        self.client.refresh_from_db()
        read = producer.read_memory_summary(self.client)
        self.assertEqual(read.reason, "current")
        self.assertNotEqual(read.provenance["generated_at"], read.provenance["capture"]["target"]["event_at"])
        changed = self.source("new correction")
        producer.enqueue_memory_source(changed.pk, now=self.clock + timedelta(seconds=6))
        self.assertEqual(producer.read_memory_summary(self.client).reason, "narrative_source_stale")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_summary, "safe captured narrative")

    def test_failure_preserves_head_and_dirty_with_bounded_backoff(self):
        self.head()
        source = self.source("correction")
        producer.enqueue_memory_source(source.pk, now=self.clock + timedelta(seconds=6))
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=10))
        producer.fail_memory_claim(claim, now=self.clock + timedelta(seconds=11))
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_summary, "safe captured narrative")
        self.assertIsNotNone(self.client.memory_dirty_at)
        self.assertGreater(self.client.memory_due_at, self.clock + timedelta(seconds=11))
        self.assertLessEqual((self.client.memory_due_at - self.clock).total_seconds(), 911)
        self.assertEqual(producer.read_memory_summary(self.client).reason, "narrative_source_stale")

    def test_unsafe_old_narrative_is_omitted_with_reason_without_backfill_generation(self):
        self.client.memory_summary = "old unsupported size/payment claims"
        self.client.memory_updated_at = timezone.now()
        self.client.save(update_fields=["memory_summary", "memory_updated_at"])
        with patch("management.services.bot_memory.gemini_generate_text") as generate:
            self.assertIsNone(bot_memory.memory_note(self.client))
            self.assertEqual(producer.read_memory_summary(self.client).reason, "narrative_provenance_missing")
        generate.assert_not_called()

    def test_event_time_and_id_tie_preserve_original_order_ignore_import_rewind(self):
        first = self.source("first", at=self.clock)
        second = self.source("second", at=self.clock)
        producer.enqueue_memory_source(first.pk, now=self.clock)
        producer.enqueue_memory_source(second.pk, now=self.clock)
        imported = self.source("old imported size XL", at=self.clock - timedelta(days=1), source="poll_history")
        self.assertFalse(producer.enqueue_memory_source(imported.pk).queued)
        late = self.source("late webhook old size XL", at=self.clock - timedelta(minutes=5))
        self.assertFalse(producer.enqueue_memory_source(late.pk).queued)
        claim = self.claim()
        self.assertEqual([row["message_id"] for row in claim.capture["sources"]][-2:], [first.pk, second.pk])
        self.assertNotIn("old imported", claim.capture["transcript"])
        self.assertEqual(claim.capture["target"]["message_id"], second.pk)

    def test_manager_pause_and_no_reply_sources_are_readable_without_send_authority(self):
        self.client.bot_paused = True
        self.client.manager_takeover = True
        self.client.save(update_fields=["bot_paused", "manager_takeover"])
        user = self.source("Дякую. Розмір L")
        producer.enqueue_memory_source(user.pk, now=self.clock)
        manager = self.source("manager private note", role="manager", source="echo")
        producer.enqueue_memory_source(manager.pk, now=self.clock)
        claim = self.claim()
        self.assertIn("manager", claim.capture["transcript"])
        self.assertTrue(producer.publish_memory_result(claim, "untrusted manager/customer narrative", now=self.clock + timedelta(seconds=5)).published)
        note = bot_memory.memory_note(self.client)
        self.assertIn("<records>", note)
        self.client.refresh_from_db()
        self.assertTrue(self.client.bot_paused)
        self.assertTrue(self.client.manager_takeover)

    def test_generation_off_or_denied_never_calls_callback_or_provider(self):
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        callback = Mock(return_value="summary")
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            self.assertEqual(producer.process_due_memory(generate=callback)["reason"], "generation_disabled")
            callback.assert_not_called()
            provider.assert_not_called()
            with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True):
                result = producer.process_due_memory(generate=callback, admission=lambda claim: "denied",
                    now=self.clock + timedelta(seconds=4))
                self.assertEqual(result["discarded"], 1)
                callback.assert_not_called()
                provider.assert_not_called()

    def test_accepted_boolean_flags_cannot_bypass_actual_accounting_enforcement(self):
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        callback = Mock(return_value="must not generate")
        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True), patch("management.services.call_ai_analysis.gemini_generate_text") as facade:
            for accounting_mode, admission_mode in (("off", "enforce"), ("shadow", "shadow"), ("shadow", "invalid")):
                with self.subTest(accounting_mode=accounting_mode, admission_mode=admission_mode), self.settings(
                    GEMINI_ACCOUNTING_V2_MODE=accounting_mode, GEMINI_NONLIVE_ADMISSION_MODE=admission_mode):
                    result = producer.process_due_memory(generate=callback, now=self.clock + timedelta(seconds=4))
                    self.assertEqual(result["reason"], "admission_not_enforced")
                    self.assertEqual(result["claimed"], 0)
                    callback.assert_not_called()
                    facade.assert_not_called()
                    self.client.refresh_from_db()
                    self.assertTrue(self.client.memory_dirty_at)
                    self.assertFalse(self.client.memory_claim_token)
            claim = self.claim()
            with self.settings(GEMINI_NONLIVE_ADMISSION_MODE="shadow"):
                self.assertEqual(producer.current_claim_admission(claim), "admission_not_enforced")

    def test_default_facade_rechecks_dynamic_callback_and_flags_at_each_dispatch(self):
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        extra_admission = Mock(side_effect=lambda claim: True if extra_admission.call_count == 1 else "analysis_priority")

        def provider(payload, **kwargs):
            self.assertEqual(kwargs["role"], "management")
            self.assertEqual(kwargs["reasoning_task"], "memory_summary")
            self.assertGreater(kwargs["deadline_seconds"], 0)
            self.assertLessEqual(kwargs["deadline_seconds"], 35)
            self.assertEqual(kwargs["pre_dispatch_guard"](), "analysis_priority")
            with self.settings(IG_MEMORY_GENERATION_ENABLED=False):
                self.assertEqual(kwargs["pre_dispatch_guard"](), "generation_disabled")
            raise RuntimeError("denied before physical HTTP")

        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True), patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=provider) as facade:
            result = producer.process_due_memory(admission=extra_admission, now=self.clock + timedelta(seconds=4))
        self.assertEqual(result["discarded"], 1)
        self.assertEqual(facade.call_count, 1)
        self.assertEqual(extra_admission.call_count, 3)
        self.client.refresh_from_db()
        self.assertFalse(self.client.memory_summary)

    def test_final_guard_does_not_create_or_lock_lane_state(self):
        from management.models import IgWorkerLaneState
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = self.claim()
        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True), patch("management.services.ig_analysis_lane._row", side_effect=AssertionError("final guard must not initialize/lock lane")):
            self.assertIs(producer.current_claim_admission(claim), True)
            IgWorkerLaneState.objects.all().delete()
            self.assertEqual(producer.current_claim_admission(claim), "lane_owner_changed")
            self.assertFalse(IgWorkerLaneState.objects.exists())

    def test_final_guard_never_creates_missing_configuration(self):
        from management.models import InstagramBotSettings
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = self.claim()
        InstagramBotSettings.objects.all().delete()
        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True), patch.object(InstagramBotSettings, "load", side_effect=AssertionError("final guard must not load/create settings")):
            self.assertEqual(producer.current_claim_admission(claim), "customer_reply_priority")
            self.assertFalse(InstagramBotSettings.objects.exists())

    def test_final_dispatch_guard_rechecks_source_owner_priority_and_maintenance(self):
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = self.claim()
        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True):
            self.assertIs(producer.current_claim_admission(claim, now=self.clock + timedelta(seconds=5)), True)
            with patch("management.services.ig_maintenance.maintenance_status", return_value={"active": True}):
                self.assertEqual(producer.current_claim_admission(claim), "maintenance_active")
            with patch("management.services.ig_db_circuit.circuit_status", return_value={"open": True}):
                self.assertEqual(producer.current_claim_admission(claim), "database_circuit_open")
            pending = self.source("needs reply")
            pending.status = "pending"
            pending.save(update_fields=["status"])
            self.assertEqual(producer.current_claim_admission(claim), "customer_reply_priority")
            pending.status = "done"
            pending.save(update_fields=["status"])
            self.assertEqual(producer.current_claim_admission(claim), "source_watermark_advanced")

    def test_changed_original_or_head_or_caller_capture_is_rejected(self):
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = self.claim()
        forged = deepcopy(claim.capture)
        forged["transcript"] = "forged"
        self.assertEqual(producer.publish_memory_result(replace(claim, capture=forged), "bad", now=self.clock + timedelta(seconds=5)).reason, "claim_replaced")
        source.text = "changed source"
        source.save(update_fields=["text"])
        self.assertEqual(producer.publish_memory_result(claim, "bad", now=self.clock + timedelta(seconds=5)).reason, "source_changed")

    def test_canonical_size_outside_sixty_and_new_correction_keep_original_proofs(self):
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        first = self.source("Розмір L")
        apply_turn(self.client, first, parse_turn(first.text), reply_payload={})
        producer.enqueue_memory_source(first.pk, now=self.clock)
        for index in range(65):
            row = self.source(f"neutral source {index}")
            producer.enqueue_memory_source(row.pk, now=self.clock)
        claim = self.claim()
        self.assertEqual(len(claim.capture["sources"]), 60)
        self.assertNotIn(first.pk, [row["message_id"] for row in claim.capture["sources"]])
        selection = claim.capture["canonical_selection"]
        self.assertEqual(selection["values"]["size"], "L")
        self.assertEqual(selection["evidence"]["size"]["source_message_id"], first.pk)
        correction = self.source("не L, а XL")
        apply_turn(self.client, correction, parse_turn(correction.text), reply_payload={})
        producer.enqueue_memory_source(correction.pk, now=self.clock + timedelta(seconds=5))
        self.assertEqual(producer.publish_memory_result(claim, "old L narrative", now=self.clock + timedelta(seconds=6)).reason, "source_watermark_advanced")
        current = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=10))
        self.assertEqual(current.capture["canonical_selection"]["values"]["size"], "XL")
        self.assertEqual(current.capture["canonical_selection"]["evidence"]["size"]["source_message_id"], correction.pk)

    def test_reconciliation_repairs_source_commit_crash_without_generation(self):
        source = self.source("committed without enqueue")
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            self.assertEqual(producer.reconcile_memory_sources()["queued"], 1)
            self.assertEqual(producer.reconcile_memory_sources()["queued"], 0)
        provider.assert_not_called()
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state["dirty"]["message_id"], source.pk)

    def test_reconciliation_sweeps_more_than_one_batch_of_distinct_clients_fairly(self):
        clients = [IgClient.objects.create(igsid=f"reconcile-fair-{index}") for index in range(31)]
        for index, client in enumerate(clients):
            InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
                role="user", source="webhook", text="missing source hook", mid=f"fair-{index}",
                provider_namespace="instagram_login:memory-owner", provider_created_at=self.clock, status="done")
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            sweeps = [producer.reconcile_memory_sources(limit=25) for _ in range(4)]
        provider.assert_not_called()
        self.assertEqual(sum(item["queued"] for item in sweeps), len(clients))
        self.assertEqual(IgClient.objects.filter(pk__in=[client.pk for client in clients], memory_dirty_at__isnull=True).count(), 0)
        self.assertLessEqual(max(item["scanned"] for item in sweeps), 25)

    def test_reconciliation_crash_replays_page_and_cas_loser_preserves_winning_cursor(self):
        from management.models import InstagramBotSettings
        first, second = self.source("first"), self.source("second")
        other = IgClient.objects.create(igsid="memory-crash-second")
        second.client, second.sender_id = other, other.igsid
        second.save(update_fields=["client", "sender_id"])
        enqueue = producer.enqueue_memory_source
        calls = [0]

        def crash(message_id, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("crash before checkpoint")
            return enqueue(message_id, **kwargs)

        with patch.object(producer, "enqueue_memory_source", side_effect=crash):
            with self.assertRaises(RuntimeError):
                producer.reconcile_memory_sources()
        self.assertEqual(InstagramBotSettings.objects.get(pk=1).memory_reconcile_cursor, 0)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state["dirty"]["message_id"], first.pk)
        self.assertEqual(producer.reconcile_memory_sources()["queued"], 1)
        configuration = InstagramBotSettings.objects.get(pk=1)
        self.assertEqual(configuration.memory_reconcile_cursor, second.pk)
        configuration.memory_reconcile_cursor = 0
        configuration.save(update_fields=["memory_reconcile_cursor"])

        def winner(message_id, **kwargs):
            result = enqueue(message_id, **kwargs)
            InstagramBotSettings.objects.filter(pk=1).update(memory_reconcile_cursor=second.pk + 10)
            return result

        with patch.object(producer, "enqueue_memory_source", side_effect=winner):
            producer.reconcile_memory_sources()
        self.assertEqual(InstagramBotSettings.objects.get(pk=1).memory_reconcile_cursor, second.pk + 10)

    def test_runtime_publish_rechecks_deadline_after_source_validation_before_write(self):
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = self.claim()
        current = [self.clock + timedelta(seconds=5)]

        def validate(client, capture):
            current[0] = claim.deadline_at + timedelta(microseconds=1)
            return ""

        with patch.object(producer.timezone, "now", side_effect=lambda: current[0]), patch.object(producer, "_source_reason", side_effect=validate):
            result = producer.publish_memory_result(claim, "expired result")
        self.assertEqual(result.reason, "claim_deadline_expired")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_version, 0)
        self.assertFalse(self.client.memory_summary)

    def test_mutation_guard_runtime_rechecks_clock_after_lane_acquisition(self):
        from management.services import ig_analysis_lane as lane
        source = self.source()
        producer.enqueue_memory_source(source.pk, now=self.clock)
        claim = self.claim()
        current = [self.clock + timedelta(seconds=5)]
        original = lane._row

        def acquire(*args, **kwargs):
            row = original(*args, **kwargs)
            current[0] = self.owner["lease_until"] + timedelta(microseconds=1)
            return row

        with patch.object(producer.timezone, "now", side_effect=lambda: current[0]), patch.object(lane, "_row", side_effect=acquire):
            result = producer.publish_memory_result(claim, "expired owner")
        self.assertEqual(result.reason, "lane_owner_changed")
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_version, 0)

    def test_blank_namespace_human_reply_requires_exact_sent_command_receipt(self):
        from django.contrib.auth import get_user_model
        from management.models import HumanReplyCommand
        context = self.source("customer question")
        reply = self.source("manager answer", role="manager", source="human_reply")
        reply.provider_namespace = ""
        reply.send_state = "sent"
        reply.provider_message_id = "human-receipt"
        reply.save(update_fields=["provider_namespace", "send_state", "provider_message_id"])
        self.assertFalse(producer.enqueue_memory_source(reply.pk, now=self.clock).queued)
        actor = get_user_model().objects.create_user(username="memory-actor")
        command = HumanReplyCommand.objects.create(client=self.client, actor=actor, context_message=context,
            reply_message=reply, recipient_igsid=self.client.igsid, provider_namespace=context.provider_namespace,
            text=reply.text, state="sent", provider_message_ids=[reply.provider_message_id],
            provider_started_at=self.clock, terminal_at=self.clock + timedelta(seconds=1),
            window_deadline=self.clock + timedelta(hours=1))
        self.assertTrue(producer.enqueue_memory_source(reply.pk, now=self.clock).queued)
        claim = self.claim()
        self.assertEqual(claim.capture["scope"]["namespace"], context.provider_namespace)
        self.assertIn("manager answer", claim.capture["transcript"])
        HumanReplyCommand.objects.filter(pk=command.pk).update(provider_message_ids=["different-receipt"])
        self.assertEqual(producer.publish_memory_result(claim, "wrong provenance", now=self.clock + timedelta(seconds=5)).reason, "source_changed")

    def test_sixty_human_receipts_have_constant_bounded_read_query_count(self):
        from django.contrib.auth import get_user_model
        from django.test.utils import CaptureQueriesContext
        from management.models import HumanReplyCommand
        context = self.source("customer context")
        actor = get_user_model().objects.create_user(username="batch-memory-actor")
        for index in range(60):
            reply = self.source(f"human message {index}", role="manager", source="human_reply")
            reply.provider_namespace = ""
            reply.send_state = "sent"
            reply.provider_message_id = f"batch-human-{index}"
            reply.save(update_fields=["provider_namespace", "send_state", "provider_message_id"])
            HumanReplyCommand.objects.create(client=self.client, actor=actor, context_message=context,
                reply_message=reply, recipient_igsid=self.client.igsid, provider_namespace=context.provider_namespace,
                text=reply.text, state="sent", provider_message_ids=[reply.provider_message_id],
                provider_started_at=self.clock, terminal_at=self.clock + timedelta(seconds=1),
                window_deadline=self.clock + timedelta(hours=1))
        producer.enqueue_memory_source(reply.pk, now=self.clock)
        with CaptureQueriesContext(connection) as capture_queries:
            claim = self.claim()
        self.assertLessEqual(len(capture_queries), 14, [query["sql"] for query in capture_queries])
        with self.settings(IG_MEMORY_GENERATION_ENABLED=True, IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True):
            with CaptureQueriesContext(connection) as guard_queries:
                self.assertIs(producer.current_claim_admission(claim), True)
            self.assertLessEqual(len(guard_queries), 20, [query["sql"] for query in guard_queries])
        self.assertTrue(producer.publish_memory_result(claim, "bounded proof", now=self.clock + timedelta(seconds=5)).published)
        with CaptureQueriesContext(connection) as read_queries:
            self.assertEqual(producer.read_memory_summary(self.client).reason, "current")
        self.assertLessEqual(len(read_queries), 8, [query["sql"] for query in read_queries])

    def test_newest_correction_survives_character_cap_and_missing_hook_rejects_head(self):
        for index in range(60):
            row = self.source("long old context " * 200)
            producer.enqueue_memory_source(row.pk, now=self.clock)
        latest = self.source("CURRENT CORRECTION size XL")
        producer.enqueue_memory_source(latest.pk, now=self.clock)
        claim = self.claim()
        self.assertIn("CURRENT CORRECTION", claim.capture["transcript"])
        self.assertTrue(any(item["chars_omitted"] for item in claim.capture["sources"]))
        self.assertTrue(producer.publish_memory_result(claim, "covered narrative", now=self.clock + timedelta(seconds=5)).published)
        self.source("a later event whose enqueue hook crashed")
        self.assertEqual(producer.read_memory_summary(self.client).reason, "narrative_source_stale")

    def test_source_failure_and_deadline_keep_dirty_without_spurious_freshness(self):
        row = self.source()
        producer.enqueue_memory_source(row.pk, now=self.clock)
        claim = self.claim()
        result = producer.publish_memory_result(claim, "too late", now=claim.deadline_at + timedelta(seconds=1))
        self.assertEqual(result.reason, "claim_deadline_expired")
        self.client.refresh_from_db()
        self.assertIsNotNone(self.client.memory_dirty_at)
        self.assertFalse(self.client.memory_summary)

    def test_current_poll_queues_but_historical_import_does_not_advance_scope(self):
        current = self.source("current poll source", source="poll")
        self.assertTrue(producer.enqueue_memory_source(current.pk, now=self.clock).queued)
        for kind in ("poll_history", "history", "import", "backfill", "poll"):
            old = self.source("older provider event", source=kind, at=self.clock - timedelta(days=1))
            self.assertFalse(producer.enqueue_memory_source(old.pk, now=self.clock).queued)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state["dirty"]["message_id"], current.pk)

    def test_actual_reset_clears_private_capture_and_invalidates_head_version(self):
        from django.contrib.auth import get_user_model
        from management.services.ig_funnel_reset import reset_funnel
        self.head()
        newer = self.source("private work in flight")
        producer.enqueue_memory_source(newer.pk, now=self.clock + timedelta(seconds=6))
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=10))
        actor = get_user_model().objects.create_user(username="memory-reset-actor")
        result = reset_funnel(client_id=self.client.pk, actor=actor, reason="memory CAS reset")
        self.assertTrue(result["ok"], result)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_version, 2)
        for field in ("memory_provenance", "memory_producer_state", "memory_claim_snapshot", "memory_claim_token", "memory_summary"):
            self.assertFalse(getattr(self.client, field))
        self.assertIsNone(self.client.memory_due_at)
        self.assertFalse(producer.publish_memory_result(claim, "late reset result").published)

    def test_actual_erasure_target_fence_clears_private_capture_before_storage_work(self):
        from types import SimpleNamespace
        from django.db import transaction
        from management.services.ig_data_deletion import _frozen_targets
        self.head()
        newer = self.source("private work in flight")
        producer.enqueue_memory_source(newer.pk, now=self.clock + timedelta(seconds=6))
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=10))
        with transaction.atomic():
            _frozen_targets(SimpleNamespace(normalized_identifier=self.client.igsid, identifier=self.client.igsid))
        self.client.refresh_from_db()
        self.assertIsNotNone(self.client.privacy_erasure_started_at)
        self.assertEqual(self.client.memory_version, 2)
        for field in ("memory_provenance", "memory_producer_state", "memory_claim_snapshot", "memory_claim_token", "memory_summary"):
            self.assertFalse(getattr(self.client, field))
        self.assertEqual(producer.publish_memory_result(claim, "late erasure result").reason, "client_erasing")

    def test_direct_delete_fence_clears_private_capture_even_when_blob_work_blocks(self):
        from management.bot_views import _delete_direct_bot_records
        source, prior = self.head()
        source.private_media_state = "active"
        source.attachment_media = [{"type": "image", "storage_key": "private-memory-test"}]
        source.save(update_fields=["private_media_state", "attachment_media"])
        with patch("management.services.ig_private_media.delete_immediately", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "private media"):
                _delete_direct_bot_records(exact_client_ids=[self.client.pk])
        self.client.refresh_from_db()
        self.assertIsNotNone(self.client.privacy_erasure_started_at)
        self.assertFalse(self.client.memory_summary)
        self.assertFalse(self.client.memory_provenance)
        self.assertFalse(self.client.memory_claim_snapshot)


@skipUnless(connection.vendor == "mysql", "Requires disposable MariaDB/InnoDB owned by root")
class NativeMemoryProducerRaceTests(TransactionTestCase):
    def setUp(self):
        self.clock = timezone.now()
        self.owner_context = owner_scope(now=self.clock, lease_seconds=3600)
        self.owner = self.owner_context.__enter__()
        self.addCleanup(self.owner_context.__exit__, None, None, None)
        self.client = IgClient.objects.create(igsid=self._testMethodName[:64])
        self.source = InstagramBotMessage.objects.create(client=self.client, sender_id=self.client.igsid,
            role="user", source="webhook", text="private captured input", mid=self._testMethodName,
            provider_namespace="instagram_login:native-memory", provider_created_at=self.clock, status="done")
        producer.enqueue_memory_source(self.source.pk, now=self.clock)

    def workers(self, callbacks):
        import threading
        from queue import Queue
        from django.db import close_old_connections, connections
        from management.services.ig_analysis_lane import bind_owner, unbind_owner
        results = Queue()

        def run(callback):
            close_old_connections()
            marker = bind_owner(self.owner)
            try:
                results.put((True, callback()))
            except Exception as exc:
                results.put((False, exc))
            finally:
                unbind_owner(marker)
                connections.close_all()

        threads = [threading.Thread(target=run, args=(callback,), daemon=True) for callback in callbacks]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive(), "bounded native race did not complete")
        output = [results.get_nowait() for _ in threads]
        for success, value in output:
            self.assertTrue(success, repr(value))
        return [value for success, value in output]

    def test_two_owned_workers_claim_one_client_exactly_once(self):
        import threading
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(5)
            return producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))

        claims = self.workers([claim, claim])
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_claim_token, next(item for item in claims if item).token)

    def test_expired_replacement_published_before_old_worker_is_monotonic(self):
        import threading
        first = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
        current = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=45))
        newer_done = threading.Event()

        def newer():
            try:
                return producer.publish_memory_result(current, "new generation", now=self.clock + timedelta(seconds=46))
            finally:
                newer_done.set()

        def older():
            self.assertTrue(newer_done.wait(5))
            return producer.publish_memory_result(first, "late generation", now=self.clock + timedelta(seconds=47))

        output = self.workers([newer, older])
        self.assertEqual(sum(item.published for item in output), 1)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_summary, "new generation")
        self.assertEqual(self.client.memory_version, 1)

    def test_locked_reset_and_erasure_win_against_inflight_publish(self):
        import threading
        from django.db import connections, transaction
        for erasure in (False, True):
            with self.subTest(erasure=erasure):
                claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
                if claim is None:
                    producer.enqueue_memory_source(self.source.pk, now=self.clock)
                    claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
                locked, publisher_arrived = threading.Event(), threading.Event()

                def boundary():
                    with transaction.atomic():
                        client = IgClient.objects.select_for_update().get(pk=self.client.pk)
                        updates = producer.memory_invalidation_updates(client)
                        if erasure:
                            updates["privacy_erasure_started_at"] = timezone.now()
                        for field, value in updates.items():
                            setattr(client, field, value)
                        client.save(update_fields=[*updates])
                        locked.set()
                        self.assertTrue(publisher_arrived.wait(5))
                    return "boundary_committed"

                def publish():
                    self.assertTrue(locked.wait(5))

                    def observe(execute, sql, params, many, context):
                        if IgClient._meta.db_table in sql and "FOR UPDATE" in sql.upper():
                            publisher_arrived.set()
                        return execute(sql, params, many, context)

                    with connections["default"].execute_wrapper(observe):
                        return producer.publish_memory_result(claim, "must not resurrect", now=self.clock + timedelta(seconds=5))

                output = self.workers([boundary, publish])
                self.assertFalse(next(item for item in output if isinstance(item, producer.MemoryResult)).published)
                self.client.refresh_from_db()
                self.assertFalse(self.client.memory_summary)
                self.assertFalse(self.client.memory_claim_snapshot)

    def _publish_after_held_lock_expiry(self, *, lane_lock):
        import threading
        from django.db import connections, transaction
        from management.models import IgWorkerLaneState
        from management.services.ig_analysis_lane import LANE_KEY
        claim = producer.claim_memory_job(client_id=self.client.pk, now=self.clock + timedelta(seconds=4))
        expiry = timezone.now() + timedelta(seconds=0.8)
        if lane_lock:
            IgWorkerLaneState.objects.filter(lane_key=LANE_KEY).update(lease_until=expiry)
        else:
            capture = deepcopy(claim.capture)
            capture["deadline_at"] = expiry.isoformat()
            capture["digest"] = producer._digest({key: value for key, value in capture.items() if key != "digest"})
            claim = replace(claim, capture=capture, deadline_at=expiry)
            IgClient.objects.filter(pk=self.client.pk).update(memory_claim_snapshot=capture)
        locked, publisher_arrived = threading.Event(), threading.Event()
        table = IgWorkerLaneState._meta.db_table if lane_lock else IgClient._meta.db_table

        def hold():
            with transaction.atomic():
                if lane_lock:
                    IgWorkerLaneState.objects.select_for_update().get(lane_key=LANE_KEY)
                else:
                    IgClient.objects.select_for_update().get(pk=self.client.pk)
                locked.set()
                self.assertTrue(publisher_arrived.wait(5))
                while timezone.now() <= expiry:
                    threading.Event().wait(0.01)
            return "lock_released_after_expiry"

        def publish():
            self.assertTrue(locked.wait(5))

            def observe(execute, sql, params, many, context):
                if table in sql and "FOR UPDATE" in sql.upper():
                    publisher_arrived.set()
                return execute(sql, params, many, context)

            with connections["default"].execute_wrapper(observe):
                return producer.publish_memory_result(claim, "must expire while waiting")

        output = self.workers([hold, publish])
        result = next(item for item in output if isinstance(item, producer.MemoryResult))
        self.assertFalse(result.published)
        self.assertEqual(result.reason, "lane_owner_changed" if lane_lock else "claim_deadline_expired")
        self.client.refresh_from_db()
        self.assertFalse(self.client.memory_summary)
        self.assertEqual(self.client.memory_version, 0)

    def test_runtime_client_lock_wait_past_capture_deadline_cannot_publish(self):
        self._publish_after_held_lock_expiry(lane_lock=False)

    def test_runtime_lane_lock_wait_past_owner_lease_cannot_publish(self):
        self._publish_after_held_lock_expiry(lane_lock=True)

    def test_reconcile_client_wait_does_not_block_restricted_inbound_settings_update(self):
        import threading
        from django.db import connections, transaction
        from management.models import InstagramBotSettings
        from management.services import instagram_bot
        configuration = InstagramBotSettings.objects.create(pk=1, is_enabled=True)
        IgClient.objects.filter(pk=self.client.pk).update(memory_producer_state={},
            memory_due_at=None, memory_dirty_at=None)
        locked, reconcile_arrived = threading.Event(), threading.Event()
        incoming_mid = "native-restricted-during-reconcile"

        def restricted_inbound():
            with transaction.atomic():
                IgClient.objects.select_for_update().get(pk=self.client.pk)
                locked.set()
                self.assertTrue(reconcile_arrived.wait(5))
                with patch.object(instagram_bot, "ingress_provider_namespace", return_value=self.source.provider_namespace), patch.object(instagram_bot, "log"):
                    return instagram_bot._observe_not_allowed_inbound(configuration,
                        sender_id=self.client.igsid, text="current customer must survive",
                        mid=incoming_mid, source="webhook", attachments=[], attachment_metadata=None,
                        received_at=timezone.now(), reply_to_provider_message_id="", quick_reply_payload="",
                        synthetic_event_key="", _commercial_lock_held=True)

        def reconcile():
            self.assertTrue(locked.wait(5))

            def observe(execute, sql, params, many, context):
                if IgClient._meta.db_table in sql and "FOR UPDATE" in sql.upper():
                    reconcile_arrived.set()
                return execute(sql, params, many, context)

            with connections["default"].execute_wrapper(observe):
                return producer.reconcile_memory_sources()

        output = self.workers([restricted_inbound, reconcile])
        self.assertIn(True, output)
        incoming = InstagramBotMessage.objects.get(mid=incoming_mid)
        self.client.refresh_from_db()
        self.assertEqual(self.client.memory_producer_state["dirty"]["message_id"], incoming.pk)
        self.assertIsNotNone(InstagramBotSettings.objects.get(pk=1).last_inbound_at)
