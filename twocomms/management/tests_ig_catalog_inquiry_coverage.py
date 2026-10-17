"""An answered catalog reference covers only the exact understood product photo."""
from copy import deepcopy

from django.test import SimpleTestCase

from management.services.ig_media_analysis import bind_turn_media_analysis
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import parse_structured_response
from management.services.ig_response_plan import build_response_plan


class CatalogInquiryCoverageTests(SimpleTestCase):
    def fixture(self, *, count=1, admitted=None, kinds=None, outcomes=None, caption="", separate=False):
        admitted = count if admitted is None else admitted
        kinds, outcomes = kinds or ["product"] * count, outcomes or ["understood"] * count
        items = [{"source_message_id": 2 + index if separate else 2,
            "source_part_id": "mp1_" + str(index + 1) * 32, "original_index": 0 if separate else index,
            "content_hash": str(index + 3) * 64, "mime": "image/jpeg", "capture_state": "owned"}
            for index in range(count)]
        sources = [{"message_id": 1, "role": "user", "text": "Вітаю\nЦікавить ця футболка", "source_digest": "a" * 64}]
        for message_id in sorted({row["source_message_id"] for row in items}):
            sources.append({"message_id": message_id, "role": "user", "text": caption,
                "source_digest": "b" * 64, "media_parts": [{**row, "capture_outcome": "owned"}
                    for row in items if row["source_message_id"] == message_id]})
        observations = [{"source_image_index": index, "outcome": outcomes[index],
            "type_code": kinds[index], "evidence_code": "visual_content",
            "source_part_id": row["source_part_id"], "content_hash": row["content_hash"]}
            for index, row in enumerate(items[:admitted])]
        binding = {"items": items, "actual_inline_count": admitted,
            "actual_content_hashes": [row["content_hash"] for row in items[:admitted]],
            "request_id": "catalog-source-request", "provider_model": "actual-model", "outcomes": []}
        artifact = {"image_observations": observations, "catalog_candidates": [{"product_id": 112, "confidence": 1.0}],
            "catalog_resolution": "auto_select", "auto_product_id": 112,
            "media_request": {"request_id": binding["request_id"], "provider_model": binding["provider_model"]}}
        artifact["media_analysis"] = bind_turn_media_analysis(artifact, binding)
        plan = build_response_plan(preferences={"values": {"garment_type": "tshirt"},
            "evidence": {"garment_type": {"source_message_id": 1, "source_digest": "a" * 64}}},
            readiness={"missing": ["product"]}, context=ReplyTruthContext(), sources=sources)
        response = parse_structured_response({"reply_text": "Це футболка «Правил немає». Який розмір вам потрібен?",
            "controls": [{"kind": "product", "value": "112"}]})
        return plan, response, {"artifact": artifact, "binding": binding}

    def test_exact_catalog_photo_and_factual_garment_answer_complete_both_sources(self):
        plan, response, evidence = self.fixture()
        covered = plan.coverage(response, catalog_media_evidence=evidence)
        self.assertEqual(covered["covered"], ["1:garment_type", "2:media:catalog_reference"])
        self.assertEqual(covered["remaining"], [])
        self.assertEqual(covered["disposition"], "complete")
        self.assertTrue(plan.coverage(response)["remaining"])
        self.assertTrue(plan.coverage(response, catalog_media_evidence=evidence, local=True)["remaining"])

    def test_wrong_selected_candidate_or_ambiguous_match_cannot_complete_sources(self):
        for changes in ({"auto_product_id": 113}, {"catalog_resolution": "clarify"},
                {"catalog_candidates": [{"product_id": 113, "confidence": 1.0}]},
                {"catalog_candidates": [{"product_id": 112, "confidence": .4}]}):
            with self.subTest(changes=changes):
                plan, response, evidence = self.fixture()
                evidence["artifact"].update(changes)
                self.assertEqual(plan.coverage(response, catalog_media_evidence=evidence)["covered"], [])

    def test_changed_sealed_hash_and_invalid_actual_hash_remain_owed(self):
        plan, response, evidence = self.fixture()
        changed = deepcopy(plan)
        changed.catalog_inquiry["media_sources"][0]["parts"][0]["content_hash"] = "c" * 64
        self.assertEqual(changed.coverage(response, catalog_media_evidence=evidence)["covered"], [])
        evidence["binding"]["actual_content_hashes"] = ["d" * 64]
        self.assertEqual(plan.coverage(response, catalog_media_evidence=evidence)["covered"], [])

    def test_unreadable_or_unknown_media_never_gains_catalog_coverage(self):
        for outcome in ("unreadable", "uncertain"):
            with self.subTest(outcome=outcome):
                plan, response, evidence = self.fixture(outcomes=[outcome])
                self.assertEqual(plan.coverage(response, catalog_media_evidence=evidence)["covered"], [])
        plan, response, evidence = self.fixture()
        evidence["artifact"]["image_observations"] = []
        evidence["artifact"]["media_analysis"] = bind_turn_media_analysis(evidence["artifact"], evidence["binding"])
        self.assertEqual(plan.coverage(response, catalog_media_evidence=evidence)["covered"], [])

    def test_omitted_sibling_prevents_whole_source_completion(self):
        plan, response, evidence = self.fixture(count=2, admitted=1)
        self.assertEqual(plan.coverage(response, catalog_media_evidence=evidence)["covered"], [])

    def test_two_product_photos_cannot_share_one_unscoped_catalog_candidate(self):
        for separate in (False, True):
            with self.subTest(separate_sources=separate):
                plan, response, evidence = self.fixture(count=2, separate=separate)
                self.assertEqual(plan.coverage(response, catalog_media_evidence=evidence)["covered"], [])

    def test_understood_document_sibling_is_not_a_product_reference(self):
        plan, response, evidence = self.fixture(count=2, kinds=["product", "document"], separate=True)
        covered = plan.coverage(response, catalog_media_evidence=evidence)
        self.assertEqual(covered["covered"], [])
        self.assertEqual(covered["remaining"], ["1:garment_type", "2:media:catalog_reference", "3:media:catalog_reference"])
        self.assertEqual(covered["disposition"], "recovery")

    def test_independent_caption_question_is_not_answered_by_product_match(self):
        plan, response, evidence = self.fixture(caption="Яка доставка?")
        covered = plan.coverage(response, catalog_media_evidence=evidence)
        self.assertIn("2:info:shipping", covered["remaining"])
        self.assertNotIn("2:media:catalog_reference", covered["covered"])
        self.assertEqual(covered["disposition"], "recovery")
