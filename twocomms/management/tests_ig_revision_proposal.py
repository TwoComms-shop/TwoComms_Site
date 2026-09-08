import hashlib
import json
from copy import deepcopy

from django.test import TransactionTestCase
from django.utils import timezone

from management.models import (
    BotPolicyPublication,
    GeminiRequest,
    GeminiRequestAttempt,
    IgClient,
    IgCustomerTurn,
    IgCustomerTurnRevision,
    IgTurnMessage,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_response_control import ValidatedResponse
from management.services.gemini_accounting_contract import (
    sanitize_request_policy_manifest,
)
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS,
    build_revision_authority_bindings,
)
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_revision_proposal import (
    project_revision_image_inspections,
    store_revision_generation_proposal,
)
from management.services.ig_turn_revisions import (
    claim_revision_preparation,
    claim_sealed_revision,
    create_collecting_revision,
    seal_revision,
)


class RevisionGenerationProposalTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        publication_snapshot = {"schema_version": 1, "instructions": []}
        publication_hash = self._digest(publication_snapshot)
        self.publication = BotPolicyPublication.objects.create(
            version=1,
            kind=BotPolicyPublication.Kind.PUBLISH,
            schema_version=1,
            snapshot=publication_snapshot,
            snapshot_hash=publication_hash,
            compiler_version="instruction-set-v1",
            instruction_count=0,
        )
        self.settings = InstagramBotSettings.objects.create(
            pk=1,
            is_enabled=True,
            reply_permission_epoch=5,
            active_instruction_publication=self.publication,
        )
        self.client_row = IgClient.objects.create(
            igsid="revision-proposal-client",
            reply_permission_epoch=7,
            language="uk",
        )
        self.body = b"fixture-image"
        self.part_id = "mp1_" + "a" * 32
        self.content_hash = hashlib.sha256(self.body).hexdigest()
        self.source = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text="Що на фото?",
            mid="revision-proposal-source",
            status=InstagramBotMessage.Status.PENDING,
            private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE,
            attachment_media=[{
                "source_part_id": self.part_id,
                "original_index": 0,
                "identity_origin": "ingress",
                "type": "image",
                "status": "owned",
                "mime": "image/jpeg",
                "bytes": len(self.body),
                "content_hash": self.content_hash,
                "private_storage": True,
                "storage_name": "ig-private/never-copy-this",
                "url": "https://signed.invalid/never-copy-this",
            }],
        )
        turn = IgCustomerTurn.objects.create(
            client=self.client_row,
            primary_source_message=self.source,
            window_started_at=timezone.now(),
            window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(
            turn=turn, message=self.source, ordinal=1, role=self.source.role
        )
        revision = create_collecting_revision(
            turn, [self.source], now=timezone.now(), bypass_quiet=True
        ).revision
        preparation = claim_revision_preparation(revision.pk)
        sealed = seal_revision(revision.pk, preparation.token).revision
        claimed = claim_sealed_revision(sealed.pk)
        self.revision = claimed.revision
        self.token = claimed.token
        self.generated_at = timezone.now()
        self.request_id = "request-proposal-1"
        self.model = "gemini-3.7-flash"
        self.publication_binding = PublicationBinding(
            self.publication.pk,
            self.publication.version,
            self.publication.snapshot_hash,
        )
        self._create_generation_graph()

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def _authority(self):
        result = build_revision_authority_bindings(
            self.client_row,
            claims=(CLAIM_PUBLIC_POLICY_INPUTS,),
            settings_obj=self.settings,
        )
        self.assertTrue(result.ready, result.reasons)
        return result

    def _policy_manifest(self):
        publication = {
            "id": self.publication.pk,
            "version": self.publication.version,
            "hash": self.publication.snapshot_hash,
            "compiler_version": self.publication.compiler_version,
        }
        return {
            "version": "compiled-core-v1",
            "content_hash": "b" * 64,
            "selected_ids": ["authority:server"],
            "omitted": [],
            "mandatory_ids": ["authority:server"],
            "budget_chars": 48000,
            "visual_trigger_codes": ["gift_candidate"],
            "core": {
                "version": "2026-09-07.core.v1",
                "prompt_hash": "c" * 64,
                "directives_hash": "d" * 64,
            },
            "knowledge_hash": "e" * 64,
            "instruction_publication": publication,
            "instruction_selection": {
                "selected_ids": [],
                "omitted": [],
                "visual_trigger_codes": ["gift_candidate"],
                "publication_id": publication["id"],
                "publication_version": publication["version"],
                "publication_hash": publication["hash"],
                "publication_compiler_version": publication["compiler_version"],
            },
        }

    def _create_generation_graph(self, *, execution_key=None):
        from management.services.gemini_accounting_runtime import revision_request_execution

        with revision_request_execution(
            self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
        ):
            graph = GeminiRequest.objects.create(
                request_id=self.request_id,
                lane="live",
                task_class="complex_live",
                reasoning_task="customer_chat",
                logical_turn_id=f"ig-revision:{self.revision.pk}",
                source_execution_key=(
                    f"ig-revision:{self.revision.pk}" if execution_key is None else execution_key
                ),
                source_message_id=self.source.pk,
                client_id=self.client_row.pk,
                policy_manifest=sanitize_request_policy_manifest(
                    self._policy_manifest()
                ),
                accounting_mode=GeminiRequest.AccountingMode.SHADOW,
            )
        attempt = GeminiRequestAttempt.objects.create(
            request_id=graph.request_id,
            request_graph=graph,
            role="chat",
            key_name="GEMINI_API",
            model=self.model,
            outcome="succeeded",
            fsm_state=GeminiRequestAttempt.FsmState.SUCCEEDED,
            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
            logical_turn_id=graph.logical_turn_id,
            source_message_id=graph.source_message_id,
            client_id=graph.client_id,
            lane="live",
            attempt_index=1,
            candidate_index=1,
            winner_claimed=True,
        )
        graph.winner_attempt = attempt
        graph.terminal_resolution = "succeeded"
        graph.terminal_reason = "provider_success"
        graph.resolved_at = timezone.now()
        graph.save(update_fields=[
            "winner_attempt", "terminal_resolution", "terminal_reason",
            "resolved_at", "updated_at",
        ])

    def _media_manifest(self):
        item = {
            "source_message_id": self.source.pk,
            "source_part_id": self.part_id,
            "original_index": 0,
            "identity_origin": "legacy_positional",
            "sealed_capture_outcome": "owned",
            "inline_index": 0,
            "mime": "image/jpeg",
            "bytes": len(self.body),
            "content_hash": self.content_hash,
        }
        coverage = {
            "total_parts": 1,
            "sealed_owned": 1,
            "admitted": 1,
            "omitted": 0,
            "unavailable": 0,
            "image_admitted": 1,
            "audio_admitted": 0,
            "raw_bytes": len(self.body),
            "serialized_inline_bytes": 100,
        }
        binding = {
            "version": "ig-revision-media-v1",
            "revision_id": self.revision.pk,
            "revision_snapshot_digest": self.revision.snapshot_digest,
            "items": [item],
            "outcomes": [{
                **item, "collection_outcome": "admitted", "reason": "",
            }],
            "actual_content_hashes": [self.content_hash],
            "coverage": coverage,
        }
        binding["digest"] = self._digest(binding)
        binding["actual_inline_count"] = 1
        return binding

    def _intelligence(self):
        return {
            "schema_version": 1,
            "candidate_set_version": "catalog-candidates-v2",
            "candidate_set_digest": "f" * 64,
            "candidate_set_size": 0,
            "catalog_candidates": [],
            "transcript": "",
            "intent": "media_review",
            "audio_status": "not_applicable",
            "confidence": 0.9,
            "image_observations": [{
                "source_image_index": 0,
                "source_part_id": self.part_id,
                "original_index": 0,
                "identity_origin": "ingress",
                "capture_state": "owned",
                "content_hash": self.content_hash,
                "outcome": "understood",
                "evidence_code": "visual_content",
                "type_code": "product",
            }],
            "media_request": {
                "request_id": self.request_id,
                "provider_model": self.model,
                "inline_count_known": True,
                "actual_inline_count": 1,
                "prepared_inline_count": 1,
                "submitted_parts": [{
                    "source_part_id": self.part_id,
                    "source_message_scope": str(self.source.pk),
                    "original_index": 0,
                    "content_hash": self.content_hash,
                }],
            },
            "catalog_resolution": "no_match",
            "auto_product_id": None,
            "request_permission_epoch": self.client_row.reply_permission_epoch,
        }

    def _store(self, **overrides):
        values = {
            "source_message_ids": (self.source.pk,),
            "settings_id": self.settings.pk,
            "settings_permission_epoch": self.settings.reply_permission_epoch,
            "publication": self.publication_binding,
            "request_id": self.request_id,
            "actual_model": self.model,
            "generated_at": self.generated_at,
            "response": ValidatedResponse(reply_text="На фото товар.", valid=True),
            "turn_intelligence": self._intelligence(),
            "request_media_manifest": self._media_manifest(),
            "policy_manifest": self._policy_manifest(),
            "authority": self._authority(),
        }
        values.update(overrides)
        return store_revision_generation_proposal(
            self.revision.pk, self.token, **values
        )

    def test_write_once_exact_replay_and_private_transport_fields_excluded(self):
        created = self._store()
        replay = self._store()

        self.assertTrue(created.created, created.reasons)
        self.assertTrue(replay.stored, replay.reasons)
        self.assertFalse(replay.created)
        self.assertEqual(created.digest, replay.digest)
        self.revision.refresh_from_db()
        serialized = json.dumps(self.revision.generation_proposal)
        self.assertNotIn("signed.invalid", serialized)
        self.assertNotIn("storage_name", serialized)
        self.assertNotIn("never-copy-this", serialized)
        self.assertEqual(
            self.revision.generation_proposal["generation"]["request_id"],
            self.request_id,
        )
        with self.assertRaises(ValueError):
            IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(
                generation_proposal={}
            )

    def test_request_hash_mismatch_is_rejected_without_durable_draft(self):
        manifest = self._media_manifest()
        manifest["items"][0]["content_hash"] = "0" * 64

        result = self._store(request_media_manifest=manifest)

        self.assertEqual(result.reasons, ("request_media_digest_mismatch",))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal, {})

    def test_different_second_proposal_cannot_overwrite_first(self):
        first = self._store()
        changed = ValidatedResponse(reply_text="Інша відповідь.", valid=True)

        second = self._store(response=changed)

        self.assertTrue(first.created)
        self.assertEqual(second.reasons, ("proposal_mismatch",))
        self.revision.refresh_from_db()
        self.assertEqual(
            self.revision.generation_proposal["response"]["reply_text"],
            "На фото товар.",
        )

    def test_stale_permission_fails_cas_without_writing(self):
        IgClient.objects.filter(pk=self.client_row.pk).update(
            reply_permission_epoch=self.client_row.reply_permission_epoch + 1
        )

        result = self._store()

        self.assertIn("client_permission_changed", result.reasons)
        self.revision.refresh_from_db()
        self.assertFalse(self.revision.generation_proposal_digest)

    def test_artifact_request_identity_must_match_actual_generation(self):
        intelligence = deepcopy(self._intelligence())
        intelligence["media_request"]["request_id"] = "other-request"

        result = self._store(turn_intelligence=intelligence)

        self.assertEqual(result.reasons, ("turn_intelligence_request_mismatch",))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal, {})

    def test_missing_successful_request_graph_is_rejected(self):
        intelligence = self._intelligence()
        intelligence["media_request"]["request_id"] = "missing-request"

        result = self._store(
            request_id="missing-request",
            turn_intelligence=intelligence,
        )

        self.assertEqual(result.reasons, ("generation_graph_invalid",))
        self.revision.refresh_from_db()
        self.assertFalse(self.revision.generation_proposal_digest)

    def test_legacy_graph_with_revision_like_logical_turn_is_rejected(self):
        self.request_id = "legacy-namespace-lookalike"
        self._create_generation_graph(execution_key="")

        result = self._store()

        self.assertEqual(result.reasons, ("generation_graph_invalid",))
        self.revision.refresh_from_db()
        self.assertFalse(self.revision.generation_proposal_digest)

    def test_projection_uses_global_index_and_preserves_old_source_artifact(self):
        old_artifact = {"schema_version": 1, "intent": "historical_source"}
        self.source.turn_intelligence_artifact = old_artifact
        self.source.save(update_fields=["turn_intelligence_artifact"])
        stored = self._store()
        self.assertTrue(stored.created, stored.reasons)

        projected = project_revision_image_inspections(
            self.revision.pk, self.token
        )

        self.assertEqual(projected.projected, 1, projected.reasons)
        self.assertEqual(projected.skipped, 0)
        self.source.refresh_from_db()
        self.assertEqual(self.source.turn_intelligence_artifact, old_artifact)
        inspection = self.source.attachment_media[0]["inspection"]
        self.assertEqual(inspection["state"], "inspected")
        self.assertEqual(inspection["source_image_index"], 0)
        self.assertEqual(inspection["content_hash"], self.content_hash)
        self.assertEqual(inspection["request_id"], self.request_id)
        self.assertEqual(inspection["revision_id"], self.revision.pk)
