"""Real ingress during canonical provider execution shares one bounded ledger."""
from datetime import timedelta
from copy import deepcopy
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from management import tests_ig_revision_provider_execution as fixtures
from management import tests_ig_revision_live as live_fixtures
from management.models import GeminiRequest, GeminiRequestAttempt, IgCustomerTurnRevision
from management.services.ig_customer_turns import ensure_turn_for_inbound
from management.services.ig_revision_burst_budget import IN_KEY, economic_root
from management.services.ig_revision_execution import prepare_revision, expired_revision_debt_ids
from management.services.ig_revision_provider_execution import (
    MANIFEST_KEY, REFERENCE_KEY, REPAIR_KEY, SALVAGE_KEY, _root,
    inspect_revision_provider_execution, revision_provider_continuation,
)
from management.services.ig_turn_revisions import create_refresh_successor
from management.tests_gemini_accounting_shadow import SHADOW


@override_settings(**SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionBurstBudgetTests(TransactionTestCase):
    setUp = fixtures.RevisionProviderExecutionTests.setUp
    _message = fixtures.RevisionProviderExecutionTests._message
    _prepare = fixtures.RevisionProviderExecutionTests._prepare
    _begin = fixtures.RevisionProviderExecutionTests._begin
    _attempt = fixtures.RevisionProviderExecutionTests._attempt
    _admit = fixtures.RevisionProviderExecutionTests._admit

    def incoming(self, *, seconds=2, text="І ще чорне худі, розмір L"):
        previous = self.revision
        source = self._message(text, f"burst-{previous.pk}")
        attached = ensure_turn_for_inbound(source, now=self.start + timedelta(seconds=seconds))
        self.revision = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        self.source = source
        return previous

    def prepare_child(self):
        prepared = prepare_revision(self.revision.pk, lambda **kwargs: None,
                                    now=max(timezone.now(), self.revision.quiet_deadline))
        self.assertTrue(prepared.ready, prepared.reason)
        self.token = prepared.execution_token
        self.revision.refresh_from_db()

    def start_root(self):
        self.start = self.revision.quiet_started_at
        observer = self._begin()
        self.assertTrue(observer.enabled, observer.block_reason)
        return observer

    def test_worker_t0_t2_t4_preserves_sources_caps_and_one_provider_budget(self):
        first = self.source
        observer = self.start_root()
        self._attempt(observer, 0)
        root = self.incoming(seconds=2)
        second = self.source
        self.prepare_child()
        observer = self._begin()
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertEqual(observer.provider_continuation.http_remaining, 7)
        self._attempt(observer, 1)
        self.incoming(seconds=4)
        self.prepare_child()
        observer = self._begin()
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertEqual(observer.provider_continuation.http_remaining, 6)
        self.assertEqual(list(self.revision.sources.order_by("ordinal").values_list("message_id", flat=True)),
                         [first.pk, second.pk, self.source.pk])
        self.assertEqual(self.revision.quiet_cap_at, root.quiet_cap_at)
        self.assertEqual(self.revision.overall_deadline, root.overall_deadline)
        self.assertEqual(economic_root(self.revision).pk, root.pk)
        self.assertEqual(_root(self.revision).pk, self.revision.pk)
        self.assertNotEqual(self.revision.snapshot_digest, root.snapshot_digest)
        self.assertNotIn(MANIFEST_KEY, self.revision.action_receipts)
        root.refresh_from_db()
        self.assertEqual(root.state, "superseded")
        self.assertNotIn(root.pk, expired_revision_debt_ids(now=root.overall_deadline + timedelta(seconds=1)))

    def test_late_old_http_settles_usage_once_without_winner_or_new_admission(self):
        observer = self.start_root()
        old_source = self.source
        row = self.raw_plan[0]
        boundary = observer.attempt(key_name=row["key_name"], model=row["model"], candidate_index=1)
        self.assertTrue(boundary.before_provider(serialized_bytes=100))
        old = self.incoming()
        self.prepare_child()
        child = self._begin()
        self.assertTrue(child.enabled, child.block_reason)
        self.assertEqual(child.provider_continuation.http_remaining, 7)
        boundary.manual_result(succeeded=True, http_code=200, usage={"promptTokenCount": 11, "totalTokenCount": 17})
        boundary.manual_result(succeeded=True, http_code=200, usage={"promptTokenCount": 99, "totalTokenCount": 99})
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        self.assertEqual(attempt.total_tokens, 17)
        self.assertIsNotNone(attempt.permit_released_at)
        self.assertFalse(attempt.winner_claimed)
        self.assertIsNone(graph.winner_attempt_id)
        self.assertEqual(graph.terminal_reason, "revision_execution_revoked")
        self.assertFalse(graph.attempts.filter(not_attempted_reason="winner_found").exists())
        retry = observer.attempt(key_name=self.raw_plan[1]["key_name"], model=self.raw_plan[1]["model"], candidate_index=2)
        self.assertFalse(retry.before_provider(serialized_bytes=100))
        old_source.refresh_from_db()
        self.assertEqual(old_source.status, "pending")
        self.assertFalse(old.delivery_effects.exists())
        self.assertEqual(inspect_revision_provider_execution(self.revision).http_remaining, 7)

    def test_manifest_without_graph_hands_off_without_new_budget_or_stall(self):
        self.start = self.revision.quiet_started_at
        frozen = revision_provider_continuation(self.revision.pk, self.token,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            candidate_plan=self.raw_plan)
        self.assertTrue(frozen.ready, frozen.reason)
        self.assertFalse(GeminiRequest.objects.exists())
        root = self.incoming()
        self.prepare_child()
        observer = self._begin()
        self.assertTrue(observer.enabled, observer.block_reason)
        self.assertEqual(observer.provider_continuation.root_revision_id, root.pk)
        self.assertEqual(observer.provider_continuation.http_remaining, 8)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(GeminiRequest.objects.get().source_message_id, self.source.pk)
        self._attempt(observer, 0)
        self.assertEqual(inspect_revision_provider_execution(self.revision).http_remaining, 7)

    def test_all_spent_http_and_reserved_repairs_remain_spent_after_caption(self):
        observer = self.start_root()
        for index in range(8):
            self._attempt(observer, index)
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        graph.candidate_outcomes = {**graph.candidate_outcomes, REPAIR_KEY: {"token": "used"}, SALVAGE_KEY: {"token": "used"}}
        graph.save(update_fields=["candidate_outcomes", "updated_at"])
        root = self.incoming(seconds=6.1)
        self.assertNotEqual(root.turn_id, self.revision.turn_id)
        self.prepare_child()
        state = inspect_revision_provider_execution(self.revision)
        self.assertEqual(state.reason, "provider_dispatch_budget")
        self.assertEqual(state.http_remaining, 0)
        self.assertFalse(state.repair_remaining)
        blocked = self._begin()
        self.assertFalse(blocked.enabled)
        self.assertEqual(GeminiRequest.objects.count(), 1)

    def test_empty_count_and_recovery_reference_survive_changed_snapshot(self):
        observer = self.start_root()
        self._attempt(observer, 0, code=200, failure="empty")
        root = self.incoming()
        self.prepare_child()
        self.assertEqual(inspect_revision_provider_execution(self.revision).empty_response_count, 1)
        result = create_refresh_successor(self.revision.pk, self.token, reason="publication_changed")
        self.assertTrue(result.created, result.reason)
        self.revision = result.revision
        self.prepare_child()
        state = inspect_revision_provider_execution(self.revision)
        self.assertEqual(state.root_revision_id, root.pk)
        self.assertEqual(state.http_remaining, 7)
        self.assertEqual(state.empty_response_count, 1)
        self.assertEqual(self.revision.action_receipts[REFERENCE_KEY]["root_revision_id"], root.pk)

    def test_late_graph_creation_rechecks_owner_under_shared_mutex(self):
        self.start = self.revision.quiet_started_at
        from management.services import ig_revision_provider_execution as provider
        original = provider.revision_provider_continuation
        def transfer_after_precheck(*args, **kwargs):
            result = original(*args, **kwargs)
            self.incoming()
            return result
        with patch.object(provider, "revision_provider_continuation", side_effect=transfer_after_precheck):
            observer = self._begin()
        self.assertFalse(observer.enabled)
        self.assertEqual(observer.block_reason, "revision_execution_invalid")
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertIn(IN_KEY, self.revision.action_receipts)

    def test_paired_budget_link_and_root_snapshot_corruption_fail_closed(self):
        from django.db.models.query import QuerySet
        from management.services.ig_revision_burst_budget import OUT_KEY
        from management.services.ig_revision_provider_execution import _digest

        self.start_root()
        root = self.incoming()
        self.prepare_child()
        root.refresh_from_db()
        root_receipts, child_receipts = deepcopy(root.action_receipts), deepcopy(self.revision.action_receipts)
        def corrupt(pk, **fields):
            # Deliberately bypass the append-only model guard in this isolated
            # corruption fixture, never in the production mutation path.
            QuerySet.update(IgCustomerTurnRevision.objects.filter(pk=pk), **fields)
        for mutation in ("missing_pair", "wrong_anchor", "wrong_root", "child_refs"):
            with self.subTest(mutation=mutation):
                receipt = deepcopy(child_receipts[IN_KEY])
                if mutation == "wrong_anchor":
                    receipt["anchor_source_id"] = self.source.pk
                elif mutation == "wrong_root":
                    receipt["root_revision_id"] = self.revision.pk
                elif mutation == "child_refs":
                    receipt["successor_sources"][0]["source_digest"] = "a" * 64
                receipt["digest"] = _digest({key: value for key, value in receipt.items() if key != "digest"})
                prior = deepcopy(root_receipts)
                if mutation == "missing_pair":
                    prior.pop(OUT_KEY)
                else:
                    prior[OUT_KEY] = receipt
                corrupt(root.pk, action_receipts=prior)
                corrupt(self.revision.pk, action_receipts={**child_receipts, IN_KEY: receipt})
                self.assertIsNone(economic_root(self.revision))
        corrupt(root.pk, action_receipts=root_receipts)
        corrupt(self.revision.pk, action_receipts=child_receipts)
        snapshot = deepcopy(root.bundle_snapshot)
        snapshot["sources"][-1]["message_id"] = self.source.pk
        corrupt(root.pk, bundle_snapshot=snapshot)
        self.assertIsNone(economic_root(self.revision))
        self.assertIsNone(economic_root(root))

    def _assert_transfer_denied_preserves_old_debt(self):
        from management.services.ig_revision_execution import record_expired_revision_debt
        from management.services.ig_response_debt import reply_debt_payload

        old = self.incoming()
        self.assertNotIn(IN_KEY, self.revision.action_receipts)
        self.assertEqual(list(self.revision.sources.values_list("message_id", flat=True)), [self.source.pk])
        old.refresh_from_db()
        self.assertIsNone(old.active_slot)
        self.assertEqual(old.state, "claimed")
        expired = old.overall_deadline + timedelta(seconds=1)
        self.assertIn(old.pk, expired_revision_debt_ids(now=expired))
        record_expired_revision_debt(old.pk, now=expired)
        self.assertTrue(reply_debt_payload(self.customer)["required"])

    def test_pause_during_active_generation_keeps_separate_visible_debt(self):
        self.start_root()
        self.customer.bot_paused = True
        self.customer.save(update_fields=["bot_paused"])
        self._assert_transfer_denied_preserves_old_debt()

    def test_opt_out_during_active_generation_keeps_separate_visible_debt(self):
        self.start_root()
        self.customer.opted_out_at = timezone.now()
        self.customer.save(update_fields=["opted_out_at"])
        self._assert_transfer_denied_preserves_old_debt()

    def test_changed_source_is_not_reintroduced_by_active_generation_transfer(self):
        self.start_root()
        self.source.text = "source corrected in storage"
        self.source.save(update_fields=["text"])
        self._assert_transfer_denied_preserves_old_debt()

    def test_reviewed_manual_debt_never_gains_a_budget_link(self):
        from management.models import IgFollowUpTask
        self.start_root()
        case = IgFollowUpTask.objects.create(client=self.customer, due_at=timezone.now(),
            kind="manager_task", reason="revision_case:execution_debt", status="cancelled",
            event_key=f"ig-revision-debt:{self.revision.pk}")
        old = self.incoming()
        self.assertNotIn(IN_KEY, self.revision.action_receipts)
        case.refresh_from_db()
        self.assertEqual(case.status, "cancelled")
        self.assertNotIn(old.pk, expired_revision_debt_ids(now=old.overall_deadline + timedelta(seconds=1)))

    def test_unknown_delivery_never_becomes_a_burst_budget_link(self):
        from types import SimpleNamespace
        from management import tests_ig_turn_revisions as turn_fixtures
        self.start_root()
        old = self.revision
        effect = turn_fixtures.TurnRevisionTests.delivery_effect(
            SimpleNamespace(client_row=self.customer, first=self.source), old, state="unknown")
        self._assert_transfer_denied_preserves_old_debt()
        effect.refresh_from_db()
        self.assertEqual(effect.state, "unknown")

    def test_duplicate_new_inbound_does_not_add_another_budget_or_link(self):
        self.start_root()
        self.incoming()
        receipt = deepcopy(self.revision.action_receipts)
        result = ensure_turn_for_inbound(self.source, now=self.start + timedelta(seconds=3))
        self.assertEqual(result.reason, "already_attached")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts, receipt)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 2)

    def test_unrelated_manual_resume_with_same_snapshot_does_not_join_economics(self):
        from management.services.ig_revision_burst_budget import economic_family
        self.start_root()
        self.revision.refresh_from_db()
        fields = {"client": self.customer, "turn": self.turn, "active_slot": None,
                  "snapshot_digest": self.revision.snapshot_digest, "bundle_snapshot": self.revision.bundle_snapshot,
                  "permission_epoch": self.revision.permission_epoch,
                  "quiet_started_at": self.revision.quiet_started_at, "quiet_deadline": self.revision.quiet_deadline,
                  "quiet_cap_at": self.revision.quiet_cap_at, "overall_deadline": self.revision.overall_deadline}
        separate = IgCustomerTurnRevision.objects.create(**fields, parent=self.revision,
            revision=2, origin="manual_resume", state="claimed")
        IgCustomerTurnRevision.objects.create(**fields, parent=separate, revision=3,
            origin="auto_refresh", state="sealed")
        self.assertEqual([row.pk for row in economic_family(self.revision)], [self.revision.pk])
        self.assertTrue(inspect_revision_provider_execution(self.revision).ready)

    @skipUnlessDBFeature("has_select_for_update")
    def test_handoff_serializes_with_admitted_http_on_economic_source_mutex(self):
        from threading import Event, Thread, current_thread
        from django.db import connections
        from management.services import ig_revision_burst_budget as budget
        from management.services import ig_revision_provider_execution as provider

        observer = self.start_root()
        row = self.raw_plan[0]
        boundary = observer.attempt(key_name=row["key_name"], model=row["model"], candidate_index=1)
        admitted, transfer_waiting, release = Event(), Event(), Event()
        errors, started = [], []
        original_admit, original_lock = provider.admit_provider_dispatch_locked, budget.lock_economic_source

        def pause_after_admission(*args, **kwargs):
            reason = original_admit(*args, **kwargs)
            if not reason and current_thread().name == "burst-provider":
                admitted.set()
                if not release.wait(10):
                    raise RuntimeError("provider barrier expired")
            return reason

        def observe_lock(*args, **kwargs):
            if current_thread().name == "burst-ingress":
                transfer_waiting.set()
            return original_lock(*args, **kwargs)

        def run_provider():
            try:
                started.append(boundary.before_provider(serialized_bytes=100))
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        def run_ingress():
            try:
                self.incoming()
            except BaseException as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(provider, "admit_provider_dispatch_locked", side_effect=pause_after_admission), patch.object(budget, "lock_economic_source", side_effect=observe_lock):
            generation = Thread(target=run_provider, name="burst-provider")
            ingress = Thread(target=run_ingress, name="burst-ingress")
            generation.start()
            try:
                self.assertTrue(admitted.wait(10), errors)
                ingress.start()
                self.assertTrue(transfer_waiting.wait(10), errors)
            finally:
                release.set()
                generation.join(10)
                if ingress.ident is not None:
                    ingress.join(10)
            self.assertFalse(generation.is_alive())
            self.assertFalse(ingress.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(started, [True])
        self.assertIn(IN_KEY, self.revision.action_receipts)
        self.prepare_child()
        self.assertEqual(inspect_revision_provider_execution(self.revision).http_remaining, 7)
        boundary.manual_result(succeeded=True, http_code=200, usage={"totalTokenCount": 17})
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertIsNone(graph.winner_attempt_id)
        self.assertEqual(graph.attempts.get(provider_started_at__isnull=False).total_tokens, 17)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionBurstDeliveryTests(TransactionTestCase):
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _prepare_two_images = live_fixtures.RevisionLiveTests._prepare_two_images
    _generate = live_fixtures.RevisionLiveTests._generate
    _execute = live_fixtures.RevisionLiveTests._execute

    def _handoff_during_generation(self, seconds, caption):
        previous = self.revision
        from management.services.ig_revision_source_coverage import plan_source_transfer
        transfer_reasons = []
        def inspect_transfer(*args, **kwargs):
            plan = plan_source_transfer(*args, **kwargs)
            transfer_reasons.append(plan.reason)
            return plan
        def inbound():
            fresh = self._message(caption, "caption-during-provider")
            self.caption = fresh
            attached = ensure_turn_for_inbound(fresh, now=previous.quiet_started_at + timedelta(seconds=seconds))
            self.successor = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        self.before_validate = inbound
        with patch("management.services.ig_revision_source_coverage.plan_source_transfer", side_effect=inspect_transfer):
            result, generation, http = self._execute()
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(result.state, "blocked", result.reasons)
        self.assertEqual(http.call_count, 0)
        self.before_validate = None
        self.revision = self.successor
        previous.refresh_from_db()
        self.assertIn(IN_KEY, self.revision.action_receipts, (transfer_reasons, list(previous.action_receipts)))
        prepared = prepare_revision(self.revision.pk, lambda **kwargs: None, now=self.revision.quiet_deadline)
        self.assertTrue(prepared.ready, prepared.reason)
        self.token = prepared.execution_token
        self.revision.refresh_from_db()
        return previous

    def test_actual_live_successor_sends_once_and_covers_both_original_sources(self):
        from management.services.ig_revision_execution import finalize_sent_revision_effects
        self._prepare()
        previous = self._handoff_during_generation(2, "Підкажіть також про інший принт")
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual([row["message_id"] for row in self.revision.generation_proposal["sources"]],
                         [self.source.pk, self.caption.pk])
        from management.models import IgCommerceTurnDecision
        old_decision = previous.action_receipts["commerce_reduction"]["decisions"][0]
        self.assertEqual(self.revision.action_receipts["commerce_reduction"]["decisions"][0], old_decision)
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message_id=self.source.pk).count(), 1)
        for source in (self.source, self.caption):
            source.refresh_from_db()
            self.assertEqual(source.status, "done")
        self.assertFalse(previous.delivery_effects.exists())
        with patch("management.services.instagram_bot._provider_http") as resend:
            self.assertTrue(finalize_sent_revision_effects(self.revision.pk).completed)
        resend.assert_not_called()

    def test_photo_caption_after_6_seconds_keeps_both_owned_images_and_exact_parts(self):
        second, body = self._prepare_two_images()
        with patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", body)):
            previous = self._handoff_during_generation(6.1, "Що саме на другому фото?")
            result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertNotEqual(previous.turn_id, self.revision.turn_id)
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        parts = self.revision.generation_proposal["turn_intelligence"]["media_request"]["submitted_parts"]
        self.assertEqual([row["source_message_id"] for row in parts], [self.source.pk, second.pk])
        self.assertEqual([row["message_id"] for row in self.revision.generation_proposal["sources"]],
                         [self.source.pk, second.pk, self.caption.pk])
        self.assertEqual(self.revision.quiet_cap_at, previous.quiet_cap_at)
        self.assertIn(self.caption.text, str(self.generation_calls[-1]))
