"""Real ingress handoffs preserve owed sources without replaying settled work."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_turn_revisions as fixtures
from management.models import IgClient, IgCustomerTurnRevision, IgFollowUpTask, InstagramBotMessage
from management.services.ig_customer_turns import ensure_turn_for_inbound, MAX_TURN_WAIT
from management.services.ig_revision_execution import (
    due_revision_ids, expired_revision_debt_ids, prepare_revision, record_expired_revision_debt,
)
from management.services.ig_revision_source_coverage import validate_source_transfer
from management.services.ig_turn_revisions import (
    claim_revision_preparation, claim_sealed_revision, seal_revision, revision_claim_is_current,
)


@override_settings(IG_REVISION_EXECUTION_ENABLED=True, IG_REVISION_EXECUTION_CUTOVER_AT="2026-01-01T00:00:00+00:00")
class RevisionSourceCoverageTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.customer = IgClient.objects.create(igsid="source-transfer-client", reply_permission_epoch=7)
        self.sequence = 0

    def incoming(self, text="Яка ціна?", *, seconds=0, media=None, **fields):
        self.sequence += 1
        message = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, role="user", source="webhook",
            mid=f"coverage-{self.sequence}", text=text, status="pending",
            provider_namespace="instagram_login:owner-1", attachment_media=media or [], **fields,
        )
        attached = ensure_turn_for_inbound(message, now=self.now + timedelta(seconds=seconds))
        revision = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        return message, revision

    def claim(self, revision, *, seconds=1.5):
        now = self.now + timedelta(seconds=seconds)
        preparation = claim_revision_preparation(revision.pk, now=now)
        self.assertTrue(preparation.token, preparation.reason)
        sealed = seal_revision(revision.pk, preparation.token, now=now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(revision.pk, now=now)
        self.assertTrue(claimed.token, claimed.reason)
        revision.refresh_from_db()
        return claimed.token

    def assert_sources(self, revision, *messages):
        self.assertEqual(list(revision.sources.order_by("ordinal").values_list("message_id", flat=True)),
                         [message.pk for message in messages])

    def assert_denied(self, predecessor, successor, fresh):
        self.assert_sources(successor, fresh)
        predecessor.refresh_from_db()
        self.assertIsNone(predecessor.active_slot)
        self.assertNotIn("source_transfer_out", predecessor.action_receipts)
        self.assertNotIn("source_transfer_in", successor.action_receipts)
        self.assertNotIn(predecessor.pk, due_revision_ids(now=self.now + timedelta(seconds=10)))

    def test_claimed_burst_chain_preserves_root_clocks_and_exact_source_receipts(self):
        first, root = self.incoming()
        token = self.claim(root)
        second, middle = self.incoming("Чорний", seconds=2)
        third, last = self.incoming("Розмір L", seconds=4)
        self.assert_sources(last, first, second, third)
        for revision in (middle, last):
            self.assertTrue(validate_source_transfer(revision))
            self.assertEqual(revision.quiet_started_at, root.quiet_started_at)
            self.assertEqual(revision.quiet_cap_at, root.quiet_cap_at)
            self.assertEqual(revision.overall_deadline, root.overall_deadline)
            self.assertEqual(revision.action_receipts["source_transfer_in"]["root_revision_id"], root.pk)
        self.assertEqual(last.quiet_deadline, root.quiet_cap_at)
        self.assertEqual(last.sources.get(message=first).source_digest, root.sources.get().source_digest)
        root.refresh_from_db()
        self.assertEqual(root.state, "superseded")
        self.assertEqual(root.claim_token, "")
        self.assertFalse(revision_claim_is_current(root.pk, token))
        first.refresh_from_db()
        self.assertEqual(first.status, "pending")
        self.assertFalse(root.turn.terminal_reason)

    def test_photo_caption_crosses_legacy_windows_without_rewriting_membership(self):
        first, root = self.incoming("", media=[{"provider_object_id": "photo-1", "type": "image", "url": "https://media.invalid/a"}])
        second, middle = self.incoming("Як на фото", seconds=6.1)
        third, last = self.incoming("Яка ціна?", seconds=15)
        self.assertNotEqual(root.turn_id, middle.turn_id)
        self.assertNotEqual(middle.turn_id, last.turn_id)
        self.assert_sources(last, first, second, third)
        self.assertTrue(validate_source_transfer(last))
        self.assertEqual(first.turn_membership.turn_id, root.turn_id)
        self.assertEqual(second.turn_membership.turn_id, middle.turn_id)
        original = root.sources.get()
        copied = last.sources.get(message=first)
        self.assertEqual(copied.discovered_media, original.discovered_media)
        self.assertEqual(copied.source_digest, original.source_digest)
        self.assertEqual(last.overall_deadline, root.overall_deadline)
        self.assertFalse(root.turn.terminal_reason)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_real_ingress_successor_prepares_all_owed_sources(self):
        first, root = self.incoming()
        self.claim(root)
        second, successor = self.incoming("Чорне худі", seconds=2)
        result = prepare_revision(successor.pk, lambda **kwargs: None, now=self.now + timedelta(seconds=4))
        self.assertTrue(result.ready, result.reason)
        successor.refresh_from_db()
        self.assertEqual([row["message_id"] for row in successor.bundle_snapshot["sources"]], [first.pk, second.pk])
        self.assertEqual(successor.quiet_cap_at, root.quiet_cap_at)

    def test_horizon_denial_remains_reconcilable_manager_debt_without_dispatch(self):
        first, root = self.incoming()
        self.claim(root)
        second, successor = self.incoming(seconds=MAX_TURN_WAIT.total_seconds() + 1)
        self.assert_denied(root, successor, second)
        expired = root.overall_deadline + timedelta(seconds=1)
        self.assertIn(root.pk, expired_revision_debt_ids(now=expired))
        self.assertEqual(record_expired_revision_debt(root.pk, now=expired), "generation_not_started")
        task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{root.pk}")
        self.assertEqual(task.manager_context["owner"], "manager")
        self.assertEqual(task.event_payload["source_message_ids"], [first.pk])
        self.assertNotIn(root.pk, expired_revision_debt_ids(now=expired))
        first.refresh_from_db()
        self.assertEqual(first.status, "pending")

    def test_capacity_denial_retains_all_old_sources_and_eventual_debt(self):
        first, root = self.incoming("а" * 40000)
        second, successor = self.incoming("б" * 30000, seconds=7)
        self.assert_denied(root, successor, second)
        self.assert_sources(root, first)
        self.assertEqual(successor.text_chars, 30000)
        expired = root.overall_deadline + timedelta(seconds=1)
        self.assertIn(root.pk, expired_revision_debt_ids(now=expired))
        self.assertEqual(record_expired_revision_debt(root.pk, now=expired), "preparation_expired")
        self.assertTrue(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{root.pk}").exists())

    def test_existing_delivery_parts_are_never_transferred(self):
        for state in ("planned", "sent", "unknown"):
            with self.subTest(state=state):
                self.customer = IgClient.objects.create(igsid=f"effect-{state}")
                first, root = self.incoming()
                self.claim(root)
                fixtures.TurnRevisionTests.delivery_effect(SimpleNamespace(client_row=self.customer, first=first), root, state=state)
                second, successor = self.incoming(seconds=2)
                self.assert_denied(root, successor, second)
                self.assert_sources(root, first)
                self.assertEqual(root.delivery_effects.get().state, state)

    def test_provider_started_and_manual_owned_predecessors_are_not_replayed(self):
        for receipt in ({"provider_execution_manifest": {"version": 1}}, {"response_debt": {"owner": "manager"}}):
            with self.subTest(receipt=receipt):
                self.customer = IgClient.objects.create(igsid=f"receipt-{len(IgClient.objects.all())}")
                first, root = self.incoming()
                root.action_receipts = receipt
                root.save(update_fields=["action_receipts", "updated_at"])
                second, successor = self.incoming(seconds=2)
                self.assert_denied(root, successor, second)
                self.assert_sources(root, first)

    def test_permission_pause_takeover_and_explicit_button_deny_transfer(self):
        for fields in ({"bot_paused": True}, {"manager_takeover": True}, {"reply_permission_epoch": 8}, {"opted_out_at": self.now}):
            with self.subTest(fields=fields):
                self.customer = IgClient.objects.create(igsid=f"permission-{self.sequence}", reply_permission_epoch=7)
                _first, root = self.incoming()
                IgClient.objects.filter(pk=self.customer.pk).update(**fields)
                second, successor = self.incoming(seconds=2)
                self.assert_denied(root, successor, second)
        self.customer = IgClient.objects.create(igsid="button-boundary")
        _first, root = self.incoming()
        second, successor = self.incoming(seconds=2, quick_reply_payload="OPT_OUT")
        self.assert_denied(root, successor, second)

    def test_legacy_worker_claim_cannot_be_revoked_by_canonical_transfer(self):
        from management.services.ig_customer_turns import claim_turn

        _first, root = self.incoming()
        _turn, legacy_token = claim_turn(root.turn_id, now=self.now)
        second, successor = self.incoming(seconds=2)
        self.assert_denied(root, successor, second)
        root.turn.refresh_from_db()
        self.assertEqual(root.turn.claim_state, "claimed")
        self.assertEqual(root.turn.claim_token, legacy_token)

    def test_changed_or_completed_source_cannot_reappear_from_native_turn_membership(self):
        for mutation in ({"text": "changed"}, {"status": "done"}, {"status": "processing"}, {"send_state": "unknown"}):
            with self.subTest(mutation=mutation):
                self.customer = IgClient.objects.create(igsid=f"mutation-{self.sequence}")
                first, root = self.incoming()
                InstagramBotMessage.objects.filter(pk=first.pk).update(**mutation)
                second, middle = self.incoming(seconds=2)
                self.assert_denied(root, middle, second)
                third, successor = self.incoming(seconds=3)
                self.assert_sources(successor, second, third)
                self.assertTrue(validate_source_transfer(successor))

    def test_duplicate_incoming_does_not_fork_or_duplicate_handoff(self):
        _first, root = self.incoming()
        second, successor = self.incoming(seconds=2)
        receipt = successor.action_receipts["source_transfer_in"]
        repeat = ensure_turn_for_inbound(second, now=self.now + timedelta(seconds=3))
        self.assertEqual(repeat.reason, "already_attached")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 2)
        successor.refresh_from_db()
        root.refresh_from_db()
        self.assertEqual(successor.action_receipts["source_transfer_in"], receipt)
        self.assertEqual(root.action_receipts["source_transfer_out"], receipt)

    def test_failed_pair_transaction_preserves_original_claim_and_memberships(self):
        first, root = self.incoming()
        token = self.claim(root)
        with patch("management.services.ig_revision_source_coverage.record_source_transfer", side_effect=RuntimeError("pair_failed")):
            with self.assertRaisesMessage(RuntimeError, "pair_failed"):
                self.incoming(seconds=7)
        root.refresh_from_db()
        self.assertEqual(root.active_slot, 1)
        self.assertEqual(root.claim_token, token)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)
        self.assertEqual(self.customer.customer_turns.count(), 1)
        self.assert_sources(root, first)

    def test_provider_event_clock_does_not_move_local_ingress_deadlines(self):
        self.sequence += 1
        message = InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, role="user", source="webhook",
            mid="delayed-provider-clock", text="Яка ціна?", provider_created_at=self.now - timedelta(hours=1),
        )
        with patch("management.services.ig_customer_turns.timezone.now", return_value=self.now):
            attached = ensure_turn_for_inbound(message)
        revision = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        self.assertEqual(revision.quiet_started_at, self.now)
        self.assertEqual(revision.sources.get().provider_created_at, message.provider_created_at)

    @override_settings(IG_REVISION_EXECUTION_ENABLED=False)
    def test_disabled_initial_shadow_remains_legacy_grouped_and_claimable(self):
        from management.services.ig_revision_live import legacy_claimable_messages

        first, root = self.incoming()
        second, successor = self.incoming(seconds=2)
        self.assert_sources(successor, first, second)
        self.assertNotIn("source_transfer_in", successor.action_receipts)
        self.assertEqual(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk__in=[first.pk, second.pk])).count(), 2)

    def test_rollout_flip_keeps_accepted_transfer_and_next_source_canonically_owned(self):
        from management.services.ig_revision_live import legacy_claimable_messages, revision_owned_turn_ids

        first, root = self.incoming()
        second, middle = self.incoming(seconds=7)
        with override_settings(IG_REVISION_EXECUTION_ENABLED=False):
            self.assertFalse(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk__in=[first.pk, second.pk])).exists())
            self.assertTrue(revision_owned_turn_ids().filter(pk=root.turn_id).exists())
            third, successor = self.incoming(seconds=9)
            self.assert_sources(successor, first, second, third)
            self.assertTrue(validate_source_transfer(successor))
            self.assertFalse(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk__in=[first.pk, second.pk, third.pk])).exists())
            self.assertIn(successor.pk, due_revision_ids(now=self.now + timedelta(seconds=11), cutover_at=self.now + timedelta(days=1)))

    @override_settings(IG_REVISION_EXECUTION_CUTOVER_AT="2099-01-01T00:00:00+00:00")
    def test_out_of_cutover_shadow_never_acquires_transfer_ownership(self):
        from management.services.ig_revision_live import legacy_claimable_messages

        first, root = self.incoming()
        second, successor = self.incoming(seconds=7)
        self.assertNotIn("source_transfer_in", successor.action_receipts)
        self.assert_sources(successor, second)
        self.assertTrue(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=first.pk)).exists())

    def test_real_ingress_union_reaches_provider_proposal_and_sent_source_projection(self):
        from management import tests_ig_revision_live as live_fixture

        case = live_fixture.RevisionLiveTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        predecessor = case.revision
        original_digest = predecessor.sources.get().source_digest
        fresh = case._message("Підкажіть ціну цього худі", "coverage-live-fresh")
        attached = ensure_turn_for_inbound(fresh)
        case.revision = IgCustomerTurnRevision.objects.get(pk=attached.revision_id)
        prepared = prepare_revision(case.revision.pk, lambda **kwargs: None, now=timezone.now() + timedelta(seconds=2))
        self.assertTrue(prepared.ready, prepared.reason)
        case.token = prepared.execution_token
        case.revision.refresh_from_db()
        self.assertTrue(validate_source_transfer(case.revision))
        result, generation, http = case._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(http.call_count, 1)
        case.revision.refresh_from_db()
        proposal_sources = case.revision.generation_proposal["sources"]
        self.assertEqual([row["message_id"] for row in proposal_sources], [case.source.pk, fresh.pk])
        self.assertEqual(proposal_sources[0]["source_digest"], original_digest)
        case.source.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(case.source.status, "done")
        self.assertEqual(fresh.status, "done")
        predecessor.refresh_from_db()
        self.assertEqual(predecessor.state, "superseded")
