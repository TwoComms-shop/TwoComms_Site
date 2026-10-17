"""A legacy catalog match is compatible with unknown new media taxonomy."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from django.test import SimpleTestCase

from management.services.ig_media_analysis import (
    MediaAnalysisError, bind_turn_media_analysis, media_reaction, validate_bound_media_analysis,
)
from management.services.ig_media_response import media_limitation_reply, normalize_media_reply
from management.services.ig_response_control import (
    TurnCatalogCandidate, TurnImageObservation, TurnIntelligenceArtifact,
)


class LegacyCatalogMediaReplyTests(SimpleTestCase):
    client = SimpleNamespace(language="uk")
    generated = "Це біла футболка «Правил немає». Який розмір вас цікавить?"

    def setUp(self):
        self.part = {"source_message_id": 3385, "source_part_id": "mp1_" + "a" * 32,
            "content_hash": "b" * 64, "original_index": 0, "capture_state": "owned",
            "mime": "image/jpeg"}
        self.binding = {"items": [self.part], "actual_inline_count": 1,
            "actual_content_hashes": [self.part["content_hash"]],
            "request_id": "source3385-request", "provider_model": "actual-model", "outcomes": []}
        self.artifact = TurnIntelligenceArtifact(
            catalog_candidates=(TurnCatalogCandidate(112, 1.0,
                "Біла футболка «Правил немає» з принтом на зображенні"),),
            image_observations=(TurnImageObservation(0, "understood", "visual_content", "product"),))

    def test_exact_bound_legacy_catalog_match_does_not_append_false_unreadable(self):
        source = bind_turn_media_analysis(self.artifact, self.binding)
        self.assertEqual(normalize_media_reply(self.client, self.generated, media_analysis=source),
            self.generated)
        row = source["parts"][0]
        self.assertEqual((row["analysis_state"], row["origin"], row["outcome"]),
            ("legacy_unknown", "legacy_observation", "understood"))
        self.assertEqual((row["content_kind"], row["sentiment"], row["confidence"]),
            ("unknown", "unknown", 0.0))
        self.assertEqual(row["content_hash"], self.part["content_hash"])
        self.assertEqual(media_reaction(source)["mode"], "neutral")
        self.assertEqual(media_reaction(source)["complaint_parts"], [])
        self.assertEqual(validate_bound_media_analysis(source, media=self.binding,
            request_id=self.binding["request_id"], provider_model=self.binding["provider_model"],
            legacy_images=self.artifact.image_observations), source)

    def test_repeated_final_normalization_is_stable(self):
        source = bind_turn_media_analysis(self.artifact, self.binding)
        first = normalize_media_reply(self.client, self.generated, media_analysis=source)
        self.assertEqual(normalize_media_reply(self.client, first, media_analysis=source), self.generated)

    def test_unknown_unreadable_and_absent_legacy_evidence_keep_limitation(self):
        for outcome in ("uncertain", "unreadable", None):
            with self.subTest(outcome=outcome):
                observations = () if outcome is None else (TurnImageObservation(
                    0, outcome, "insufficient_detail", "unknown"),)
                source = bind_turn_media_analysis(replace(self.artifact,
                    image_observations=observations), self.binding)
                result = normalize_media_reply(self.client, self.generated, media_analysis=source)
                self.assertIn(media_limitation_reply(self.client, media_kinds=("image",)), result)

    def test_catalog_candidate_alone_does_not_prove_image_readable(self):
        source = bind_turn_media_analysis(replace(self.artifact, image_observations=()), self.binding)
        self.assertIn("Не вдалося розібрати зображення", normalize_media_reply(
            self.client, self.generated, media_analysis=source))

    def test_understood_legacy_image_cannot_hide_unknown_or_omitted_sibling(self):
        second = self.part | {"source_part_id": "mp1_" + "c" * 32,
            "content_hash": "d" * 64, "original_index": 1}
        for count in (1, 2):
            with self.subTest(actual_inline_count=count):
                binding = self.binding | {"items": [self.part, second], "actual_inline_count": count,
                    "actual_content_hashes": [self.part["content_hash"], second["content_hash"]][:count]}
                source = bind_turn_media_analysis(self.artifact, binding)
                result = normalize_media_reply(self.client, self.generated, media_analysis=source)
                self.assertTrue(result.startswith(self.generated))
                self.assertIn("частину вкладень із зображеннями", result)

    def test_unavailable_audio_sibling_keeps_partial_audio_limitation(self):
        binding = self.binding | {"outcomes": [{"source_message_id": 3386,
            "source_part_id": "mp1_" + "c" * 32, "original_index": 0,
            "collection_outcome": "unavailable", "reason": "capture_failed"}]}
        source = bind_turn_media_analysis(self.artifact, binding)
        source["unavailable_kinds"] = ["audio"]
        result = normalize_media_reply(self.client, self.generated, media_analysis=source)
        self.assertIn("аудіочастину", result)

    def test_legacy_unknown_does_not_gain_inspection_social_or_reward_authority(self):
        source = bind_turn_media_analysis(self.artifact, self.binding)
        generated = ("Я бачу вас на фото. Ви чудово виглядаєте! За фото дамо знижку! "
            "Передала менеджеру. " + self.generated)
        self.assertEqual(normalize_media_reply(self.client, generated, media_analysis=source),
            self.generated)
        self.assertEqual(normalize_media_reply(self.client, generated,
            media_analysis=source, social_only=True), "Дякуємо, що поділилися!")

    def test_unbound_or_different_origin_rows_cannot_suppress_limitation(self):
        source = bind_turn_media_analysis(self.artifact, self.binding)
        for changes in ({"content_hash": ""}, {"source_part_id": ""},
            {"source_inline_index": None}, {"source_inline_index": True},
            {"original_index": None}, {"origin": "observation_missing"}, {"mime": "audio/ogg"}):
            with self.subTest(changes=changes):
                modified = deepcopy(source)
                modified["parts"][0].update(changes)
                self.assertIn("Не вдалося", normalize_media_reply(
                    self.client, self.generated, media_analysis=modified))

    def test_mismatched_submitted_hash_still_rejects_legacy_catalog_proof(self):
        with self.assertRaisesRegex(MediaAnalysisError, "media_analysis_binding_invalid"):
            bind_turn_media_analysis(self.artifact,
                self.binding | {"actual_content_hashes": ["e" * 64]})
