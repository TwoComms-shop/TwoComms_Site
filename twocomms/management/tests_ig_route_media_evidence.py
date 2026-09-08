"""Actual winning-request media can establish a topic, never business authority."""
import hashlib
import json
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from management import tests_ig_revision_live as fixtures
from management.models import IgConversationRouteDecision, IgTurnMessage, InstagramBotMessage
from management.services.ig_turn_revisions import create_collecting_revision
from management.tests_ig_live_customer_routes import route_payload


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RouteMediaEvidenceTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute

    def prepare_media(self, bundles=(("image",),), *, evidence_index=0):
        self.body = b"route-owned-media-fixture"
        self.media_sources = []
        global_index = 0
        observations = []
        for ordinal, kinds in enumerate(bundles):
            row = self.source if ordinal == 0 else self._message("", f"route-media-{ordinal}")
            row.text = ""
            row.private_media_state = InstagramBotMessage.PrivateMediaState.ACTIVE
            row.attachment_media = []
            for index, kind in enumerate(kinds):
                row.attachment_media.append({
                    "source_part_id": "mp1_" + f"{global_index + 1:032x}",
                    "original_index": index, "identity_origin": "ingress",
                    "type": kind, "status": "owned", "mime": f"{kind}/{'jpeg' if kind == 'image' else 'ogg'}",
                    "bytes": len(self.body), "content_hash": hashlib.sha256(self.body).hexdigest(),
                    "private_storage": True, "storage_name": f"ig-private/route-{row.pk}-{index}",
                })
                if kind == "image":
                    observations.append({"source_image_index": global_index,
                        "outcome": "understood", "evidence_code": "visual_content", "type_code": "document"})
                global_index += 1
            row.save(update_fields=["text", "private_media_state", "attachment_media"])
            if ordinal:
                IgTurnMessage.objects.create(turn=self.turn, message=row, ordinal=ordinal + 1, role="user")
            self.media_sources.append(row)
        self.revision = create_collecting_revision(self.turn, self.media_sources, bypass_quiet=True).revision
        self._prepare()
        self.actual_inline_count = global_index
        has_audio = any("audio" in kinds for kinds in bundles)
        self.parsed.update({"reply_text": "Дякую за звернення. Уточніть, будь ласка, ваше запитання.",
            "customer_routes": route_payload(self.media_sources[evidence_index].pk),
            "turn_intelligence": {"catalog_candidates": [], "intent": "visual_question", "confidence": 0.9,
                "audio_status": "transcribed" if has_audio else "not_applicable",
                "transcript": "Мене цікавить робота у вас" if has_audio else "",
                "image_observations": observations}})

    def run_media(self, accepted=True, *, changed_owner=False):
        with patch("management.services.instagram_bot._owned_media_bytes",
            side_effect=lambda part, **kwargs: (part["mime"], self.body)):
            result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked" if changed_owner else "completed", result.reasons)
        if changed_owner:
            # Existing image-inspection privacy safeguard independently blocks
            # delivery after ownership mutation; the valid proposal still survives.
            self.assertEqual(result.reasons, ("parts_skipped",))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 0 if changed_owner else 1)
        self.revision.refresh_from_db()
        self.assertTrue(self.revision.generation_proposal["response"]["reply_text"].startswith(self.parsed["reply_text"]))
        self.assertEqual(IgConversationRouteDecision.objects.filter(revision=self.revision).exists(), accepted)
        return self.revision.generation_proposal

    def test_understood_image_without_prior_inspection_is_accepted(self):
        self.prepare_media()
        proposal = self.run_media()
        proof = proposal["route_media_evidence"]
        self.assertEqual(proof[0]["source_message_id"], self.source.pk)
        self.assertEqual(proof[0]["outcome"], "understood")
        self.assertEqual(proof[0]["request_id"], proposal["generation"]["request_id"])
        self.assertEqual(set(proof[0]), {"source_message_id", "source_part_id", "kind",
            "source_image_index", "content_hash", "outcome", "request_id", "provider_model"})
        prompt = self.generation_calls[0]["system_instruction"]["parts"][0]["text"]
        text = prompt.split("[CURRENT CUSTOMER ROUTE EVIDENCE]\n", 1)[1]
        context = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
        self.assertEqual(context["sources"][0]["media_parts"], [{"kind": "image", "source_image_index": 0}])
        self.assertNotIn("content_hash", json.dumps(context))
        self.assertNotIn(self.token, json.dumps(context))

    def test_every_image_in_message_must_be_understood(self):
        self.prepare_media((("image", "image"),))
        self.parsed["turn_intelligence"]["image_observations"][1]["outcome"] = "uncertain"
        proposal = self.run_media(False)
        self.assertEqual(proposal["route_abstention_reason"], "route_media_not_understood")

    def test_every_understood_image_is_recorded(self):
        self.prepare_media((("image", "image"),))
        proposal = self.run_media()
        self.assertEqual([part["source_image_index"] for part in proposal["route_media_evidence"]], [0, 1])

    def test_unreadable_image_abstains_route_only(self):
        self.prepare_media()
        self.parsed["turn_intelligence"]["image_observations"][0]["outcome"] = "unreadable"
        self.run_media(False)

    def test_provider_omitted_sibling_abstains_route_only(self):
        self.prepare_media((("image", "image"),))
        self.actual_inline_count = 1
        self.parsed["turn_intelligence"]["image_observations"] = self.parsed["turn_intelligence"]["image_observations"][:1]
        proposal = self.run_media(False)
        self.assertEqual(proposal["route_abstention_reason"], "route_media_not_admitted")

    def test_single_transcribed_audio_is_accepted_without_transcript_in_proof(self):
        self.prepare_media((("audio",),))
        proposal = self.run_media()
        self.assertEqual(proposal["route_media_evidence"][0]["outcome"], "transcribed")
        self.assertNotIn(self.parsed["turn_intelligence"]["transcript"], json.dumps(proposal["route_media_evidence"], ensure_ascii=False))

    def test_two_audio_messages_cannot_share_unattributed_transcript(self):
        self.prepare_media((("audio",), ("audio",)))
        proposal = self.run_media(False)
        self.assertEqual(proposal["route_abstention_reason"], "route_audio_not_attributable")

    def test_two_audio_parts_in_one_user_message_are_attributable(self):
        self.prepare_media((("audio", "audio"),))
        proposal = self.run_media()
        self.assertEqual(len(proposal["route_media_evidence"]), 2)

    def test_unintelligible_audio_abstains_route_only(self):
        self.prepare_media((("audio",),))
        self.parsed["turn_intelligence"].update(audio_status="unintelligible", transcript="")
        self.run_media(False)

    def test_image_index_is_global_after_audio_part(self):
        self.prepare_media((("audio",), ("image",)), evidence_index=1)
        proposal = self.run_media()
        self.assertEqual(proposal["route_media_evidence"][0]["source_image_index"], 1)
        self.assertEqual(proposal["route_media_evidence"][0]["source_message_id"], self.media_sources[1].pk)

    def mutate_part(self, **changes):
        self.source.refresh_from_db()
        self.source.attachment_media[0].update(changes)
        self.source.save(update_fields=["attachment_media"])

    def append_late_part(self):
        self.source.refresh_from_db()
        late_part = {**self.source.attachment_media[0],
            "source_part_id": "mp1_" + "f" * 32, "original_index": 1}
        self.source.attachment_media.append(late_part)
        self.source.save(update_fields=["attachment_media"])

    def test_late_part_after_winner_abstains_route_only(self):
        self.prepare_media()
        self.after_validate = self.append_late_part
        proposal = self.run_media(False)
        self.assertEqual(proposal["route_abstention_reason"], "route_media_owner_changed")

    def test_late_part_between_storage_and_acceptance_abstains_route_only(self):
        from management.services.ig_conversation_routes import accept_customer_routes

        self.prepare_media()
        results = []
        def append_then_accept(*args, **kwargs):
            self.append_late_part()
            result = accept_customer_routes(*args, **kwargs)
            results.append(result)
            return result
        with patch("management.services.ig_conversation_routes.accept_customer_routes", side_effect=append_then_accept):
            proposal = self.run_media(False)
        self.assertTrue(proposal["route_media_evidence"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].reason_code, "route_media_owner_changed")

    def test_hash_changed_after_winner_abstains_route_and_preserves_proposal(self):
        self.prepare_media()
        self.after_validate = lambda: self.mutate_part(content_hash="a" * 64)
        proposal = self.run_media(False, changed_owner=True)
        self.assertEqual(proposal["route_abstention_reason"], "route_media_owner_changed")

    def test_part_no_longer_owned_after_winner_abstains_route_and_preserves_proposal(self):
        self.prepare_media()
        self.after_validate = lambda: self.mutate_part(status="failed", capture_state="failed")
        self.run_media(False, changed_owner=True)

    def test_private_media_removed_after_winner_abstains_route_and_preserves_proposal(self):
        self.prepare_media()
        self.after_validate = lambda: InstagramBotMessage.objects.filter(pk=self.source.pk).update(private_media_state="deleted")
        self.run_media(False, changed_owner=True)

    def test_acceptance_rechecks_current_media_after_proposal_storage(self):
        from management.services.ig_conversation_routes import accept_customer_routes

        self.prepare_media()
        def change_then_accept(*args, **kwargs):
            self.mutate_part(content_hash="b" * 64)
            return accept_customer_routes(*args, **kwargs)
        with patch("management.services.ig_conversation_routes.accept_customer_routes", side_effect=change_then_accept):
            proposal = self.run_media(False, changed_owner=True)
        self.assertTrue(proposal["route_media_evidence"])
