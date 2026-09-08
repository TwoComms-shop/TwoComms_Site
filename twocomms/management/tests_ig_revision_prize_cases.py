import hashlib
import json

from django.test import TransactionTestCase
from django.utils import timezone

from management.models import (
    BotInstruction,
    BotPolicyPublication,
    IgBotNotification,
    IgClient,
    IgCustomerTurn,
    IgFollowUpTask,
    IgTurnMessage,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_policy_publication import snapshot_from_rows, snapshot_hash
from management.services.ig_prize_cases import upsert_prize_review_case
from management.services.ig_prize_programme import active_shooting_prize_programme
from management.services.ig_revision_authority import (
    CLAIM_PUBLIC_POLICY_INPUTS,
    build_revision_authority_bindings,
)
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_revisions import (
    claim_revision_preparation,
    claim_sealed_revision,
    create_collecting_revision,
    seal_revision,
)


class RevisionPrizeCaseTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        instruction = BotInstruction.objects.create(
            title="Shooting programme",
            body="Review visible shooting certificate cues.",
            intent_tags="programme:shooting_prize",
            is_active=True,
            priority=10,
            locale="all",
            programme_metadata={
                "kind": "shooting_prize",
                "programme_id": "shooting_prize",
                "manager_required": True,
                "confirmed_visual_sample": False,
            },
            allowed_actions=[],
            trust_scope="public_policy",
        )
        snapshot = snapshot_from_rows([instruction])
        self.publication = BotPolicyPublication.objects.create(
            version=1,
            kind=BotPolicyPublication.Kind.PUBLISH,
            schema_version=1,
            snapshot=snapshot,
            snapshot_hash=snapshot_hash(snapshot),
            compiler_version="instruction-set-v1",
            instruction_count=1,
        )
        self.settings = InstagramBotSettings.objects.create(
            pk=1,
            is_enabled=True,
            reply_permission_epoch=7,
            active_instruction_publication=self.publication,
        )
        self.publication_binding = PublicationBinding(
            self.publication.pk,
            self.publication.version,
            self.publication.snapshot_hash,
        )
        self.programme = active_shooting_prize_programme()

    def _case(self, suffix, *, candidate=True):
        client = IgClient.objects.create(
            igsid=f"revision-prize-{suffix}", reply_permission_epoch=3
        )
        part_id = "mp1_" + hashlib.sha256(suffix.encode()).hexdigest()[:32]
        content_hash = hashlib.sha256((suffix + ":image").encode()).hexdigest()
        source = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text="",
            mid=f"revision-prize-source-{suffix}",
            media_capture_eligible=True,
            private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE,
            attachment_media=[{
                "url": f"https://private.invalid/{suffix}.jpg",
                "source_part_id": part_id,
                "original_index": 0,
                "content_hash": content_hash,
                "status": "owned",
                "capture_state": "owned",
                "identity_origin": "ingress",
                "provenance": "live_webhook",
                "private_storage": True,
                "storage_name": f"private/{suffix}.jpg",
                "mime": "image/jpeg",
                "bytes": 100,
            }],
            # This historical artifact is intentionally unrelated. The
            # revision branch must never consult or rewrite it.
            turn_intelligence_artifact={
                "schema_version": 1,
                "intent": "historical_non_prize",
                "image_observations": [],
            },
        )
        now = timezone.now()
        turn = IgCustomerTurn.objects.create(
            client=client,
            primary_source_message=source,
            window_started_at=now,
            window_deadline=now,
        )
        IgTurnMessage.objects.create(
            turn=turn, message=source, ordinal=1, role=source.role
        )
        revision = create_collecting_revision(
            turn, [source], now=now, bypass_quiet=True
        ).revision
        preparation = claim_revision_preparation(revision.pk, now=now)
        revision = seal_revision(
            revision.pk, preparation.token, now=now
        ).revision
        claim = claim_sealed_revision(revision.pk, now=now)
        revision = claim.revision
        source_row = revision.sources.get(message_id=source.pk)

        request_id = f"request-{suffix}"
        model = "gemini-test"
        media = list(source.attachment_media)
        media[0]["inspection"] = {
            "version": "ig-media-inspection-v1",
            "state": "inspected",
            "source_part_id": part_id,
            "source_image_index": 0,
            "outcome": "understood",
            "evidence_code": "visual_content",
            "type_code": "certificate" if candidate else "product",
            "content_hash": content_hash,
            "request_id": request_id,
            "provider_model": model,
            "revision_id": revision.pk,
        }
        source.attachment_media = media
        source.save(update_fields=["attachment_media"])

        authority = build_revision_authority_bindings(
            client,
            claims=(CLAIM_PUBLIC_POLICY_INPUTS,),
            settings_obj=self.settings,
            server_authorized_actions=("prize_review_case_create",),
        )
        self.assertTrue(authority.ready, authority.reasons)
        observation = {
            "source_image_index": 0,
            "source_part_id": part_id,
            "original_index": 0,
            "content_hash": content_hash,
            "outcome": "understood",
            "evidence_code": "visual_content",
            "type_code": "certificate" if candidate else "product",
        }
        if candidate:
            observation["prize_certificate"] = {
                "programme_id": self.programme.programme_id,
                "programme_version": self.programme.version,
                "status": "uncertain",
                "cue_codes": ["shooting_target"],
                "reason_code": "visible_programme_cues",
                "manager_required": True,
            }
        proposal = {
            "schema_version": 1,
            "sources": [{
                "message_id": source.pk,
                "source_digest": source_row.source_digest,
                "ordinal": source_row.ordinal,
            }],
            "generation": {
                "request_id": request_id,
                "actual_model": model,
                "generated_at": now.isoformat(),
            },
            "response": {"reply_text": "Safe reply", "controls": []},
            "turn_intelligence": {
                "schema_version": 1,
                "intent": "media_review",
                "image_observations": [observation],
                "media_request": {
                    "request_id": request_id,
                    "provider_model": model,
                    "inline_count_known": True,
                    "actual_inline_count": 1,
                    "prepared_inline_count": 1,
                    "submitted_parts": [{
                        "source_message_id": source.pk,
                        "source_part_id": part_id,
                        "original_index": 0,
                        "content_hash": content_hash,
                    }],
                },
            },
            "authority": {
                "allowed_actions": list(authority.allowed_actions),
                "fact_bindings": list(authority.fact_bindings),
                "offer_bindings": list(authority.offer_bindings),
                "authority_digest": authority.authority_digest,
            },
        }
        digest = hashlib.sha256(json.dumps(
            proposal,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        revision.generation_proposal = proposal
        revision.generation_proposal_digest = digest
        revision.generation_proposed_at = now
        revision.save(update_fields=[
            "generation_proposal",
            "generation_proposal_digest",
            "generation_proposed_at",
            "updated_at",
        ])
        return client, source, revision, claim.token, digest

    def _upsert(self, source, revision, token, digest):
        return upsert_prize_review_case(
            source,
            programme=self.programme,
            expected_permission_epoch=revision.permission_epoch,
            revision_id=revision.pk,
            revision_token=token,
            expected_generation_proposal_digest=digest,
            settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.publication_binding,
        )

    def test_valid_revision_candidate_creates_one_idempotent_case(self):
        _client, source, revision, token, digest = self._case("valid")

        first = self._upsert(source, revision, token, digest)
        second = self._upsert(source, revision, token, digest)

        self.assertTrue(first.created, first.reason)
        self.assertFalse(second.created)
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)
        task = IgFollowUpTask.objects.get(pk=first.task_id)
        self.assertEqual(len(task.manager_context["evidence"]), 1)
        evidence = task.manager_context["evidence"][0]
        self.assertEqual(evidence["revision_id"], revision.pk)
        self.assertEqual(evidence["generation_proposal_digest"], digest)
        source.refresh_from_db()
        self.assertEqual(
            source.turn_intelligence_artifact["intent"],
            "historical_non_prize",
        )

    def test_publication_change_and_erasure_fail_before_case_writes(self):
        client, source, revision, token, digest = self._case("cas")
        empty_snapshot = {"schema_version": 1, "instructions": []}
        other = BotPolicyPublication.objects.create(
            version=2,
            kind=BotPolicyPublication.Kind.PUBLISH,
            schema_version=1,
            snapshot=empty_snapshot,
            snapshot_hash=snapshot_hash(empty_snapshot),
            compiler_version="instruction-set-v1",
            instruction_count=0,
        )
        self.settings.active_instruction_publication = other
        self.settings.save(update_fields=["active_instruction_publication", "updated_at"])
        changed = self._upsert(source, revision, token, digest)
        self.assertEqual(changed.reason, "publication_changed")

        self.settings.active_instruction_publication = self.publication
        self.settings.save(update_fields=["active_instruction_publication", "updated_at"])
        client.privacy_erasure_started_at = timezone.now()
        client.save(update_fields=["privacy_erasure_started_at", "updated_at"])
        erased = self._upsert(source, revision, token, digest)
        self.assertIn(erased.reason, {"client_erasure_active", "client_erasure_changed"})
        self.assertEqual(IgFollowUpTask.objects.count(), 0)
        self.assertEqual(IgBotNotification.objects.count(), 0)

    def test_stale_projected_inspection_creates_no_case(self):
        _client, source, revision, token, digest = self._case("stale")
        media = list(source.attachment_media)
        media[0]["inspection"]["request_id"] = "stale-request"
        source.attachment_media = media
        source.save(update_fields=["attachment_media"])

        result = self._upsert(source, revision, token, digest)

        self.assertEqual(result.reason, "candidate_not_validated")
        self.assertEqual(IgFollowUpTask.objects.count(), 0)

    def test_ordinary_nonprize_image_creates_no_case(self):
        _client, source, revision, token, digest = self._case(
            "ordinary", candidate=False
        )

        result = self._upsert(source, revision, token, digest)

        self.assertEqual(result.reason, "candidate_not_validated")
        self.assertEqual(IgFollowUpTask.objects.count(), 0)
