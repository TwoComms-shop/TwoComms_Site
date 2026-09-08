from dataclasses import replace

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from management import tests_ig_revision_proposal as revision_fixtures
from management import tests_ig_analysis_v2_projector as analysis_fixtures
from management.models import (IgAnalysisProposal, IgClient, IgCommercialEpisode,
    IgConversationRouteDecision, IgCustomerTurn, IgCustomerTurnRevision,
    IgFunnelNodeState, IgFunnelResetAudit, IgTurnMessage, InstagramBotMessage,
    InstagramBotSettings)
from management.services.ig_conversation_routes import (AnalysisRouteSource,
    RevisionRouteSource, accept_customer_routes, conversation_route_reset_floor)
from management.services.ig_revision_proposal import _snapshot_sources
from management.services.ig_turn_revisions import (create_collecting_revision,
    claim_revision_preparation, seal_revision, claim_sealed_revision)


class ConversationRouteDecisionTests(TestCase):
    # Reuse real sealed-source + canonical winner fixture, not mocked authority.
    setUp = revision_fixtures.RevisionGenerationProposalTests.setUp
    _digest = staticmethod(revision_fixtures.RevisionGenerationProposalTests._digest)
    _policy_manifest = revision_fixtures.RevisionGenerationProposalTests._policy_manifest
    _create_generation_graph = revision_fixtures.RevisionGenerationProposalTests._create_generation_graph

    def _seal_routes(self, *intents, focus=None, binding_overrides=None):
        value = {"schema_version": "customer-route.v1", "intents": [
            {"kind": kind, "subtype": subtype, "operation": operation,
             "evidence_message_ids": [self.source.pk], "confidence": 0.9}
            for kind, subtype, operation in intents], "focus_index": focus}
        binding = {"schema_version": "route-source.v1", "settings_id": self.settings.pk,
            "settings_permission_epoch": self.settings.reply_permission_epoch,
            "client_permission_epoch": self.client_row.reply_permission_epoch,
            "reset_floor": conversation_route_reset_floor(self.client_row.pk),
            "watermark_message_id": self.source.pk, "input_digest": self.revision.snapshot_digest}
        binding.update(binding_overrides or {})
        # Future writer contract: insert ONCE with the already verified source
        # and winner. Current production writer intentionally has no route hook.
        proposal = {"schema_version": 1, "route_binding": binding,
            "customer_routes": value, "sources": _snapshot_sources(self.revision)[0],
            "execution_binding": {"settings_id": self.settings.pk,
                "settings_permission_epoch": self.settings.reply_permission_epoch},
            "policy_manifest": self._policy_manifest(), "generation": {
                "request_id": self.request_id, "actual_model": self.model}}
        self.revision.generation_proposal = proposal
        self.revision.generation_proposal_digest = self._digest(proposal)
        self.revision.generation_proposed_at = timezone.now()
        self.revision.save(update_fields=["generation_proposal", "generation_proposal_digest",
            "generation_proposed_at", "updated_at"])
        return RevisionRouteSource(self.client_row.pk, self.settings.pk,
            self.revision.pk, self.token, self.revision.generation_proposal_digest)

    def _next_turn(self):
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(
            active_slot=None, state=IgCustomerTurnRevision.State.SUPERSEDED)
        self.source = InstagramBotMessage.objects.create(client=self.client_row,
            sender_id=self.client_row.igsid, provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER, text="Уточнюю свій запит",
            status=InstagramBotMessage.Status.PENDING)
        turn = IgCustomerTurn.objects.create(client=self.client_row,
            primary_source_message=self.source, window_started_at=timezone.now(),
            window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=self.source, ordinal=1, role=self.source.role)
        revision = create_collecting_revision(turn, [self.source], bypass_quiet=True).revision
        preparation = claim_revision_preparation(revision.pk)
        seal_revision(revision.pk, preparation.token)
        claim = claim_sealed_revision(revision.pk)
        self.revision, self.token = claim.revision, claim.token
        self.request_id = f"route-request-{self.revision.pk}"
        self._create_generation_graph()

    def test_multi_intent_focus_journal_does_not_create_commerce_or_funnel_states(self):
        source = self._seal_routes(("employment", "none", "open"),
            ("collaboration", "designer", "open"), focus=1)
        result = accept_customer_routes(source, expected_previous_decision_id=None)
        self.assertTrue(result.created, result.reason_code)
        row = IgConversationRouteDecision.objects.get(pk=result.decision_id)
        self.assertEqual({x["key"] for x in row.active_intents},
            {"employment:none", "collaboration:designer"})
        self.assertEqual(row.focus_key, "collaboration:designer")
        self.assertIsNone(row.previous_id)
        self.assertEqual([x["operation"] for x in row.transitions], ["open", "open", "focus"])
        self.assertFalse(IgCommercialEpisode.objects.exists())
        self.assertFalse(IgFunnelNodeState.objects.exists())

    def test_replay_is_idempotent_and_journal_is_immutable(self):
        source = self._seal_routes(("employment", "none", "open"))
        first = accept_customer_routes(source, expected_previous_decision_id=None)
        replay = accept_customer_routes(source, expected_previous_decision_id=None)
        self.assertEqual(first.decision_id, replay.decision_id)
        self.assertFalse(replay.created)
        row = IgConversationRouteDecision.objects.get(pk=first.decision_id)
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            IgConversationRouteDecision.objects.filter(pk=row.pk).update(focus_key="catalog:none")
        with self.assertRaises(ValidationError):
            row.delete()

    def test_absence_preserves_other_intent_and_explicit_withdrawal_and_correction_append(self):
        first = accept_customer_routes(self._seal_routes(("employment", "none", "open"),
            ("collaboration", "designer", "open")), expected_previous_decision_id=None)
        self._next_turn()
        second = accept_customer_routes(self._seal_routes(("employment", "none", "continue")),
            expected_previous_decision_id=first.decision_id)
        self.assertTrue(second.created, second.reason_code)
        row = IgConversationRouteDecision.objects.get(pk=second.decision_id)
        self.assertEqual(len(row.active_intents), 2)
        self.assertEqual(row.transitions, [])
        self._next_turn()
        third = accept_customer_routes(self._seal_routes(("employment", "none", "withdraw"),
            ("collaboration", "designer", "correct")), expected_previous_decision_id=second.decision_id)
        self.assertTrue(third.created, third.reason_code)
        row = IgConversationRouteDecision.objects.get(pk=third.decision_id)
        self.assertEqual(row.sequence, 3)
        self.assertEqual([x["key"] for x in row.active_intents], ["collaboration:designer"])
        self.assertEqual([x["operation"] for x in row.transitions], ["withdraw", "correct"])
        self.assertEqual(row.reason_code, "customer_correction")

    def test_compare_and_swap_prevents_competing_source(self):
        first = accept_customer_routes(self._seal_routes(("employment", "none", "open")),
            expected_previous_decision_id=None)
        self._next_turn()
        source = self._seal_routes(("support", "none", "open"))
        stale = accept_customer_routes(source, expected_previous_decision_id=None)
        self.assertEqual(stale.reason_code, "previous_decision_changed")
        self.assertEqual(IgConversationRouteDecision.objects.count(), 1)
        self.assertTrue(accept_customer_routes(source,
            expected_previous_decision_id=first.decision_id).created)

    def test_purchase_episode_does_not_close_client_conversation_route(self):
        first = accept_customer_routes(self._seal_routes(("employment", "none", "open")),
            expected_previous_decision_id=None)
        self._next_turn()
        episode = IgCommercialEpisode.objects.create(client=self.client_row,
            sequence=1, open_slot=1, materialization_key="route-existing-generic-episode",
            opened_watermark_message_id=self.source.pk)
        self.client_row.current_commercial_episode = episode
        self.client_row.save(update_fields=["current_commercial_episode", "updated_at"])
        result = accept_customer_routes(self._seal_routes(("catalog", "none", "open")),
            expected_previous_decision_id=first.decision_id)
        self.assertTrue(result.created, result.reason_code)
        row = IgConversationRouteDecision.objects.get(pk=result.decision_id)
        self.assertEqual(row.sequence, 2)
        self.assertEqual({item["kind"] for item in row.active_intents}, {"employment", "catalog"})
        self.assertEqual(IgCommercialEpisode.objects.count(), 1)

    def test_new_inbound_makes_source_stale(self):
        source = self._seal_routes(("employment", "none", "open"))
        InstagramBotMessage.objects.create(client=self.client_row, role="user", text="Новий запит")
        self.assertEqual(accept_customer_routes(source, expected_previous_decision_id=None).reason_code,
            "source_watermark_stale")

    def test_permission_epoch_cannot_be_reconstructed_at_acceptance(self):
        source = self._seal_routes(("employment", "none", "open"),
            binding_overrides={"client_permission_epoch": self.client_row.reply_permission_epoch - 1})
        self.assertEqual(accept_customer_routes(source, expected_previous_decision_id=None).reason_code,
            "source_binding_mismatch")

    def test_reset_and_takeover_block_acceptance(self):
        source = self._seal_routes(("employment", "none", "open"))
        IgFunnelResetAudit.objects.create(client=self.client_row,
            reset_after_message_id=self.source.pk, reason="test reset")
        self.assertEqual(accept_customer_routes(source, expected_previous_decision_id=None).reason_code,
            "reset_floor_changed")
        IgClient.objects.filter(pk=self.client_row.pk).update(manager_takeover=True)
        self.assertEqual(accept_customer_routes(source, expected_previous_decision_id=None).reason_code,
            "manager_takeover")

    def test_foreign_revision_token_and_digest_are_rejected(self):
        source = self._seal_routes(("employment", "none", "open"))
        other = IgClient.objects.create(igsid="foreign-route-client")
        for invalid, reason in (
            (replace(source, client_id=other.pk), "source_not_owned"),
            (replace(source, revision_token="wrong"), "revision_not_current"),
            (replace(source, expected_source_digest="a" * 64), "source_digest_invalid"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(accept_customer_routes(invalid,
                    expected_previous_decision_id=None).reason_code, reason)
        self.assertFalse(IgConversationRouteDecision.objects.exists())

    def test_customer_evidence_content_and_role_mutation_fail_closed(self):
        source = self._seal_routes(("employment", "none", "open"))
        # Source content/role mutation after sealing must not become new evidence.
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="")
        self.assertEqual(accept_customer_routes(source, expected_previous_decision_id=None).reason_code,
            "source_content_changed")
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(role="manager")
        self.assertFalse(accept_customer_routes(source, expected_previous_decision_id=None).accepted)

    def test_withdraw_does_not_fabricate_a_previous_route(self):
        source = self._seal_routes(("employment", "none", "withdraw"))
        self.assertEqual(accept_customer_routes(source, expected_previous_decision_id=None).reason_code,
            "route_not_active")
        self.assertFalse(IgConversationRouteDecision.objects.exists())


class AnalysisConversationRouteBoundaryTests(TestCase):
    _proposal = analysis_fixtures.AnalysisV2ProjectorTests._proposal

    def setUp(self):
        analysis_fixtures.AnalysisV2ProjectorTests.setUp(self)
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True)

    def test_old_analysis_is_not_automatically_accepted(self):
        self._proposal(IgAnalysisProposal.ProposalType.OPEN_SUBFUNNEL, {})
        source = AnalysisRouteSource(self.client_row.pk, self.settings.pk,
            self.result.pk, self.result.result_digest)
        result = accept_customer_routes(source, expected_previous_decision_id=None)
        self.assertEqual(result.reason_code, "source_binding_missing")
        self.assertFalse(IgConversationRouteDecision.objects.exists())

    def test_foreign_result_is_rejected(self):
        other = IgClient.objects.create(igsid="foreign-analysis-route")
        source = AnalysisRouteSource(other.pk, self.settings.pk,
            self.result.pk, self.result.result_digest)
        result = accept_customer_routes(source, expected_previous_decision_id=None)
        self.assertEqual(result.reason_code, "source_not_owned")
