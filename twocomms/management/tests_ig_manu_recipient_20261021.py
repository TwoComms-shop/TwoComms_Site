"""Voluntary beneficiary context stays within its source-bound draft line."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgClient, IgCommerceTurnDecision, InstagramBotMessage, InstagramBotSettings
from management.services.ig_commerce_projection import capture_current_selection_lines, _line_operation_proof_matches
from management.services.ig_commerce_state import _apply_line_operations, _jsonable
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_memory_timeline_consumers import capture_useful_memory
from management.services.instagram_bot import ingress_provider_namespace
from storefront.models import Category, Product, ProductStatus


FIRST_MESSAGE = "Hello! I am in Kharkiv and I would like to buy a specific shirt for my boyfriend. Do you have a store? I have to leave tomorrow at 10 AM😭"


class BeneficiarySourceTests(SimpleTestCase):
    def test_voluntary_roles_are_source_facts_without_gift_or_identity_inference(self):
        for role in ("boyfriend", "girlfriend"):
            request = parse_turn(f"I would like a shirt for my {role}.")
            self.assertEqual(request.recipient_id, role)
            self.assertFalse(request.exact_product_id)
            self.assertEqual(dict(request.field_updates), {})
        self.assertEqual(parse_turn(FIRST_MESSAGE).recipient_id, "boyfriend")
        for text in ('He said "a shirt for my boyfriend".', "Previously I bought for my boyfriend.",
                "Not for my boyfriend.", "A shirt for my colleague."):
            self.assertEqual(parse_turn(text).recipient_id, "", text)

    def test_historical_line_operation_drift_cannot_retarget_recorded_self(self):
        operation = parse_turn("add t-shirt for my boyfriend").line_operations
        self.assertEqual(operation[0].recipient_id, "boyfriend")
        recorded = _jsonable(operation)
        self.assertTrue(_line_operation_proof_matches(recorded, operation))
        old_recorded = deepcopy(recorded)
        old_recorded[0]["recipient_id"] = ""
        self.assertFalse(_line_operation_proof_matches(old_recorded, operation))

    def test_same_friend_roles_require_line_binding_for_remove_and_correction(self):
        before = {"lines": [
            {"line_id": "line:1", "product_id": 111, "garment_type": "tshirt", "recipient_id": "friend", "size": "L"},
            {"line_id": "line:2", "product_id": 112, "garment_type": "tshirt", "recipient_id": "friend", "size": "M"}],
            "active_index": 1}
        original = deepcopy(before)
        for text in ("remove t-shirt for my friend", "change t-shirt for my friend to size XL"):
            after, facts, reason = _apply_line_operations(before, parse_turn(text), SimpleNamespace(pk=3))
            self.assertIsNone(after, text)
            self.assertEqual(facts, [])
            self.assertEqual(reason, "ambiguous_line_target")
            self.assertEqual(before, original)
        after, _, reason = _apply_line_operations(before, parse_turn("remove second t-shirt for my friend"), SimpleNamespace(pk=3))
        self.assertEqual(reason, "")
        self.assertEqual(after["lines"], [before["lines"][0]])
        after, _, reason = _apply_line_operations(before, parse_turn("change first t-shirt for my friend to size XL"), SimpleNamespace(pk=4))
        self.assertEqual(reason, "")
        self.assertEqual(after["lines"][0]["size"], "XL")
        self.assertEqual(after["lines"][1], before["lines"][1])


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class BeneficiaryDraftContinuityTests(TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        settings = InstagramBotSettings.objects.create(pk=1, ig_user_id="beneficiary-owner")
        self.namespace = ingress_provider_namespace(settings)
        self.customer = IgClient.objects.create(igsid="beneficiary-customer")
        category = Category.objects.create(name="Beneficiary fixture", slug="beneficiary-fixture")
        self.product = Product.objects.create(pk=111, title="Selected tee", slug="selected-beneficiary-tee",
            category=category, price=790, status=ProductStatus.PUBLISHED)
        self.now = timezone.now()
        self.ordinal = 0

    def reduce(self, text):
        self.ordinal += 1
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", text=text, mid=f"beneficiary-{self.ordinal}",
            provider_namespace=self.namespace,
            provider_created_at=self.now - timedelta(minutes=2) + timedelta(seconds=self.ordinal))
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        result = reduce_inbound_commerce_source(self.customer, source, expected_provider_namespace=self.namespace)
        self.assertTrue(result.ready, result.reason)
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        self.customer.refresh_from_db()
        decision.session.refresh_from_db()
        return source, decision

    def test_first_english_recipient_survives_later_sku_and_2xl_with_provenance(self):
        _, first = self.reduce(FIRST_MESSAGE)
        first_line = first.session.lines[0]
        self.assertEqual(first_line["recipient_id"], "boyfriend")
        self.assertNotIn("product_id", first_line)
        episode = self.customer.current_commercial_episode_id
        product_source, _ = self.reduce(f"https://twocomms.shop/product/{self.product.slug}/")
        size_source, sized = self.reduce("2XL")
        self.assertEqual((sized.session.lines[0]["recipient_id"], sized.session.lines[0]["product_id"], sized.session.lines[0]["size"]),
            ("boyfriend", 111, "XXL"))
        self.assertEqual(sized.session.lines[0]["line_id"], first_line["line_id"])
        self.assertEqual(self.customer.current_commercial_episode_id, episode)
        captured = capture_current_selection_lines(self.customer.pk, now=self.now)
        self.assertEqual(captured["status"], "captured", captured)
        line = captured["lines"][0]
        self.assertEqual(line["recipient_id"], "boyfriend")
        self.assertEqual(line["source_selection"]["scope"]["recipient_id"], "boyfriend")
        self.assertIn("product_id", line["fields"], line)
        self.assertEqual(line["fields"]["product_id"]["source"]["source_message_id"], product_source.pk)
        self.assertEqual(line["fields"]["size"]["source"]["source_message_id"], size_source.pk)
        self.assertNotIn("gift", sized.session.lines[0])

    def test_explicit_self_correction_discards_previous_recipient_configuration(self):
        self.reduce(FIRST_MESSAGE)
        self.reduce(f"https://twocomms.shop/product/{self.product.slug}/ size L")
        _, corrected = self.reduce("For myself.")
        self.assertEqual(corrected.session.lines[0]["recipient_id"], "self")
        self.assertNotIn("size", corrected.session.lines[0])
        self.assertNotIn("product_id", corrected.session.lines[0])
        self.assertIn("recipient_scope_changed", corrected.transition.reasons)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_MEMORY_TIMELINE_ENABLED=True,
    IG_MEMORY_GENERATION_ENABLED=True)
class BeneficiaryHistoricalQuoteTests(TransactionTestCase):
    def test_verified_historical_beneficiary_quote_is_useful_without_current_authority(self):
        from management.tests_ig_memory_timeline_consumers import TimelineConsumerCaptureTests
        fixture = TimelineConsumerCaptureTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        previous = fixture.source("I am buying for my boyfriend.", days=-20)
        fixture.publish(previous, topic="recipient")
        current = fixture.source("Now I am buying for myself.")
        context, memory = fixture.captured(current)
        from management.services.ig_turn_capture import detached_payload
        snapshot = capture_useful_memory(fixture.client, boundary=detached_payload(context.boundary), captured_memory=detached_payload(memory))
        self.assertEqual(snapshot["reason"], "historical_sources_verified", snapshot)
        self.assertEqual(snapshot["source_message_ids"], [previous.pk])
        self.assertEqual(snapshot["observations"][0]["quote"], previous.text)
        self.assertEqual(snapshot["observations"][0]["kinds"], ["recipient_history"])
        self.assertIs(snapshot["current_selection_authority"], False)
        self.assertNotIn("recipient_id", snapshot["observations"][0])
