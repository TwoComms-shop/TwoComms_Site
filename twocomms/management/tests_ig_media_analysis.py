"""P2–5: exact admitted parts, conservative UGC replies and real proposal proof."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.services.ig_media_analysis import (
    MediaAnalysisError, bind_media_analysis, bind_turn_media_analysis,
    parse_media_observations, media_reaction, validate_bound_media_analysis,
)
from management.services.ig_response_control import parse_structured_response
from management.services.ig_ugc_assessment import safe_ugc_acknowledgement


def part(index, *, mime="image/jpeg", owner=41, digest=None):
    return {"source_part_id": "mp1_" + f"{index + 1:032x}", "original_index": index,
        "source_message_id": owner, "capture_state": "owned", "mime": mime,
        "content_hash": digest or f"{index + 1:064x}", "bytes": 100}


def observation(index=0, **changes):
    return {"source_inline_index": index, "outcome": "understood", "content_kind": "wearing",
        "sentiment": "positive", "confidence": 0.9, "evidence_code": "visual_content",
        "evidence": "Customer wearing apparel", **changes}


def bind(parts, observations=(), *, actual_count=None, legacy_images=(), capture_outcomes=()):
    count = len(parts) if actual_count is None else actual_count
    return bind_media_analysis(parts=parts, observations=observations, actual_inline_count=count,
        actual_content_hashes=[row["content_hash"] for row in parts[:count]],
        request_id="actual-request", provider_model="actual-model", legacy_images=legacy_images,
        capture_outcomes=capture_outcomes)


class MediaAnalysisPureTests(SimpleTestCase):
    def test_legacy_understood_image_does_not_invent_taxonomy_or_positive_sentiment(self):
        result = bind([part(0)], legacy_images=[{"source_image_index": 0, "outcome": "understood"}])
        row = result["parts"][0]
        self.assertEqual((row["analysis_state"], row["content_kind"], row["sentiment"]),
            ("legacy_unknown", "unknown", "unknown"))
        self.assertEqual(media_reaction(result)["mode"], "neutral")

    def test_actual_global_inline_order_keeps_two_voice_owners_and_image_distinct(self):
        parts = [part(0, mime="audio/ogg", owner=41), part(1, mime="image/jpeg", owner=42),
            part(2, mime="audio/ogg", owner=43)]
        observations = [observation(0, content_kind="unknown", sentiment="neutral", evidence_code="audio_speech",
            audio_status="transcribed", transcript="First voice"), observation(1),
            observation(2, content_kind="unknown", sentiment="negative", evidence_code="audio_speech",
            audio_status="transcribed", transcript="Second voice", complaint_code="current_customer_complaint")]
        result = bind(parts, observations)
        self.assertEqual([row["source_message_id"] for row in result["parts"]], [41, 42, 43])
        self.assertEqual([row["transcript"] for row in result["parts"]], ["First voice", "", "Second voice"])
        self.assertEqual([row["content_hash"] for row in result["parts"]], [row["content_hash"] for row in parts])

    def test_positive_negative_unavailable_burst_preserves_every_outcome_and_service_priority(self):
        parts = [part(0), part(1)]
        unavailable = {"source_message_id": 41, "source_part_id": part(2)["source_part_id"],
            "original_index": 2, "collection_outcome": "unavailable", "reason": "owned_bytes_unavailable"}
        result = bind(parts, [observation(0), observation(1, sentiment="negative",
            complaint_code="current_customer_complaint", evidence="Seam ripped")], capture_outcomes=[unavailable])
        reaction = media_reaction(result)
        self.assertEqual(reaction["sentiments"], ["negative", "positive"])
        self.assertEqual(reaction["mode"], "service")
        self.assertTrue(reaction["incomplete"])
        self.assertEqual(result["capture_outcomes"][0], unavailable)
        self.assertEqual(reaction["complaint_parts"][0]["content_hash"], parts[1]["content_hash"])

    def test_negative_without_current_complaint_is_neutral_without_action(self):
        result = bind([part(0)], [observation(sentiment="negative", complaint_code="none")])
        self.assertEqual(media_reaction(result)["mode"], "neutral")
        self.assertEqual(media_reaction(result)["complaint_parts"], [])

    def test_single_mixed_complaint_remains_mixed(self):
        result = bind([part(0)], [observation(sentiment="mixed", complaint_code="current_customer_complaint")])
        self.assertEqual(media_reaction(result)["sentiments"], ["mixed"])
        self.assertEqual(media_reaction(result)["mode"], "service")

    def test_failed_capture_reason_is_not_other_or_understood(self):
        result = bind([], capture_outcomes=[{"source_message_id": 41, "source_part_id": part(0)["source_part_id"],
            "original_index": 0, "collection_outcome": "unavailable", "reason": "capture_failed"}])
        self.assertFalse(result["parts"])
        self.assertTrue(media_reaction(result)["incomplete"])
        self.assertEqual(result["capture_outcomes"][0]["reason"], "capture_failed")

    def test_provider_omitted_tail_has_no_analysis(self):
        result = bind([part(0), part(1)], [observation()], actual_count=1)
        self.assertEqual(result["parts"][1]["analysis_state"], "omitted")
        self.assertEqual(result["parts"][1]["outcome"], "provider_omitted")
        self.assertEqual(result["parts"][1]["sentiment"], "unknown")
        self.assertEqual(media_reaction(result)["mode"], "neutral")

    def test_observation_for_prepared_but_unsent_tail_is_rejected(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_part_not_admitted"):
            bind([part(0), part(1)], [observation(1)], actual_count=1)

    def test_actual_provider_hash_mismatch_is_rejected(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_binding_invalid"):
            bind_media_analysis(parts=[part(0)], actual_inline_count=1, actual_content_hashes=["f" * 64],
                request_id="actual-request", provider_model="actual-model")

    def test_missing_or_coerced_actual_hash_evidence_is_rejected_even_without_observations(self):
        for hashes in (None, [], "1" * 64, [1], ["A" * 64], [" " + part(0)["content_hash"]]):
            with self.subTest(hashes=hashes), self.assertRaises(MediaAnalysisError):
                bind_media_analysis(parts=[part(0)], actual_inline_count=1, actual_content_hashes=hashes,
                    request_id="actual-request", provider_model="actual-model")

    def test_two_message_owners_can_share_original_index_but_one_owner_cannot(self):
        parts = [part(0, owner=41), part(1, owner=42) | {"original_index": 0}]
        self.assertEqual(len(bind(parts, [observation(0), observation(1)])["parts"]), 2)
        parts[1]["source_message_id"] = 41
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_duplicate_part"):
            bind(parts)

    def test_invalid_owner_mime_and_hash_never_gain_an_unknown_record(self):
        for changes in ({"source_message_id": True}, {"source_message_id": 0},
            {"mime": "application/pdf"}, {"mime": "image/jpeg; secret=value"},
            {"content_hash": 1}, {"capture_state": "expired"}):
            with self.subTest(changes=changes), self.assertRaises(MediaAnalysisError):
                bind([part(0) | changes])

    def test_blank_or_control_request_identity_and_model_are_rejected(self):
        for request_id, model in ((" ", "actual-model"), ("actual-request", " "),
            ("actual-request\n", "actual-model"), ("actual-request", "actual\x00model")):
            with self.subTest(request_id=request_id, model=model), self.assertRaises(MediaAnalysisError):
                bind_media_analysis(parts=[], actual_inline_count=0, actual_content_hashes=[],
                    request_id=request_id, provider_model=model)

    def test_capture_outcomes_cannot_contradict_submitted_owner_part_or_position(self):
        captured = {"source_message_id": 41, "source_part_id": part(0)["source_part_id"],
            "original_index": 0, "collection_outcome": "admitted", "reason": ""}
        for changes in ({"original_index": 1}, {"collection_outcome": "unavailable"},
            {"source_message_id": 42}, {"source_part_id": part(1)["source_part_id"]}):
            with self.subTest(changes=changes), self.assertRaisesRegex(MediaAnalysisError, "media_capture_outcome_binding_mismatch"):
                bind([part(0)], capture_outcomes=[captured | changes])

    def test_malformed_capture_state_fails_with_typed_error(self):
        with self.assertRaises(MediaAnalysisError):
            bind([], capture_outcomes=[{"source_message_id": 41, "source_part_id": part(0)["source_part_id"],
                "original_index": 0, "collection_outcome": []}])

    def test_missing_provider_observation_does_not_claim_unavailable_audio_was_inspected(self):
        result = bind([part(0, mime="audio/ogg")])
        self.assertEqual((result["parts"][0]["analysis_state"], result["parts"][0]["transcript"]),
            ("legacy_unknown", ""))
        self.assertEqual(media_reaction(result)["mode"], "neutral")

    def test_audio_index_cannot_claim_image_legacy_observation(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_legacy_index_invalid"):
            bind([part(0, mime="audio/ogg")], legacy_images=[{"source_image_index": 0, "outcome": "understood"}])

    def test_sponsorship_cannot_come_from_brand_tag_only(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_sponsorship_evidence_missing"):
            bind([part(0)], [observation(content_kind="sponsorship", evidence="@twocomms tag", evidence_code="text_visible")])

    def test_explicit_sponsorship_is_interpretation_without_reward_fields(self):
        result = bind([part(0)], [observation(content_kind="sponsorship",
            evidence_code="sponsorship_disclosure", evidence="Explicit paid partnership disclosure")])
        self.assertEqual(result["parts"][0]["content_kind"], "sponsorship")
        for key in ("qualified_auto", "reward", "allowed_actions", "payment", "consent"):
            self.assertNotIn(key, result["parts"][0])

    def test_uncertain_analysis_cannot_claim_known_sentiment_or_complaint(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_observation_not_understood"):
            parse_media_observations([observation(outcome="uncertain")])

    def test_known_content_or_sentiment_requires_useful_evidence(self):
        for changes in ({"evidence": " "}, {"evidence_code": "insufficient_detail"},
            {"evidence_code": "text_unreadable"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(MediaAnalysisError, "media_observation_evidence_missing"):
                parse_media_observations([observation(**changes)])

    def test_evidence_modality_cannot_be_assigned_to_a_different_part(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_evidence_mime_mismatch"):
            bind([part(0)], [observation(evidence_code="audio_speech")])
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_evidence_mime_mismatch"):
            bind([part(0, mime="audio/ogg")], [observation(audio_status="transcribed", transcript="Voice")])

    def test_unintelligible_audio_cannot_claim_understood_content(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_evidence_mime_mismatch"):
            bind([part(0, mime="audio/ogg")], [observation(audio_status="unintelligible", evidence_code="audio_speech")])

    def test_audio_transcript_on_image_is_rejected(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_audio_mime_mismatch"):
            bind([part(0)], [observation(audio_status="transcribed", transcript="OCR text")])

    def test_unintelligible_audio_preserves_unknown_empty_transcript(self):
        result = bind([part(0, mime="audio/ogg")], [observation(outcome="unreadable", content_kind="unknown",
            sentiment="unknown", audio_status="unintelligible", evidence_code="insufficient_detail", evidence="")])
        self.assertEqual(result["parts"][0]["transcript"], "")
        self.assertEqual(result["parts"][0]["audio_status"], "unintelligible")

    def test_total_per_part_transcript_budget_is_bounded(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_transcript_budget_exceeded"):
            parse_media_observations([observation(0, audio_status="transcribed", transcript="a" * 2500),
                observation(1, audio_status="transcribed", transcript="b" * 2500)])

    def test_bool_nan_duplicate_and_unknown_schema_are_rejected(self):
        for changes in ({"confidence": True}, {"confidence": float("nan")}, {"source_inline_index": True},
            {"sentiment": "happy"}, {"reward": True}):
            with self.subTest(changes=changes), self.assertRaises(MediaAnalysisError):
                parse_media_observations([observation(**changes)])
        with self.assertRaises(MediaAnalysisError):
            parse_media_observations([observation(), observation()])

    def test_legacy_and_new_image_outcomes_must_agree(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_legacy_conflict"):
            bind([part(0)], [observation()], legacy_images=[{"source_image_index": 0, "outcome": "uncertain"}])

    def test_arbitrary_legacy_outcome_cannot_create_inspected_state(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_legacy_index_invalid"):
            bind([part(0)], legacy_images=[{"source_image_index": 0, "outcome": "inspected"}])

    def test_applicable_video_uses_same_exact_binding_without_audio_invention(self):
        result = bind([part(0, mime="video/webm")], [observation(content_kind="review_video", evidence_code="video_content")])
        self.assertEqual(result["parts"][0]["mime"], "video/webm")
        self.assertEqual(result["parts"][0]["audio_status"], "not_applicable")

    def test_bound_proof_rejects_wrong_part_hash_request_owner_or_extra_authority(self):
        parts = [part(0)]
        result = bind(parts, [observation()])
        media = {"items": parts, "actual_inline_count": 1, "actual_content_hashes": [parts[0]["content_hash"]]}
        for field, changed in (("source_part_id", part(1)["source_part_id"]), ("content_hash", "f" * 64),
            ("source_message_id", 99), ("allowed_actions", ["reward"])):
            invalid = deepcopy(result)
            invalid["parts"][0][field] = changed
            with self.subTest(field=field), self.assertRaises(MediaAnalysisError):
                validate_bound_media_analysis(invalid, media=media, request_id="actual-request", provider_model="actual-model")
        with self.assertRaises(MediaAnalysisError):
            validate_bound_media_analysis(result, media=media, request_id="other-request", provider_model="actual-model")

    def test_bound_proof_distinguishes_boolean_from_integer_identity_and_checks_model(self):
        parts = [part(0, owner=1)]
        result = bind(parts, [observation()])
        media = {"items": parts, "actual_inline_count": 1, "actual_content_hashes": [parts[0]["content_hash"]]}
        invalid = deepcopy(result)
        invalid["parts"][0]["source_message_id"] = True
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_proof_mismatch"):
            validate_bound_media_analysis(invalid, media=media, request_id="actual-request", provider_model="actual-model")
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_proof_mismatch"):
            validate_bound_media_analysis(result, media=media, request_id="actual-request", provider_model="other-model")

    def test_invalid_validator_manifest_is_a_typed_binding_error(self):
        result = bind([])
        for media in (None, {}, {"items": []}, {"items": "invalid"}):
            with self.subTest(media=media), self.assertRaises(MediaAnalysisError):
                validate_bound_media_analysis(result, media=media, request_id="actual-request", provider_model="actual-model")

    def test_structured_parser_accepts_optional_extension_and_keeps_legacy(self):
        payload = {"reply_text": "Дякуємо", "controls": [], "turn_intelligence": {
            "catalog_candidates": [], "transcript": "", "intent": "media_review", "confidence": .9,
            "media_observations": [observation()]}}
        parsed = parse_structured_response(payload)
        self.assertTrue(parsed.valid, parsed.error)
        self.assertEqual(parsed.turn_intelligence.media_observations[0].sentiment, "positive")
        payload["turn_intelligence"].pop("media_observations")
        self.assertEqual(parse_structured_response(payload).turn_intelligence.media_observations, ())

    def test_adapter_uses_exact_actual_metadata_and_does_not_expose_storage(self):
        parts = [part(0) | {"url": "https://private.invalid", "storage_name": "private-file"}]
        result = bind_turn_media_analysis(SimpleNamespace(media_observations=parse_media_observations([observation()])),
            {"items": parts, "actual_inline_count": 1, "actual_content_hashes": [parts[0]["content_hash"]],
                "request_id": "actual-request", "provider_model": "actual-model"})
        self.assertEqual(result["request_id"], "actual-request")
        self.assertNotIn("url", result["parts"][0])
        self.assertNotIn("storage_name", result["parts"][0])

    def test_service_reply_precedes_qualified_gratitude_without_mutating_reward(self):
        assessment = SimpleNamespace(decision="qualified_auto", pk=5)
        analysis = bind([part(0)], [observation(sentiment="mixed", complaint_code="current_customer_complaint")])
        generated = "Підкажіть, будь ласка, що сталося зі швом?"
        self.assertEqual(safe_ugc_acknowledgement(SimpleNamespace(language="uk"), generated,
            assessment=assessment, media_analysis=analysis), generated)
        self.assertEqual(assessment.decision, "qualified_auto")

    def test_service_fallback_never_invents_manager_transfer_or_praises_complaint(self):
        analysis = bind([part(0)], [observation(sentiment="negative", complaint_code="current_customer_complaint")])
        reply = safe_ugc_acknowledgement(SimpleNamespace(language="uk"), "Передала менеджеру! Ви круто виглядаєте!",
            media_analysis=analysis)
        self.assertNotIn("Передала", reply)
        self.assertNotIn("круто", reply)
        self.assertIn("проблема", reply)

    def test_unknown_legacy_and_unavailable_never_claim_brand_clothes_or_inspection(self):
        for analysis in (None, bind([part(0)]), bind([part(0), part(1)], [observation()], actual_count=1)):
            reply = safe_ugc_acknowledgement(SimpleNamespace(language="uk"), "Круто виглядаєте в нашому одязі!",
                assessment=SimpleNamespace(decision="qualified_auto"), media_analysis=analysis)
            self.assertNotIn("нашому одязі", reply)
            self.assertNotIn("виглядаєте", reply)
            self.assertNotIn("Перевіряємо", reply)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class RevisionMediaAnalysisProjectionTests(TransactionTestCase):
    """Use the existing genuine seal/publication/winning-request fixture producer."""
    def setUp(self):
        from management import tests_ig_revision_proposal
        self.fixture = tests_ig_revision_proposal.RevisionGenerationProposalTests(methodName="runTest")
        self.fixture.setUp()

    def artifact(self, **changes):
        from management.services.ig_revision_proposal import _request_media_projection
        fixture = self.fixture
        media, reason = _request_media_projection(fixture.revision, fixture._media_manifest())
        self.assertFalse(reason)
        intelligence = fixture._intelligence()
        intelligence["media_analysis"] = bind_media_analysis(parts=media["items"],
            observations=[observation(**changes)], actual_inline_count=1,
            actual_content_hashes=media["actual_content_hashes"], request_id=fixture.request_id,
            provider_model=fixture.model, legacy_images=intelligence["image_observations"], capture_outcomes=media["outcomes"])
        return intelligence

    def test_real_proposal_projects_exact_taxonomy_and_preserves_historical_source_artifact(self):
        from management.services.ig_revision_proposal import project_revision_image_inspections
        fixture = self.fixture
        fixture.source.turn_intelligence_artifact = {"intent": "historical_source"}
        fixture.source.save(update_fields=["turn_intelligence_artifact"])
        stored = fixture._store(turn_intelligence=self.artifact(content_kind="unboxing"))
        self.assertTrue(stored.created, stored.reasons)
        result = project_revision_image_inspections(fixture.revision.pk, fixture.token)
        self.assertEqual((result.projected, result.skipped), (1, 0), result.reasons)
        fixture.source.refresh_from_db()
        row = fixture.source.attachment_media[0]["inspection"]
        self.assertEqual((row["content_kind"], row["sentiment"], row["content_hash"]),
            ("unboxing", "positive", fixture.content_hash))
        self.assertEqual(fixture.source.turn_intelligence_artifact, {"intent": "historical_source"})

    def test_real_proposal_rejects_analysis_owner_hash_mutation_before_durable_write(self):
        artifact = self.artifact()
        artifact["media_analysis"]["parts"][0]["content_hash"] = "f" * 64
        result = self.fixture._store(turn_intelligence=artifact)
        self.assertFalse(result.stored)
        self.assertEqual(result.reasons, ("media_analysis_proof_mismatch",))

    def test_real_accepted_complaint_keeps_proof_and_rejects_later_namespace_change(self):
        from management.services.ig_revision_proposal import validated_media_complaint_evidence
        fixture = self.fixture
        stored = fixture._store(turn_intelligence=self.artifact(sentiment="negative",
            complaint_code="current_customer_complaint", evidence="Explicit torn seam complaint"))
        self.assertTrue(stored.created, stored.reasons)
        fixture.revision.refresh_from_db()
        evidence, reason = validated_media_complaint_evidence(fixture.revision)
        self.assertFalse(reason)
        self.assertEqual(evidence[0]["source_message_id"], fixture.source.pk)
        self.assertEqual(evidence[0]["proposal_digest"], stored.digest)
        fixture.source.provider_namespace = "instagram_login:foreign-owner"
        fixture.source.save(update_fields=["provider_namespace"])
        evidence, reason = validated_media_complaint_evidence(fixture.revision)
        self.assertEqual((evidence, reason), ([], "media_complaint_owner_changed"))

    def test_real_legacy_projection_explicitly_retains_unknown_sentiment(self):
        from management.services.ig_revision_proposal import project_revision_image_inspections
        fixture = self.fixture
        self.assertTrue(fixture._store().created)
        self.assertEqual(project_revision_image_inspections(fixture.revision.pk, fixture.token).projected, 1)
        fixture.source.refresh_from_db()
        inspection = fixture.source.attachment_media[0]["inspection"]
        self.assertEqual(inspection["sentiment"], "unknown")
        self.assertEqual(inspection["analysis_state"], "legacy_unknown")

    def test_real_complaint_permission_epoch_rechecks_database_despite_cached_client(self):
        from management.models import IgClient
        from management.services.ig_revision_proposal import validated_media_complaint_evidence
        fixture = self.fixture
        self.assertTrue(fixture._store(turn_intelligence=self.artifact(sentiment="negative",
            complaint_code="current_customer_complaint")).created)
        fixture.revision.refresh_from_db()
        cached_epoch = fixture.revision.client.reply_permission_epoch
        IgClient.objects.filter(pk=fixture.client_row.pk).update(reply_permission_epoch=cached_epoch + 1)
        self.assertEqual(validated_media_complaint_evidence(fixture.revision),
            ([], "media_complaint_scope_changed"))

    def assert_projection_preserves_uninspected_source(self):
        from management.services.ig_revision_proposal import project_revision_image_inspections
        fixture = self.fixture
        result = project_revision_image_inspections(fixture.revision.pk, fixture.token)
        self.assertEqual(result.projected, 0, result)
        fixture.source.refresh_from_db()
        self.assertNotIn("inspection", fixture.source.attachment_media[0])

    def test_projection_cannot_inspect_source_after_explicit_conversation_reset(self):
        from management.models import IgFunnelResetAudit
        fixture = self.fixture
        self.assertTrue(fixture._store(turn_intelligence=self.artifact()).created)
        IgFunnelResetAudit.objects.create(client=fixture.client_row,
            reset_after_message_id=fixture.source.pk, reason="Explicit reset")
        self.assert_projection_preserves_uninspected_source()

    def test_projection_cannot_inspect_changed_caption_or_manager_owned_media(self):
        fixture = self.fixture
        self.assertTrue(fixture._store(turn_intelligence=self.artifact()).created)
        fixture.source.text = "Caption changed after accepted request"
        fixture.source.save(update_fields=["text"])
        self.assert_projection_preserves_uninspected_source()
        fixture.source.role = "manager"
        fixture.source.save(update_fields=["role"])
        self.assert_projection_preserves_uninspected_source()

    def test_projection_requires_current_part_mime_position_bytes_and_private_ownership(self):
        fixture = self.fixture
        self.assertTrue(fixture._store(turn_intelligence=self.artifact()).created)
        original = deepcopy(fixture.source.attachment_media)
        for field, value in (("mime", "video/webm"), ("original_index", 9), ("bytes", 999),
            ("private_storage", False), ("capture_state", "expired")):
            with self.subTest(field=field):
                fixture.source.attachment_media = deepcopy(original)
                fixture.source.attachment_media[0][field] = value
                fixture.source.save(update_fields=["attachment_media"])
                self.assert_projection_preserves_uninspected_source()

    def test_projection_cannot_write_analysis_after_private_media_deletion_begins(self):
        fixture = self.fixture
        self.assertTrue(fixture._store(turn_intelligence=self.artifact()).created)
        fixture.source.private_media_state = fixture.source.PrivateMediaState.DELETE_PENDING
        fixture.source.save(update_fields=["private_media_state"])
        self.assert_projection_preserves_uninspected_source()


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class RevisionMediaComplaintBridgeTests(TransactionTestCase):
    setUp = RevisionMediaAnalysisProjectionTests.setUp
    artifact = RevisionMediaAnalysisProjectionTests.artifact

    def candidate(self, **changes):
        payload = {"reply_text": "Допоможемо з цим питанням.", "controls": [{"kind": "manager", "value": True}],
            "turn_intelligence": {"catalog_candidates": [], "transcript": "", "intent": "media_review",
                "audio_status": "not_applicable", "confidence": .9,
                "image_observations": [{"source_image_index": 0, "outcome": "understood",
                    "evidence_code": "visual_content", "type_code": "product"}],
                "media_observations": [observation(**({"sentiment": "negative", "complaint_code": "current_customer_complaint",
                    "evidence": "Customer reports torn seam"} | changes))]}}
        result = parse_structured_response(payload)
        self.assertTrue(result.valid, result.error)
        return result

    def capture(self, response, *, usage=None):
        from management.services.ig_turn_lineage import turn_lineage, bind_request_id
        from management.services.ig_revision_intents import capture_candidate_media_complaint_evidence
        fixture = self.fixture
        if usage is None:
            usage = {"_request_inline_count": 1, "_request_inline_content_hashes": [fixture.content_hash]}
        with turn_lineage(lane="live", client_id=fixture.client_row.pk, source_message_id=fixture.source.pk,
            logical_turn_id=f"ig-revision:{fixture.revision.pk}"):
            bind_request_id(fixture.request_id)
            return capture_candidate_media_complaint_evidence(fixture.revision, response,
                request_media_binding=fixture._media_manifest(), usage=usage)

    def store_manager_proposal(self, *, analysis=True):
        from management.services.ig_revision_authority import build_revision_authority_bindings, CLAIM_PUBLIC_POLICY_INPUTS
        fixture = self.fixture
        artifact = self.artifact(sentiment="negative", complaint_code="current_customer_complaint") if analysis else fixture._intelligence()
        authority = build_revision_authority_bindings(fixture.client_row, claims=(CLAIM_PUBLIC_POLICY_INPUTS,),
            settings_obj=fixture.settings, server_authorized_actions=("manager_escalation_intent",))
        result = fixture._store(turn_intelligence=artifact, response=self.candidate(), authority=authority)
        self.assertTrue(result.created, result.reasons)
        fixture.revision.refresh_from_db()
        return result

    def assert_no_case(self):
        from management.models import IgFollowUpTask, IgBotNotification, InstagramBotMessage
        self.assertFalse(IgFollowUpTask.objects.filter(kind=IgFollowUpTask.Kind.MANAGER_TASK).exists())
        self.assertFalse(IgBotNotification.objects.exists())
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())

    def test_real_candidate_current_complaint_admits_existing_manager_case_reason_only(self):
        from management.services.ig_revision_intents import manager_case_reason
        response = self.candidate()
        cap, reason = self.capture(response)
        self.assertFalse(reason)
        self.fixture.revision._candidate_media_complaint = cap
        self.assertEqual(manager_case_reason(self.fixture.revision, response), "media_complaint_review")
        self.assert_no_case()

    def test_actual_generation_boundary_grants_manager_only_after_source_bound_candidate(self):
        from management.services.ig_revision_live import RevisionGenerationBoundary
        fixture = self.fixture
        boundary = RevisionGenerationBoundary(fixture.revision, fixture.token, fixture.settings,
            fixture.publication_binding, has_images=True)
        response = self.candidate()
        before = boundary.response_authority(response)
        self.assertNotIn("manager_escalation_intent", before.allowed_actions)
        cap, reason = self.capture(response)
        self.assertFalse(reason)
        fixture.revision._candidate_media_complaint = cap
        after = boundary.response_authority(response)
        self.assertTrue(after.ready, after.reasons)
        self.assertIn("manager_escalation_intent", after.allowed_actions)
        self.assertNotIn("checkout_proposal_create", after.allowed_actions)
        self.assertNotIn("prize_review_case_create", after.allowed_actions)
        self.assert_no_case()

    def test_real_candidate_normalized_response_preserves_same_artifact_digest(self):
        from management.services.ig_revision_intents import manager_case_reason
        response = self.candidate()
        cap, reason = self.capture(response)
        self.assertFalse(reason)
        self.fixture.revision._candidate_media_complaint = cap
        normalized = replace(response, reply_text="Підкажіть, що сталося зі швом?")
        self.assertIs(normalized.turn_intelligence, response.turn_intelligence)
        self.assertEqual(manager_case_reason(self.fixture.revision, normalized), "media_complaint_review")

    def test_real_candidate_same_identity_mutated_artifact_digest_denies_authority(self):
        from management.services.ig_revision_intents import manager_case_reason
        response = self.candidate()
        cap, reason = self.capture(response)
        self.assertFalse(reason)
        self.fixture.revision._candidate_media_complaint = cap
        object.__setattr__(response.turn_intelligence, "confidence", .1)
        self.assertEqual(manager_case_reason(self.fixture.revision, response), "")
        self.assert_no_case()

    def test_failed_attempt_same_kind_different_parsed_artifact_cannot_authorize_winner(self):
        from management.services.ig_revision_intents import manager_case_reason
        first, winner = self.candidate(), self.candidate()
        cap, reason = self.capture(first)
        self.assertFalse(reason)
        self.fixture.revision._candidate_media_complaint = cap
        self.assertEqual(first.turn_intelligence, winner.turn_intelligence)
        self.assertIsNot(first.turn_intelligence, winner.turn_intelligence)
        self.assertEqual(manager_case_reason(self.fixture.revision, winner), "")
        self.assert_no_case()

    def test_positive_tone_and_manager_control_without_complaint_grant_no_authority(self):
        from management.services.ig_revision_intents import manager_case_reason
        response = self.candidate(sentiment="positive", complaint_code="none")
        cap, reason = self.capture(response)
        self.assertIsNone(cap)
        self.assertEqual(reason, "media_complaint_absent")
        self.fixture.revision._candidate_media_complaint = cap
        self.assertEqual(manager_case_reason(self.fixture.revision, response), "")
        self.assert_no_case()

    def test_actual_request_hash_missing_or_wrong_rejects_candidate_before_any_case(self):
        for usage in ({"_request_inline_count": 1},
            {"_request_inline_count": 1, "_request_inline_content_hashes": ["f" * 64]}):
            with self.subTest(usage=usage):
                cap, reason = self.capture(self.candidate(), usage=usage)
                self.assertIsNone(cap)
                self.assertTrue(reason)
        self.assert_no_case()

    def test_stale_owned_part_after_capture_cannot_authorize_candidate(self):
        from management.services.ig_revision_intents import manager_case_reason
        response = self.candidate()
        cap, reason = self.capture(response)
        self.assertFalse(reason)
        self.fixture.revision._candidate_media_complaint = cap
        self.fixture.source.attachment_media[0]["content_hash"] = "f" * 64
        self.fixture.source.save(update_fields=["attachment_media"])
        self.assertEqual(manager_case_reason(self.fixture.revision, response), "")
        self.assert_no_case()

    def test_erasure_during_candidate_wait_cannot_authorize_case(self):
        from django.utils import timezone
        from management.models import IgClient
        from management.services.ig_revision_intents import manager_case_reason
        response = self.candidate()
        cap, reason = self.capture(response)
        self.assertFalse(reason)
        self.fixture.revision._candidate_media_complaint = cap
        IgClient.objects.filter(pk=self.fixture.client_row.pk).update(privacy_erasure_started_at=timezone.now())
        self.assertEqual(manager_case_reason(self.fixture.revision, response), "")
        self.assert_no_case()

    def test_actual_accepted_complaint_creates_one_queued_case_no_sent_receipt_and_replays(self):
        from management.models import IgFollowUpTask, IgBotNotification, InstagramBotMessage
        from management.services.ig_revision_intents import ensure_revision_manager_case
        self.store_manager_proposal()
        fixture = self.fixture
        result = ensure_revision_manager_case(fixture.revision.pk, fixture.token, settings_id=fixture.settings.pk)
        self.assertTrue(result.ready, result.reason)
        task = IgFollowUpTask.objects.get(pk=result.task_id)
        notification = IgBotNotification.objects.get(pk=result.notification_id)
        self.assertEqual(task.manager_context["case_kind"], "media_complaint_review")
        self.assertEqual(task.manager_context["media_complaint_evidence"][0]["content_hash"], fixture.content_hash)
        self.assertEqual(notification.status, IgBotNotification.Status.PENDING)
        self.assertEqual(task.manager_approval_status, IgFollowUpTask.ManagerApprovalStatus.PENDING)
        self.assertFalse(InstagramBotMessage.objects.filter(role="manager").exists())
        replay = ensure_revision_manager_case(fixture.revision.pk, fixture.token, settings_id=fixture.settings.pk)
        self.assertTrue(replay.replayed)
        self.assertEqual((replay.task_id, replay.notification_id), (result.task_id, result.notification_id))
        self.assertEqual(IgBotNotification.objects.count(), 1)

    def test_accepted_missing_media_analysis_does_not_make_arbitrary_manager_action(self):
        from management.services.ig_revision_intents import ensure_revision_manager_case
        self.store_manager_proposal(analysis=False)
        fixture = self.fixture
        result = ensure_revision_manager_case(fixture.revision.pk, fixture.token, settings_id=fixture.settings.pk)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "manager_case_not_requested")
        self.assert_no_case()

    def test_accepted_part_hash_changed_denies_case_without_notification(self):
        from management.services.ig_revision_intents import ensure_revision_manager_case
        self.store_manager_proposal()
        fixture = self.fixture
        fixture.source.attachment_media[0]["content_hash"] = "f" * 64
        fixture.source.save(update_fields=["attachment_media"])
        result = ensure_revision_manager_case(fixture.revision.pk, fixture.token, settings_id=fixture.settings.pk)
        self.assertFalse(result.ready)
        self.assertTrue(result.reason)
        self.assert_no_case()

    def test_accepted_client_erasure_denies_case_without_notification(self):
        from django.utils import timezone
        from management.models import IgClient
        from management.services.ig_revision_intents import ensure_revision_manager_case
        self.store_manager_proposal()
        fixture = self.fixture
        IgClient.objects.filter(pk=fixture.client_row.pk).update(privacy_erasure_started_at=timezone.now())
        result = ensure_revision_manager_case(fixture.revision.pk, fixture.token, settings_id=fixture.settings.pk)
        self.assertFalse(result.ready)
        self.assertTrue(result.reason)
        self.assert_no_case()

    def test_accepted_complaint_reset_denies_case_without_notification(self):
        from management.models import IgFunnelResetAudit
        from management.services.ig_revision_intents import ensure_revision_manager_case
        self.store_manager_proposal()
        fixture = self.fixture
        IgFunnelResetAudit.objects.create(client=fixture.client_row,
            reset_after_message_id=fixture.source.pk, reason="Explicit reset")
        result = ensure_revision_manager_case(fixture.revision.pk, fixture.token, settings_id=fixture.settings.pk)
        self.assertFalse(result.ready)
        self.assert_no_case()

    def test_accepted_complaint_expired_private_owner_denies_case_without_notification(self):
        from management.services.ig_revision_intents import ensure_revision_manager_case
        self.store_manager_proposal()
        fixture = self.fixture
        fixture.source.private_media_state = fixture.source.PrivateMediaState.DELETE_PENDING
        fixture.source.save(update_fields=["private_media_state"])
        result = ensure_revision_manager_case(fixture.revision.pk, fixture.token, settings_id=fixture.settings.pk)
        self.assertFalse(result.ready)
        self.assert_no_case()

    def test_accepted_winner_model_mutation_is_not_valid_complaint_authority(self):
        from django.core.exceptions import ValidationError
        from management.models import GeminiRequestAttempt
        from management.services.ig_revision_intents import ensure_revision_manager_case
        self.store_manager_proposal()
        fixture = self.fixture
        with self.assertRaises(ValidationError):
            GeminiRequestAttempt.objects.filter(request_id=fixture.request_id).update(model="different-model")
        self.assertEqual(GeminiRequestAttempt.objects.get(request_id=fixture.request_id).model, fixture.model)
        self.assert_no_case()

    def test_legacy_binding_captures_actual_namespace_and_source_digest_then_refuses_caption_mutation(self):
        from management.services import instagram_bot
        from management.services.ig_revision_intents import validated_legacy_media_complaint_evidence
        fixture = self.fixture
        binding = instagram_bot._source_media_binding(fixture.source, [{"data": fixture.body, "mime": "image/jpeg",
            "source_part_id": fixture.part_id, "original_index": 0, "identity_origin": "ingress"}])
        normalized = instagram_bot._normalize_turn_media_binding([("image/jpeg", fixture.body)], binding)
        self.assertEqual(normalized["source_namespace"], fixture.source.provider_namespace)
        normalized.update(actual_inline_count=1, actual_content_hashes=[fixture.content_hash],
            request_id=fixture.request_id, provider_model=fixture.model)
        artifact = instagram_bot._validated_turn_intelligence(self.candidate().turn_intelligence, {}, normalized)
        artifact["request_permission_epoch"] = fixture.client_row.reply_permission_epoch
        proof, reason = validated_legacy_media_complaint_evidence(fixture.source, artifact)
        self.assertFalse(reason)
        self.assertEqual(proof[0]["content_hash"], fixture.content_hash)
        fixture.source.text = "Changed caption after provider returned"
        fixture.source.save(update_fields=["text"])
        self.assertEqual(validated_legacy_media_complaint_evidence(fixture.source, artifact),
            ([], "media_complaint_scope_changed"))
