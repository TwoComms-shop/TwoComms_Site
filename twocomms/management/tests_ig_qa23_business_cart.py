"""Business assortment cannot become or edit a personal retail cart."""
import hashlib
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import (
    IgClient, IgCommerceSelectionSession, IgCommerceSelectionTransition,
    IgCommerceTurnDecision, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_business_cooperation import business_cooperation_for_sources
from management.services.ig_commerce_state import _business_cart_observation_reason
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_revision_commerce import reduce_inbound_commerce_source
from management.services.ig_revision_intents import collaboration_brief_for_revision


URBAN = (
    "Добрий день! Хочу запустити Instagram-магазин URBAN DROP в Україні. "
    "Цікавить співпраця з продажу чорних oversize-футболок. Підкажіть: "
    "1 Яка ціна для партнера? 2 Які є розміри та кольори? "
    "3 Чи можна почати без закупівлі партії товару? "
    "4 Чи можна продавати через Instagram і які умови доставки? "
    "5 Чи дозволяєте використовувати ваші фото товарів у магазині? Дякую!"
)
BUSINESS = (
    URBAN,
    "Хочу открыть Instagram-магазин. Интересует продажа черных oversize-футболок. Какая цена для партнера?",
    "I am launching an Instagram store. Interested in selling black oversize t-shirts. What are partner prices?",
)
PERSONAL = "Хочу купити білу футболку M для себе."
HYBRID = "Хочу відкрити магазин. Яка ціна для партнера? А для себе хочу купити білу футболку M."
CONFLICT = "Хочу відкрити магазин. Для магазину потрібні чорні худі. Яка ціна для партнера? А для себе хочу купити білу футболку M."


class BusinessCartBoundaryTests(SimpleTestCase):
    def test_business_is_an_observation_even_with_affirmed_assortment(self):
        for text in (*BUSINESS, "Хочу купити 100 чорних футболок оптом."):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(_business_cart_observation_reason(text, request, request), "business_source_observed")

    def test_unambiguous_personal_hybrid_keeps_original_request(self):
        request = parse_turn(HYBRID)
        self.assertTrue(request.purchase_requested)
        self.assertEqual(request.field_updates.get("color"), "white")
        self.assertEqual(_business_cart_observation_reason(HYBRID, request, request), "")

    def test_conflicting_business_and_personal_choices_abstain(self):
        request = parse_turn(CONFLICT)
        self.assertEqual(_business_cart_observation_reason(CONFLICT, request, request), "business_personal_cart_ambiguous")

    def test_withdrawal_quotes_reports_and_negation_do_not_open_business(self):
        for text in (
            "Співпраця вже не цікавить. " + PERSONAL,
            '«Хочу відкрити магазин» — цитата. ' + PERSONAL,
            'My friend said: I want to open a store. I want to buy a white t-shirt for myself.',
            "Я не пропоную співпрацю. " + PERSONAL,
        ):
            with self.subTest(text=text):
                request = parse_turn(text)
                self.assertEqual(_business_cart_observation_reason(text, request, request), "")

    def test_negated_personal_purchase_does_not_waive_business_boundary(self):
        text = "Хочу відкрити магазин. Яка ціна для партнера? Для себе не хочу купити футболку."
        request = parse_turn(text)
        self.assertEqual(_business_cart_observation_reason(text, request, request), "business_personal_cart_ambiguous")


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_PROVIDER_TRANSPORT="instagram_login")
class NativeBusinessCartBoundaryTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.settings, _ = InstagramBotSettings.objects.update_or_create(pk=1, defaults={"ig_user_id": "qa23-business-owner"})
        self.customer = IgClient.objects.create(igsid="qa23-business-customer", language="uk")
        self.namespace = "instagram_login:qa23-business-owner"
        self.ordinal = 0

    def source(self, text):
        self.ordinal += 1
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", mid=f"qa23-business-{self.ordinal}", text=text,
            provider_namespace=self.namespace, provider_created_at=timezone.now())

    def reduce(self, source):
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.customer, source, expected_provider_namespace=self.namespace)
        self.assertTrue(result.ready, result.reason)
        self.customer.refresh_from_db()
        return result

    def test_fresh_business_never_bootstraps_retail_session_or_episode(self):
        for text in BUSINESS:
            with self.subTest(text=text):
                source = self.source(text)
                result = self.reduce(source)
                self.assertEqual(result.observations[0]["reason"], "business_source_observed")
                self.assertEqual(result.observations[0]["source_digest"], hashlib.sha256(text.encode()).hexdigest())
                self.assertFalse(IgCommerceSelectionSession.objects.filter(client=self.customer).exists())
                self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())
                self.assertIsNone(self.customer.current_commercial_episode_id)
                source.refresh_from_db()
                self.assertEqual(source.text, text)
                brief = collaboration_brief_for_revision(SimpleNamespace(bundle_snapshot={"sources": [
                    {"message_id": source.pk, "role": "user", "text": source.text,
                     "source_digest": hashlib.sha256(text.encode()).hexdigest()}]}, snapshot_digest="qa23-owned"))
                self.assertTrue(brief["business_cooperation"])
                self.assertEqual(brief["business_cooperation"]["decision_owner"], "manager")

    def test_business_cannot_overwrite_existing_personal_configuration(self):
        self.reduce(self.source(PERSONAL))
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        before = session.snapshot()
        transitions = IgCommerceSelectionTransition.objects.filter(session=session).count()
        result = self.reduce(self.source(URBAN))
        self.assertEqual(result.observations[0]["reason"], "business_source_observed")
        session.refresh_from_db()
        self.assertEqual(session.snapshot(), before)
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(session=session).count(), transitions)

    def test_personal_hybrid_uses_original_source_provenance(self):
        from management.services.ig_commerce_projection import source_preferences_for

        source = self.source(HYBRID)
        self.reduce(source)
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual(session.lines[0]["color"], "white")
        self.assertEqual(session.lines[0]["size"], "M")
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        self.assertEqual(decision.result_payload["source_facts"]["source_digest"], hashlib.sha256(HYBRID.encode()).hexdigest())
        source.refresh_from_db()
        self.assertEqual(source.text, HYBRID)
        self.assertTrue(business_cooperation_for_sources([{"role": "user", "message_id": source.pk, "text": source.text}]))
        projection = source_preferences_for(self.customer)
        self.assertEqual(projection["values"]["color"], "white")
        self.assertEqual(projection["evidence"]["color"]["source_message_id"], source.pk)

    def test_conflicting_hybrid_preserves_existing_cart(self):
        self.reduce(self.source(PERSONAL))
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        before = session.snapshot()
        result = self.reduce(self.source(CONFLICT))
        self.assertEqual(result.observations[0]["reason"], "business_personal_cart_ambiguous")
        session.refresh_from_db()
        self.assertEqual(session.snapshot(), before)

    def test_business_withdrawal_then_personal_purchase_remains_retail(self):
        self.reduce(self.source(URBAN))
        self.reduce(self.source("Співпраця вже не цікавить. " + PERSONAL))
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual(session.lines[0]["color"], "white")
        self.assertEqual(session.lines[0]["size"], "M")
