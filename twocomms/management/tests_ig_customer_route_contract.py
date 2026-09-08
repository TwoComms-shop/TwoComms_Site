from dataclasses import FrozenInstanceError
import json

from django.test import SimpleTestCase

from management.services.ig_customer_route_contract import (
    KINDS, OPERATIONS, SCHEMA_VERSION, normalize_customer_routes,
)


class CustomerRouteContractTests(SimpleTestCase):
    def intent(self, kind="employment", **overrides):
        return {"kind": kind, "operation": "open", "evidence_message_ids": [11],
                "confidence": 0.9, **overrides}

    def envelope(self, *intents, **overrides):
        return {"schema_version": SCHEMA_VERSION, "intents": list(intents), **overrides}

    def assert_abstain(self, payload, reason):
        result = normalize_customer_routes(payload)
        self.assertTrue(result.abstained)
        self.assertIsNone(result.proposal)
        self.assertEqual(result.reason_code, reason)

    def test_simultaneous_employment_collaboration_and_purchase_are_preserved(self):
        result = normalize_customer_routes(self.envelope(self.intent(),
            self.intent("collaboration", subtype="creator"), self.intent("catalog"), focus_index=0))
        self.assertFalse(result.abstained)
        self.assertEqual([item.kind for item in result.proposal.intents], ["employment", "collaboration", "catalog"])
        self.assertEqual(result.proposal.focus_index, 0)
        self.assertEqual(result.proposal.to_dict()["schema_version"], SCHEMA_VERSION)

    def test_empty_missing_and_malformed_abstain_without_withdrawing(self):
        self.assert_abstain(None, "route_missing")
        self.assert_abstain(self.envelope(), "route_empty")
        self.assert_abstain({}, "route_fields_invalid")
        self.assert_abstain("withdraw", "route_shape_invalid")
        result = normalize_customer_routes(self.envelope(self.intent(operation="withdraw")))
        self.assertFalse(result.abstained)
        self.assertEqual(result.proposal.intents[0].operation, "withdraw")
        self.assertEqual(len(result.proposal.intents), 1)

    def test_finite_kinds_operations_and_collaboration_subtypes(self):
        for kind in sorted(KINDS):
            for operation in sorted(OPERATIONS):
                with self.subTest(kind=kind, operation=operation):
                    self.assertFalse(normalize_customer_routes(self.envelope(self.intent(kind, operation=operation))).abstained)
        for subtype in ("designer", "partnership", "dropship", "wholesale_store", "creator", "other", "none"):
            self.assertFalse(normalize_customer_routes(self.envelope(self.intent("collaboration", subtype=subtype))).abstained)
        self.assert_abstain(self.envelope(self.intent(subtype="creator")), "route_subtype_invalid")
        self.assert_abstain(self.envelope(self.intent("collaboration", subtype="hiring")), "route_subtype_invalid")
        self.assert_abstain(self.envelope(self.intent(operation="complete")), "route_operation_invalid")

    def test_business_authority_keys_and_kinds_are_rejected_whole(self):
        for kind in ("paid", "spam", "block", "reward", "discount", "order", "manager"):
            self.assert_abstain(self.envelope(self.intent(kind)), "route_kind_invalid")
        for key in ("policy", "reason_text", "payment", "grant", "order_id", "manager_send", "vacancies_open"):
            self.assert_abstain(self.envelope(self.intent(), **{key: True}), "route_fields_invalid")
            self.assert_abstain(self.envelope(self.intent(), self.intent("catalog", **{key: True})), "route_intent_fields_invalid")

    def test_four_intents_and_eight_total_unique_refs_are_bounded(self):
        intents = [self.intent(kind, evidence_message_ids=[index * 2 + 1, index * 2 + 2])
                   for index, kind in enumerate(("employment", "catalog", "support", "information"))]
        self.assertFalse(normalize_customer_routes(self.envelope(*intents)).abstained)
        self.assert_abstain(self.envelope(*intents, self.intent("community")), "route_intent_limit")
        intents[-1]["evidence_message_ids"].append(9)
        self.assert_abstain(self.envelope(*intents), "route_evidence_limit")

    def test_evidence_ids_are_strict_and_duplicates_are_not_silently_dropped(self):
        for refs in ([], [True], [False], [0], [-1], [1.0], ["11"], None, {11}, list(range(1, 10))):
            with self.subTest(refs=refs):
                self.assert_abstain(self.envelope(self.intent(evidence_message_ids=refs)), "route_evidence_invalid")
        self.assert_abstain(self.envelope(self.intent(evidence_message_ids=[11, 11])), "route_evidence_duplicate")
        self.assert_abstain(self.envelope(self.intent(), self.intent(operation="withdraw")), "route_intent_duplicate")

    def test_confidence_is_finite_numeric_not_boolean_or_string(self):
        for confidence in (True, False, "0.9", None, float("nan"), float("inf"), -float("inf"), -0.1, 1.1, 10 ** 1000):
            with self.subTest(confidence_type=type(confidence).__name__):
                self.assert_abstain(self.envelope(self.intent(confidence=confidence)), "route_confidence_invalid")
        for confidence in (0, 1, 0.1234567, -0.0):
            self.assertFalse(normalize_customer_routes(self.envelope(self.intent(confidence=confidence))).abstained)

    def test_focus_indexes_only_an_existing_nonwithdrawn_intent(self):
        for focus in (True, False, "0", 0.0, -1, 1):
            self.assert_abstain(self.envelope(self.intent(), focus_index=focus), "route_focus_invalid")
        self.assert_abstain(self.envelope(focus_index=0), "route_focus_invalid")
        self.assert_abstain(self.envelope(self.intent(operation="withdraw"), focus_index=0), "route_focus_withdrawn")
        self.assertIsNone(normalize_customer_routes(self.envelope(self.intent(), focus_index=None)).proposal.focus_index)

    def test_schema_and_json_shapes_are_strict(self):
        self.assert_abstain(self.envelope(self.intent(), schema_version="customer-route.v2"), "route_schema_unsupported")
        self.assert_abstain({"schema_version": SCHEMA_VERSION, "intents": ()}, "route_intents_invalid")
        self.assert_abstain(self.envelope(None), "route_intent_shape_invalid")
        value = self.intent()
        del value["operation"]
        self.assert_abstain(self.envelope(value), "route_intent_fields_invalid")
        for key in ("kind", "subtype", "operation"):
            self.assertTrue(normalize_customer_routes(self.envelope(self.intent(**{key: []}))).abstained)

    def test_canonical_digest_and_detached_immutable_result(self):
        raw = self.envelope(self.intent(evidence_message_ids=[12, 11], confidence=1))
        result = normalize_customer_routes(raw).proposal
        equivalent = normalize_customer_routes(self.envelope(self.intent(subtype="none",
            evidence_message_ids=[11, 12], confidence=1.0), focus_index=None)).proposal
        self.assertEqual(result.digest, equivalent.digest)
        self.assertEqual(len(result.digest), 64)
        self.assertEqual(normalize_customer_routes(json.loads(json.dumps(result.to_dict()))).proposal, result)
        raw["intents"][0]["evidence_message_ids"].clear()
        exported = result.to_dict()
        exported["intents"][0]["operation"] = "withdraw"
        self.assertEqual(result.digest, equivalent.digest)
        with self.assertRaises(FrozenInstanceError):
            result.intents[0].operation = "withdraw"
        changed = normalize_customer_routes(self.envelope(self.intent(operation="continue",
            evidence_message_ids=[11, 12], confidence=1))).proposal
        self.assertNotEqual(result.digest, changed.digest)
