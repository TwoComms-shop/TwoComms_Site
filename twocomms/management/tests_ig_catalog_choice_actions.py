"""Native catalog sources preserve real provenance and exact cart positions."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from management.services import ig_catalog_choice_actions as choices


class ChoiceOperationTests(SimpleTestCase):
    def test_sessionless_bridge_is_only_default_empty_first_product_state(self):
        proof = {"scope": {"session_id": None, "generation": 0, "selection_revision": 0,
            "candidate_generation": 0, "line_id": "", "recipient_id": "self"},
            "action": {"kind": "product", "product_id": 7}}
        before = {"lines": [], "active_index": 0, "revision": 0, "generation": 2, "state": "open"}
        self.assertTrue(choices._bootstrap_before(proof, before))
        for change in ({"lines": [{"line_id": "foreign", "product_id": 9}]},
            {"revision": 1}, {"query_constraints": {"purchase_requested": True}},
            {"last_provider_message_id": "older-source"}):
            self.assertFalse(choices._bootstrap_before(proof, {**before, **change}))
        self.assertFalse(choices._bootstrap_before({**proof, "scope": {**proof["scope"], "recipient_id": "friend"}}, before))
        self.assertFalse(choices._bootstrap_before({**proof, "action": {"kind": "size", "product_id": 7, "size": "L"}}, before))

    def test_second_product_adds_without_replacing_first_recipient_configuration(self):
        before = {"lines": [{"line_id": "first", "product_id": 7, "recipient_id": "friend", "size": "L"}]}
        operation, = choices._operations({"kind": "product", "product_id": 8}, {"recipient_id": "self"}, before)
        self.assertEqual(operation.operation, "add")
        self.assertEqual(operation.exact_product_id, 8)
        self.assertEqual(before["lines"][0]["size"], "L")

    def test_size_is_exact_line_and_recipient_scoped_even_for_duplicate_product(self):
        before = {"lines": [{"line_id": "one", "product_id": 7, "recipient_id": "self"},
            {"line_id": "two", "product_id": 7, "recipient_id": "friend"}]}
        operation, = choices._operations({"kind": "size", "product_id": 7, "size": "XXL"},
            {"line_id": "two", "recipient_id": "friend"}, before)
        self.assertEqual((operation.target_line_id, operation.target_recipient_id), ("two", "friend"))
        with self.assertRaises(ValueError):
            choices._operations({"kind": "size", "product_id": 7, "size": "L"},
                {"line_id": "two", "recipient_id": "self"}, before)

    def test_same_product_pick_selects_existing_position(self):
        before = {"lines": [{"line_id": "one", "product_id": 7, "recipient_id": "self"}]}
        operation, = choices._operations({"kind": "product", "product_id": 7}, {}, before)
        self.assertEqual(operation.operation, "select")

    def test_payload_value_cannot_introduce_stock_payment_or_purchase_semantics(self):
        before = {"lines": [{"line_id": "one", "product_id": 7, "recipient_id": "self"}]}
        operations = choices._operations({"kind": "color", "product_id": 7, "variant_id": 9,
            "label": "Койот", "purchase_requested": True}, {"line_id": "one"}, before)
        source = SimpleNamespace(pk=12, client_id=1, text="Койот", quick_reply_payload="CC1:3:0:seal",
            mid="user-mid", provider_message_id="user-mid", provider_namespace="instagram_login:owner",
            reply_to_provider_message_id="", provider_created_at=timezone.now(), created_at=timezone.now())
        delivered = {"invitation_id": 3, "manifest_digest": "a" * 64, "effect_id": 4,
            "provider_message_id": "sent-mid", "scope": {"line_id": "one"},
            "action": {"kind": "color", "product_id": 7, "variant_id": 9, "label": "Койот"}}
        request = choices._request(choices._proof(delivered, source, before, operations))
        self.assertFalse(request.purchase_requested)
        self.assertFalse(request.checkout_requested)
        self.assertEqual(dict(request.line_operations[0].field_updates), {"color": "Койот"})


class ChoiceReducerTests(TestCase):
    def setUp(self):
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        from management.models import IgClient, IgCommercialEpisode, IgCommerceSelectionSession
        SourceCartCatalogTests.setUp(self)
        self.customer = IgClient.objects.create(igsid="native-choice-customer")
        self.episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            materialization_key="native-choice-episode")
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=["current_commercial_episode"])
        self.session = IgCommerceSelectionSession.objects.create(client=self.customer,
            commercial_episode=self.episode, generation=1,
            lines=[{"line_id": "native-first", "quantity": 1, "recipient_id": "self"}])
        self.clock = timezone.now()
        self.serial = 0
        self.deliveries = {}
        witness = patch.object(choices, "_delivery", side_effect=lambda client, source, fresh: deepcopy(self.deliveries.get(source.pk,
            {"ready": False, "reason": "catalog_choice_not_delivered"})))
        witness.start()
        self.addCleanup(witness.stop)

    def source(self, action):
        from management.models import InstagramBotMessage
        self.serial += 1
        self.session.refresh_from_db()
        line = self.session.lines[self.session.active_index] if self.session.lines else {}
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="pending", text=action.get("label", "Обрати"),
            mid=f"native-choice-user-{self.serial}", quick_reply_payload=f"CC1:7:{self.serial}:signed",
            provider_namespace="instagram_login:choice-owner",
            provider_created_at=self.clock + timedelta(seconds=self.serial))
        self.deliveries[source.pk] = {"ready": True, "invitation_id": 7, "manifest_digest": "a" * 64,
            "effect_id": 8, "provider_message_id": "own-sent-menu",
            "scope": {"client_id": self.customer.pk, "episode_id": self.episode.pk,
                "session_id": self.session.pk, "selection_revision": self.session.revision,
                "line_id": line.get("line_id", ""), "recipient_id": line.get("recipient_id", "self")},
            "action": action}
        return source

    def consume(self, source):
        with transaction.atomic():
            return choices.consume_catalog_choice(self.customer, source)

    def test_product_and_size_use_real_sources_and_historical_projection(self):
        from management.services.ig_commerce_projection import source_preferences_for
        product = self.source({"kind": "product", "product_id": self.products[0].pk})
        picked = self.consume(product)
        self.assertTrue(picked["ready"], picked)
        size = self.source({"kind": "size", "product_id": self.products[0].pk, "size": "L", "label": "L"})
        selected = self.consume(size)
        self.assertTrue(selected["ready"], selected)
        with patch("management.services.ig_admin_state_capture._namespace", return_value=size.provider_namespace):
            facts = source_preferences_for(self.customer)
        self.assertEqual(facts["values"]["size"], "L")
        self.assertEqual(facts["evidence"]["size"]["source_message_id"], size.pk)
        self.assertEqual(facts["evidence"]["product_id"]["source_message_id"], product.pk)
        size.refresh_from_db()
        self.assertEqual(size.text, "L")
        self.assertEqual(size.source, "webhook")

    def test_zero_stock_configuration_choice_is_only_customer_wish(self):
        from management.models import IgCommerceTurnDecision
        product = self.source({"kind": "product", "product_id": self.products[0].pk})
        self.assertTrue(self.consume(product)["ready"])
        color = self.source({"kind": "color", "product_id": self.products[0].pk,
            "variant_id": self.variants[0].pk, "label": self.variants[0].color.name})
        result = self.consume(color)
        self.assertTrue(result["ready"], result)
        decision = IgCommerceTurnDecision.objects.get(pk=result["decision_id"])
        self.assertFalse(decision.request_payload["purchase_requested"])
        self.assertFalse(decision.request_payload["checkout_requested"])
        self.assertEqual(decision.result_payload["line_source_facts"]["lines"][0]["values"],
            {"color": self.variants[0].color.name})

    def test_changed_source_payload_invalidates_historical_choice_without_text_fallback(self):
        source = self.source({"kind": "product", "product_id": self.products[0].pk})
        self.assertTrue(self.consume(source)["ready"])
        source.quick_reply_payload = "CC1:7:99:foreign"
        source.save(update_fields=["quick_reply_payload"])
        self.assertIsNone(choices.native_choice_request(self.customer, source, historical=True))

    def test_duplicate_source_reuses_same_decision_and_transition(self):
        from management.models import IgCommerceTurnDecision, IgCommerceSelectionTransition
        source = self.source({"kind": "product", "product_id": self.products[0].pk})
        first, second = self.consume(source), self.consume(source)
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=source).count(), 1)
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(source_message=source).count(), 1)

    def test_unknown_delivery_never_creates_choice_decision(self):
        from management.models import IgCommerceTurnDecision
        source = self.source({"kind": "product", "product_id": self.products[0].pk})
        self.deliveries[source.pk] = {"ready": False, "reason": "catalog_choice_not_delivered"}
        self.assertFalse(self.consume(source)["ready"])
        self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())

    def test_actual_sessionless_pick_bootstraps_and_second_pick_keeps_first_position(self):
        from management.models import InstagramBotMessage, IgCommerceTurnDecision, IgCommerceSelectionSession
        self.session.delete()
        scope = {"client_id": self.customer.pk, "episode_id": self.episode.pk,
            "session_id": None, "generation": 0, "selection_revision": 0, "candidate_generation": 0,
            "line_id": "", "recipient_id": "self"}
        sources, results = [], []
        for index, product in enumerate(self.products):
            source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
                role="user", source="webhook", status="pending", text="Обрати",
                mid=f"sessionless-user-{index}", quick_reply_payload=f"CC1:7:{index}:signed",
                provider_namespace="instagram_login:choice-owner",
                provider_created_at=self.clock + timedelta(seconds=index + 1))
            self.deliveries[source.pk] = {"ready": True, "invitation_id": 7, "manifest_digest": "a" * 64,
                "effect_id": 8, "provider_message_id": "own-sent-menu", "scope": deepcopy(scope),
                "action": {"kind": "product", "product_id": product.pk}}
            result = self.consume(source)
            self.assertTrue(result["ready"], result)
            decision = IgCommerceTurnDecision.objects.select_related("transition__session", "transition__source_message").get(pk=result["decision_id"])
            self.assertTrue(choices.validated_choice_transition(decision.transition,
                invitation_id=7, manifest_digest="a" * 64))
            self.assertIsNotNone(choices.native_choice_request(self.customer, source, historical=True))
            sources.append(source)
            results.append(result)
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual([row["product_id"] for row in session.lines], [row.pk for row in self.products])
        self.assertEqual([row["recipient_id"] for row in session.lines], ["self", "self"])
        self.assertEqual(results[0]["proof"]["before_digest"], "")
        self.assertTrue(results[1]["proof"]["before_digest"])
        sources[0].refresh_from_db()
        self.assertEqual(sources[0].text, "Обрати")
        self.assertEqual(sources[0].source, "webhook")
