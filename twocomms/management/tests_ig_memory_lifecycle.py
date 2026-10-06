"""Offline lifecycle hooks and the existing daemon's bounded memory ownership."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgConversationAnalysisJob, InstagramBotMessage, InstagramBotSettings
from management.services import instagram_bot as bot
from management.services import ig_memory_producer as producer
from management.services.ig_analysis_lane import owner_scope
from management.management.commands import run_instagram_bot as daemon


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_GENERATION_ENABLED=False,
                   IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=False)
class MemoryInboundLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.settings_row = InstagramBotSettings.load()
        self.settings_row.ig_user_id = "999999"
        self.settings_row.ai_enabled = False
        self.settings_row.save(update_fields=["ig_user_id", "ai_enabled"])
        self.customer = IgClient.get_or_create_for_sender("123456789")
        self.environment = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def enqueue(self, mid="memory-inbound", text="Хочу футболку L", source="webhook"):
        return bot.enqueue_inbound(self.settings_row, sender_id=self.customer.igsid, text=text,
            mid=mid, source=source, received_at=timezone.now(), persistence_only=True)

    def test_accepted_inbound_dirty_while_ai_off_and_client_paused_without_generation(self):
        self.customer.bot_paused = True
        self.customer.manager_takeover = True
        self.customer.save(update_fields=["bot_paused", "manager_takeover"])
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider:
            self.assertTrue(self.enqueue())
        provider.assert_not_called()
        self.customer.refresh_from_db()
        source = InstagramBotMessage.objects.get(mid="memory-inbound")
        self.assertEqual(self.customer.memory_producer_state["dirty"]["message_id"], source.pk)
        self.assertIsNotNone(self.customer.memory_dirty_at)
        before = self.customer.memory_producer_state
        self.assertFalse(self.enqueue())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.memory_producer_state, before)

    def test_no_reply_optout_still_admits_accepted_source(self):
        self.assertTrue(self.enqueue(text="Не пишіть мені більше"))
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.memory_dirty_at)

    def test_accepted_live_poll_source_dirty_without_provider_io(self):
        # The live poll loop uses this same canonical acceptance entrypoint.
        with patch.object(bot, "_provider_http") as provider:
            self.assertTrue(self.enqueue(mid="memory-live-poll", source="poll"))
        provider.assert_not_called()
        self.customer.refresh_from_db()
        source = InstagramBotMessage.objects.get(mid="memory-live-poll")
        self.assertEqual(self.customer.memory_producer_state["dirty"]["message_id"], source.pk)

    @override_settings(IG_REVISION_EXECUTION_ENABLED=False)
    def test_legacy_manager_accepted_source_dirty(self):
        bot._handle_echo(self.customer.igsid, "Розмір L у наявності",
            mid="memory-legacy-manager", received_at=timezone.now(), persistence_only=True,
            provider_namespace=bot.ingress_provider_namespace(self.settings_row))
        self.customer.refresh_from_db()
        message = InstagramBotMessage.objects.get(mid="memory-legacy-manager")
        self.assertEqual(self.customer.memory_producer_state["dirty"]["message_id"], message.pk)

    def test_history_and_erased_clients_never_create_dirty_work(self):
        self.enqueue(source="poll_history")
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.memory_dirty_at)
        self.customer.privacy_erasure_started_at = timezone.now()
        self.customer.save(update_fields=["privacy_erasure_started_at"])
        self.assertFalse(self.enqueue(mid="memory-erased"))
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.memory_dirty_at)

    def test_enqueue_failure_isolated_and_bounded_reconcile_repairs_crash_gap(self):
        with patch.object(producer, "enqueue_memory_source", side_effect=RuntimeError("offline")):
            self.assertTrue(self.enqueue())
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.memory_dirty_at)
        producer.reconcile_memory_sources(limit=1)
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.memory_dirty_at)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class MemoryHumanLifecycleTests(TransactionTestCase):
    from management.tests_ig_human_reply import HumanReplyCommandTests
    setUp = HumanReplyCommandTests.setUp

    def test_exact_sent_postcommit_enqueues_once_and_replay_does_not_send(self):
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command
        with patch("management.services.instagram_bot.send_text", return_value=SimpleNamespace(
            ok=True, kind="", hint="", provider_message_ids=("mid-memory-human",))) as send, patch(
            "management.services.ig_human_reply.InstagramBotSettings.load", return_value=self.settings):
            command = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово").command
            delivered = dispatch_human_reply_command(command.pk)
            self.customer.refresh_from_db()
            self.assertEqual(self.customer.memory_producer_state["dirty"]["message_id"], delivered.reply_message_id)
            before = self.customer.memory_producer_state
            dispatch_human_reply_command(command.pk)
        self.assertEqual(send.call_count, 1)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.memory_producer_state, before)

    def test_unknown_human_reply_never_enqueues(self):
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command
        for kind in ("unknown",):
            with self.subTest(kind=kind), patch("management.services.instagram_bot.send_text", return_value=SimpleNamespace(
                ok=False, kind=kind, hint="offline", provider_message_ids=())), patch(
                "management.services.ig_human_reply.InstagramBotSettings.load", return_value=self.settings):
                command = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово").command
                dispatch_human_reply_command(command.pk)
                self.customer.refresh_from_db()
                self.assertIsNone(self.customer.memory_dirty_at)

    def test_failed_human_reply_never_enqueues(self):
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command
        with patch("management.services.instagram_bot.send_text", return_value=SimpleNamespace(
            ok=False, kind="failed", hint="offline", provider_message_ids=())), patch(
            "management.services.ig_human_reply.InstagramBotSettings.load", return_value=self.settings):
            command = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово").command
            dispatch_human_reply_command(command.pk)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.memory_dirty_at)

    def test_partial_unknown_human_receipt_never_enqueues(self):
        from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command
        with patch("management.services.instagram_bot.send_text", return_value=SimpleNamespace(
            ok=False, kind="unknown", hint="partial", provider_message_ids=("mid-partial",))), patch(
            "management.services.ig_human_reply.InstagramBotSettings.load", return_value=self.settings):
            command = create_human_reply_command(self.customer.pk, actor=self.actor, text="Готово").command
            delivered = dispatch_human_reply_command(command.pk)
        self.assertEqual(delivered.state, "unknown")
        self.assertEqual(delivered.provider_message_ids, ["mid-partial"])
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.memory_dirty_at)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=True,
    IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00", SITE_BASE_URL="https://twocomms.test")
class MemoryManagerEchoLifecycleTests(TransactionTestCase):
    from management.tests_ig_revision_echo import RevisionEchoTests
    setUp = RevisionEchoTests.setUp

    def test_canonical_accepted_manager_echo_dirty_and_replay_noop(self):
        from management.services.ig_revision_echo_integration import observe_and_project_echo
        kwargs = dict(namespace=self.namespace, recipient=self.client_row.igsid,
            mid="manager-memory-source", text="Розмір L у наявності", received_at=timezone.now())
        self.assertTrue(observe_and_project_echo(self.settings, **kwargs))
        self.client_row.refresh_from_db()
        message = InstagramBotMessage.objects.get(mid=kwargs["mid"])
        self.assertEqual(self.client_row.memory_producer_state["dirty"]["message_id"], message.pk)
        before = self.client_row.memory_producer_state
        observe_and_project_echo(self.settings, **kwargs)
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.memory_producer_state, before)

    def test_historical_manager_echo_never_dirty(self):
        from management.services.ig_revision_echo_integration import observe_and_project_echo
        self.assertTrue(observe_and_project_echo(self.settings, namespace=self.namespace,
            recipient=self.client_row.igsid, mid="memory-history-echo", text="Історична відповідь", historical=True))
        self.client_row.refresh_from_db()
        self.assertIsNone(self.client_row.memory_dirty_at)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class MemoryRevisionLifecycleTests(TransactionTestCase):
    from management.tests_ig_revision_live import RevisionLiveTests
    setUp = RevisionLiveTests.setUp
    _message = RevisionLiveTests._message
    _prepare = RevisionLiveTests._prepare
    _execute = RevisionLiveTests._execute
    _generate = RevisionLiveTests._generate

    def test_sent_reply_finalization_enqueues_projection_and_replay_is_noop(self):
        from management.services.ig_revision_execution import finalize_sent_revision_effects
        self._prepare()
        result, _, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.customer.refresh_from_db()
        reply = InstagramBotMessage.objects.get(source="revision_reply")
        self.assertEqual(self.customer.memory_producer_state["dirty"]["message_id"], reply.pk)
        before = self.customer.memory_producer_state
        with patch("management.services.instagram_bot._provider_http") as replay_http:
            finalize_sent_revision_effects(self.revision.pk)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.memory_producer_state, before)
        self.assertEqual(http.call_count, 1)
        replay_http.assert_not_called()


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_GENERATION_ENABLED=True,
                   IG_MEMORY_PROVIDER_ADMISSION_ACCEPTED=True, GEMINI_ACCOUNTING_V2_MODE="shadow",
                   GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2020-01-01T08:00:00+00:00",
                   GEMINI_NONLIVE_ADMISSION_MODE="enforce")
class MemoryDaemonOwnershipTests(TransactionTestCase):
    def setUp(self):
        InstagramBotSettings.objects.create(pk=1, is_enabled=False)
        self.customer = IgClient.objects.create(igsid="memory-daemon-customer")
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="done", text="L", mid="memory-daemon-source",
            provider_namespace="instagram_login:owner", provider_created_at=timezone.now())
        self.owner_context = owner_scope(lease_seconds=3600)
        self.owner_context.__enter__()
        self.addCleanup(self.owner_context.__exit__, None, None, None)
        producer.enqueue_memory_source(self.source.pk)
        self.claim = producer.claim_memory_job(client_id=self.customer.pk, now=timezone.now()+timedelta(seconds=4))
        self.assertIsNotNone(self.claim)

    def test_analysis_priority_guard_read_only_and_current(self):
        with patch("management.services.ig_analysis_lane._row", side_effect=AssertionError("lock inversion")):
            self.assertIs(daemon._memory_background_admission(self.claim), True)
            IgConversationAnalysisJob.objects.create(client=self.customer, due_at=timezone.now(), next_attempt_at=timezone.now())
            self.assertEqual(daemon._memory_background_admission(self.claim), "memory_analysis_priority")
            IgConversationAnalysisJob.objects.all().delete()
            self.assertIs(daemon._memory_background_admission(self.claim), True)

    def test_memory_tick_bounded_and_uses_final_dynamic_guard(self):
        with patch.object(producer, "process_due_memory", return_value={"claimed": 0}) as process:
            daemon._memory_background_tick()
        process.assert_called_once_with(limit=1, admission=daemon._memory_background_admission)

    def test_existing_worker_prioritizes_analysis_and_events_before_one_memory_tick(self):
        from management.tests_ig_daemon import _BoundedWorkerEvent
        order = []
        with patch.object(daemon, "close_old_connections"), patch.object(
            daemon, "require_database_ready"), patch.object(
            daemon, "maintenance_status", return_value={"active": False}), patch.object(
            daemon, "_memory_background_tick", side_effect=lambda: order.append("memory")) as memory, patch(
            "management.services.bot_conversation_analysis.reconcile_analysis_jobs", return_value={}), patch(
            "management.services.ig_typed_memory.reconcile_typed_memory"), patch.object(
            producer, "reconcile_memory_sources") as reconcile, patch(
            "management.services.bot_conversation_analysis.process_due_analysis", side_effect=lambda **kw: order.append("analysis")), patch(
            "management.services.ig_analysis_events.process_due_analysis_events",
            side_effect=lambda **kw: order.append("events") or {}):
            daemon._analysis_worker(_BoundedWorkerEvent(cycles=1))
        self.assertEqual(order, ["analysis", "events", "memory"])
        memory.assert_called_once_with()
        reconcile.assert_called_once_with(limit=daemon.MEMORY_RECONCILE_BATCH)

    def test_optional_source_reconcile_failure_preserves_analysis_cadence(self):
        from management.tests_ig_daemon import _BoundedWorkerEvent
        with patch.object(daemon, "close_old_connections"), patch.object(
            daemon, "require_database_ready"), patch.object(
            daemon, "maintenance_status", return_value={"active": False}), patch.object(
            daemon.time, "monotonic", return_value=100), patch.object(
            daemon, "_memory_background_tick"), patch.object(daemon.bot, "log"), patch(
            "management.services.bot_conversation_analysis.reconcile_analysis_jobs", return_value={}) as analysis, patch(
            "management.services.ig_typed_memory.reconcile_typed_memory"), patch.object(
            producer, "reconcile_memory_sources", side_effect=RuntimeError("offline")) as narrative, patch(
            "management.services.bot_conversation_analysis.process_due_analysis"), patch(
            "management.services.ig_analysis_events.process_due_analysis_events", return_value={}):
            daemon._analysis_worker(_BoundedWorkerEvent(cycles=3))
        analysis.assert_called_once_with(limit=daemon.ANALYSIS_RECONCILE_BATCH)
        narrative.assert_called_once_with(limit=daemon.MEMORY_RECONCILE_BATCH)

    def test_provider_final_guard_rechecks_analysis_priority(self):
        # A job arrives after claim/initial admission but before physical HTTP.
        # Provider mock exercises the exact pre-dispatch callback, with no I/O.
        producer.fail_memory_claim(self.claim)
        self.customer.refresh_from_db()
        self.customer.memory_due_at = timezone.now() - timedelta(seconds=1)
        self.customer.save(update_fields=["memory_due_at"])
        def generate(_payload, **kwargs):
            self.assertIs(kwargs["pre_dispatch_guard"](), True)
            IgConversationAnalysisJob.objects.create(client=self.customer, due_at=timezone.now(), next_attempt_at=timezone.now())
            self.assertEqual(kwargs["pre_dispatch_guard"](), "memory_analysis_priority")
            raise RuntimeError("admission denied before physical HTTP")
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=generate) as provider:
            result = daemon._memory_background_tick()
        self.assertEqual(provider.call_count, 1, result)
        self.assertEqual(result["published"], 0)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.memory_summary)
