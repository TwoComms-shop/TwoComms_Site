"""Complaint debt keeps its own sources and only independently proved scope."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    IgAnalysisProposal, IgBotNotification, IgCommercialEpisode, IgConversationAnalysisJob,
    IgConversationAnalysisResult, IgConversationAnalysisSnapshot, IgConversationRouteDecision, IgFollowUpTask,
)
from management.services.ig_media_analysis import bind_media_analysis
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS, build_revision_authority_bindings,
)
from management.services.ig_revision_intents import (
    ensure_revision_manager_case, validated_legacy_media_complaint_evidence,
)
from management.services.ig_response_control import parse_structured_response
from management.services.ig_turn_intent import (
    _case_scope, _disjoint_scopes, media_complaint_source_scope, purpose_blockers,
)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class MediaComplaintCaseScopeTests(TransactionTestCase):
    def setUp(self):
        from management.tests_ig_revision_proposal import RevisionGenerationProposalTests
        self.fixture = RevisionGenerationProposalTests(methodName="runTest")
        self.fixture.setUp()

    def response(self):
        return parse_structured_response({"reply_text": "Допоможемо з цим питанням.",
            "controls": [{"kind": "manager", "value": True}],
            "turn_intelligence": {"catalog_candidates": [], "transcript": "",
                "audio_status": "not_applicable", "intent": "media_review", "confidence": .9,
                "image_observations": [{"source_image_index": 0, "outcome": "understood",
                    "evidence_code": "visual_content", "type_code": "product"}],
                "media_observations": [{"source_inline_index": 0, "outcome": "understood",
                    "content_kind": "wearing", "sentiment": "negative", "confidence": .9,
                    "evidence_code": "visual_content", "evidence": "Customer reports a torn seam",
                    "complaint_code": "current_customer_complaint"}]}})

    def store_complaint(self):
        from management.services.ig_revision_proposal import _request_media_projection
        f = self.fixture
        media, reason = _request_media_projection(f.revision, f._media_manifest())
        self.assertFalse(reason)
        response = self.response()
        intelligence = f._intelligence()
        intelligence["media_analysis"] = bind_media_analysis(parts=media["items"],
            observations=response.turn_intelligence.media_observations, actual_inline_count=1,
            actual_content_hashes=media["actual_content_hashes"], request_id=f.request_id,
            provider_model=f.model, legacy_images=intelligence["image_observations"],
            capture_outcomes=media["outcomes"])
        authority = build_revision_authority_bindings(f.client_row, claims=(CLAIM_PUBLIC_POLICY_INPUTS,),
            settings_obj=f.settings, server_authorized_actions=("manager_escalation_intent",))
        stored = f._store(response=response, turn_intelligence=intelligence, authority=authority)
        self.assertTrue(stored.created, stored.reasons)
        f.revision.refresh_from_db()

    def ensure(self):
        f = self.fixture
        return ensure_revision_manager_case(f.revision.pk, f.token, settings_id=f.settings.pk)

    def episode(self, sequence, *, current=False):
        f = self.fixture
        episode = IgCommercialEpisode.objects.create(client=f.client_row, sequence=sequence,
            open_slot=1 if current else None, materialization_key=f"media-case-scope:{sequence}")
        if current:
            f.client_row.current_commercial_episode = episode
            f.client_row.save(update_fields=["current_commercial_episode", "updated_at"])
        return episode

    def unsupported_analysis_scope(self, episode):
        """Prove the current native producer abstains until route schema support exists."""
        from management.services import ig_analysis_v2 as v2
        from management.services.ig_conversation_routes import (
            AnalysisRouteSource, accept_customer_routes, conversation_route_reset_floor,
        )
        from management.services.ig_customer_route_contract import SCHEMA_VERSION
        f = self.fixture
        # This is the episode current when the complaint source is analyzed.
        # Tests advance to a new episode only after accepting this old scope.
        f.client_row.current_commercial_episode = episode
        f.client_row.save(update_fields=["current_commercial_episode", "updated_at"])
        fingerprint = f._digest({"client_id": f.client_row.pk, "message_id": f.source.pk,
            "episode_id": episode.pk, "text": f.source.text})
        artifact_digest = f._digest({"source_digest": f.revision.sources.get(message=f.source).source_digest,
            "episode_id": episode.pk})
        materiality_digest = f._digest({"artifact_digest": artifact_digest, "event_highwater": 1})
        authority_digest = f._digest({"episode_id": episode.pk, "authority": "interpretation_only"})
        IgConversationAnalysisJob.objects.create(client=f.client_row,
            watermark_message_id=f.source.pk, revision=2, status=IgConversationAnalysisJob.Status.DONE,
            due_at=timezone.now(), next_attempt_at=timezone.now(), materiality_episode=episode,
            materiality_event_highwater=1, materiality_digest=materiality_digest,
            authority_digest=authority_digest, artifact_digest=artifact_digest,
            required_state_fingerprint=fingerprint)
        snapshot = IgConversationAnalysisSnapshot.objects.create(client=f.client_row,
            last_analyzed_message=f.source, dedupe_key="media-case-scope:snapshot",
            commercial_episode=episode,
            score_band=IgConversationAnalysisSnapshot.Band.EXPLORING,
            interaction_type=IgConversationAnalysisSnapshot.InteractionType.PRODUCT_INTEREST,
            required_state_fingerprint=fingerprint)
        result_key = "analysis-v2:" + v2._sha({"snapshot": snapshot.dedupe_key,
            "watermark": f.source.pk, "revision": 2, "materiality_event_highwater": 1,
            "materiality_digest": materiality_digest, "schema": v2.RESULT_SCHEMA_VERSION})
        result = IgConversationAnalysisResult(result_key=result_key,
            legacy_snapshot=snapshot, client=f.client_row, commercial_episode=episode,
            watermark_message_id=f.source.pk, job_revision=2, materiality_event_highwater=1,
            materiality_digest=materiality_digest, authority_digest=authority_digest,
            artifact_digest=artifact_digest, state_correlation=v2.state_correlation(fingerprint),
            result_schema_version=v2.RESULT_SCHEMA_VERSION, normalizer_version=v2.NORMALIZER_VERSION,
            interaction_type=snapshot.interaction_type, score_band=snapshot.score_band,
            evidence_manifest=[{"message_id": f.source.pk, "source_role": "user", "claim_codes": ["interaction"]}],
            customer_evidence_count=1,
            result_digest="", analyzed_at=timezone.now())
        result.result_digest = v2.result_digest_for_instance(result)
        result.save()
        route_binding = {"schema_version": "route-source.v1", "settings_id": f.settings.pk,
            "settings_permission_epoch": f.settings.reply_permission_epoch,
            "client_permission_epoch": f.client_row.reply_permission_epoch,
            "reset_floor": conversation_route_reset_floor(f.client_row.pk),
            "watermark_message_id": f.source.pk, "input_digest": artifact_digest}
        proposal = IgAnalysisProposal(proposal_key="", analysis_result=result, ordinal=1,
            client=f.client_row, commercial_episode=episode, proposal_type=IgAnalysisProposal.ProposalType.OPEN_SUBFUNNEL,
            target_scope=IgAnalysisProposal.TargetScope.CLIENT, target_definition_version=SCHEMA_VERSION,
            typed_value={"kind": "support", "subtype": "none", "operation": "open",
                "evidence_message_ids": [f.source.pk], "confidence": .9, "focus": True,
                "route_binding": route_binding}, evidence_message_ids=[f.source.pk], confidence=Decimal(".9000"),
            source_result_digest=result.result_digest, expected_materiality_digest=materiality_digest,
            expected_authority_digest=authority_digest, expected_state_correlation=result.state_correlation)
        proposal.proposal_key = v2.proposal_key_for_instance(proposal)
        unsupported_key = proposal.proposal_key
        unsupported_value = deepcopy(proposal.typed_value)
        with self.assertRaises(ValidationError):
            proposal.save()
        # The supported persisted OPEN_SUBFUNNEL shape is empty. It cannot
        # provide a route binding and the real route producer must abstain.
        proposal.typed_value = {}
        proposal.proposal_key = v2.proposal_key_for_instance(proposal)
        proposal.save()
        abstained = accept_customer_routes(AnalysisRouteSource(f.client_row.pk, f.settings.pk,
            result.pk, result.result_digest), expected_previous_decision_id=None)
        self.assertFalse(abstained.accepted)
        self.assertEqual(abstained.reason_code, "source_binding_missing")
        self.assertFalse(IgConversationRouteDecision.objects.exists())
        # Also exercise the installed native trigger against an attempted
        # unsupported insert. It must reject even outside model validation;
        # no guard, immutable record or accepted journal is bypassed/changed.
        fields = [field for field in IgAnalysisProposal._meta.local_concrete_fields
            if not field.primary_key]
        quote = connection.ops.quote_name
        params, expressions = [], []
        for field in fields:
            if field.name == "proposal_key":
                expressions.append("%s")
                params.append(unsupported_key)
            elif field.name == "typed_value":
                expressions.append("%s")
                params.append(json.dumps(unsupported_value))
            else:
                expressions.append(quote(field.column))
        params.append(proposal.pk)
        table = quote(IgAnalysisProposal._meta.db_table)
        sql = f"INSERT INTO {table} ({', '.join(quote(field.column) for field in fields)}) " \
            f"SELECT {', '.join(expressions)} FROM {table} WHERE id=%s"
        with self.assertRaisesRegex(DatabaseError, "IgAnalysisProposal insert guard"):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
        self.assertEqual(IgAnalysisProposal.objects.count(), 1)
        return result

    def later_decision(self, episode_id):
        f = self.fixture
        return {"source_message_ids": [f.source.pk + 1], "commerce_evidence_refs": [f.source.pk + 1],
            "source_scope": {"source_message_ids": [f.source.pk + 1], "route_kinds": ["catalog"],
                "commercial_episode_id": episode_id}, "purpose": "retail"}

    def test_complaint_never_overwrites_existing_generic_manager_case_and_replays_once(self):
        f = self.fixture
        old_context = {"case_kind": "customer_manager_request", "sources": [{"message_id": 999}],
            "required_decisions": ["customer_request"], "operator_note": "Keep this request"}
        old = IgFollowUpTask.objects.create(client=f.client_row, due_at=timezone.now(),
            kind="manager_task", status="skipped", reason="revision_case:manager_handoff",
            event_key="existing-request", manager_context=deepcopy(old_context))
        self.store_complaint()
        first = self.ensure()
        self.assertTrue(first.ready, first.reason)
        task = IgFollowUpTask.objects.get(pk=first.task_id)
        old.refresh_from_db()
        self.assertNotEqual(task.pk, old.pk)
        self.assertEqual(old.manager_context, old_context)
        self.assertEqual(task.reason, "revision_case:media_complaint")
        self.assertEqual(task.manager_context["media_complaint_scope"], {})
        repeated = self.ensure()
        self.assertTrue(repeated.replayed)
        self.assertEqual((repeated.task_id, repeated.notification_id), (first.task_id, first.notification_id))
        self.assertEqual(IgBotNotification.objects.count(), 1)

    def test_other_complaint_case_retains_its_own_sources(self):
        f = self.fixture
        previous_context = {"case_kind": "media_complaint_review", "sources": [{"message_id": 998}],
            "media_complaint_evidence": [{"content_hash": "old-evidence"}]}
        previous = IgFollowUpTask.objects.create(client=f.client_row, due_at=timezone.now(),
            kind="manager_task", status="skipped", reason="revision_case:media_complaint",
            event_key="different-complaint-source", manager_context=deepcopy(previous_context))
        self.store_complaint()
        result = self.ensure()
        self.assertTrue(result.ready, result.reason)
        self.assertNotEqual(result.task_id, previous.pk)
        previous.refresh_from_db()
        self.assertEqual(previous.manager_context, previous_context)

    def test_explicit_manager_request_cannot_hide_accepted_complaint(self):
        self.store_complaint()
        with patch("management.services.ig_revision_intents.manager_case_reason",
            return_value="customer_manager_request"):
            result = self.ensure()
        self.assertTrue(result.ready, result.reason)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        self.assertEqual(task.reason, "revision_case:media_complaint")
        self.assertTrue(task.manager_context["media_complaint_evidence"])
        self.assertIn("customer_service_review", task.manager_context["required_decisions"])

    def test_custom_decision_keeps_complaint_debt_additively_without_other_case_overwrite(self):
        f = self.fixture
        old_context = {"case_kind": "custom_print", "operator_note": "Unrelated design decision"}
        old = IgFollowUpTask.objects.create(client=f.client_row, due_at=timezone.now(),
            kind="manager_task", status="skipped", reason="revision_case:custom_print",
            event_key="other-custom-source", manager_context=deepcopy(old_context))
        self.store_complaint()
        with patch("management.services.ig_revision_intents.manager_case_reason", return_value="custom_print"):
            result = self.ensure()
        self.assertTrue(result.ready, result.reason)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        self.assertNotEqual(task.pk, old.pk)
        old.refresh_from_db()
        self.assertEqual(old.manager_context, old_context)
        self.assertEqual(task.manager_context["case_kind"], "custom_print")
        self.assertTrue(task.manager_context["media_complaint_evidence"])
        self.assertEqual(task.manager_context["required_decisions"],
            ["design_feasibility", "quote_approval", "customer_service_review"])
        self.assertEqual(_case_scope(f.client_row, task), ("unknown", {}))

    def test_missing_independent_scope_never_guesses_current_or_historical_episode(self):
        f = self.fixture
        self.episode(1)
        current = self.episode(2, current=True)
        self.store_complaint()
        result = self.ensure()
        self.assertTrue(result.ready, result.reason)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        self.assertEqual(task.manager_context["media_complaint_scope"], {})
        self.assertEqual(_case_scope(f.client_row, task), ("unknown", {}))
        self.assertEqual(purpose_blockers(f.client_row, self.later_decision(current.pk)), "pending_manager_case")

    def test_native_analysis_route_dependency_abstains_and_does_not_unblock_complaint(self):
        f = self.fixture
        old_episode = self.episode(1)
        self.unsupported_analysis_scope(old_episode)
        current = self.episode(2, current=True)
        self.store_complaint()
        result = self.ensure()
        self.assertTrue(result.ready, result.reason)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        case_kind, scope = _case_scope(f.client_row, task)
        self.assertEqual((case_kind, scope), ("unknown", {}))
        self.assertEqual(purpose_blockers(f.client_row, self.later_decision(current.pk)), "pending_manager_case")
        self.assertEqual(purpose_blockers(f.client_row, self.later_decision(old_episode.pk)), "pending_manager_case")
        self.assertEqual(purpose_blockers(f.client_row, self.later_decision(None)), "pending_manager_case")
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertEqual(task.manager_approval_status, "pending")
        self.assertEqual(IgBotNotification.objects.get(pk=result.notification_id).status, "pending")
        # Conditional adapter logic only: this does not claim that a native
        # accepted analysis route can currently produce the required scope.
        old_scope = {"source_message_ids": [f.source.pk], "commercial_episode_id": old_episode.pk}
        self.assertTrue(_disjoint_scopes(self.later_decision(current.pk)["source_scope"], old_scope,
            episodes_only=True))
        self.assertFalse(_disjoint_scopes(self.later_decision(old_episode.pk)["source_scope"], old_scope,
            episodes_only=True))

    def test_scope_or_current_source_tampering_keeps_debt_blocking(self):
        f = self.fixture
        episode = self.episode(1)
        self.store_complaint()
        result = self.ensure()
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        task.manager_context["media_complaint_scope"] = {"version": "media-complaint-scope.v1",
            "commercial_episode_id": episode.pk, "route_decision_id": 999,
            "route_decision_digest": "d" * 64, "analysis_result_id": 999,
            "source_message_ids": [f.source.pk], "route_kinds": ["support"],
            "reset_floor": 1, "permission_epoch": f.revision.permission_epoch, "order_id": None}
        task.save(update_fields=["manager_context", "updated_at"])
        self.assertEqual(_case_scope(f.client_row, task), ("unknown", {}))
        self.assertEqual(purpose_blockers(f.client_row, self.later_decision(episode.pk + 2)), "pending_manager_case")
        task.manager_context["media_complaint_scope"] = media_complaint_source_scope(f.client_row,
            f.revision, [f.source.pk])
        task.save(update_fields=["manager_context", "updated_at"])
        f.source.text = "Edited after complaint was queued"
        f.source.save(update_fields=["text"])
        self.assertEqual(_case_scope(f.client_row, task), ("unknown", {}))

    def legacy_artifact(self):
        from management.services import instagram_bot as bot
        f = self.fixture
        binding = bot._source_media_binding(f.source, [{"data": f.body, "mime": "image/jpeg",
            "source_part_id": f.part_id, "original_index": 0, "identity_origin": "ingress"}])
        normalized = bot._normalize_turn_media_binding([("image/jpeg", f.body)], binding)
        normalized.update(actual_inline_count=1, actual_content_hashes=[f.content_hash],
            request_id=f.request_id, provider_model=f.model)
        artifact = bot._validated_turn_intelligence(self.response().turn_intelligence, {}, normalized)
        artifact["request_permission_epoch"] = f.client_row.reply_permission_epoch
        return artifact

    def test_legacy_exact_submitted_request_proof_and_mutations(self):
        f = self.fixture
        artifact = self.legacy_artifact()
        evidence, reason = validated_legacy_media_complaint_evidence(f.source, artifact)
        self.assertFalse(reason)
        self.assertEqual(evidence[0]["content_hash"], f.content_hash)
        for mutate in (
            lambda request: request.pop("actual_content_hashes"),
            lambda request: request.update(actual_content_hashes=["f" * 64]),
            lambda request: request.update(actual_inline_count=True),
            lambda request: request.update(prepared_inline_count=2),
            lambda request: request.update(prepared_inline_count=True),
            lambda request: request.update(request_id="foreign-request"),
            lambda request: request.update(provider_model="foreign-model"),
            lambda request: request["submitted_parts"][0].update(content_hash="f" * 64),
            lambda request: request["submitted_parts"][0].update(source_part_id="mp1_" + "f" * 32),
        ):
            changed = deepcopy(artifact)
            mutate(changed["media_request"])
            self.assertEqual(validated_legacy_media_complaint_evidence(f.source, changed),
                ([], "media_complaint_proof_invalid"))
