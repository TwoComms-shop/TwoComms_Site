"""W3 / IMP-013 — «клієнт є покупцем» відокремлено від «провайдер підтвердив гроші».

DR-001 вимагав два предикати замість одного, але його рецепт («розширити
`client_has_verified_payment`, переиспользовав `manual_confirmation_q`») на
живих даних нездійсненний:

1. `manual_confirmation_q` прив'язаний до `IgDeal` через
   `payment_confirmation_reviews__`. На проді **всі 28 review мають
   `deal_id IS NULL`**, а у клієнта #59 немає жодного `IgDeal` взагалі.
2. `client_has_verified_payment` викликається у двох **грошових** місцях:
   `payment_link_allowed` (`instagram_bot.py:430`) і safety-net створення
   замовлення (`instagram_bot.py:5676`). Розширення предиката означало б,
   що покупець більше ніколи не отримає посилання на оплату, а будь-який
   контакт у його повідомленні запускав би створення замовлення.

Тому: строгий провайдерський предикат лишається незмінним, а поряд з'являється
`client_has_confirmed_purchase` — CRM-істина «ця людина у нас купувала».

Одиниця підрахунку покупок — **окреме реальне замовлення**, не review і не
projection. Причина конкретна: у клієнта #59 два review (superseded + confirmed)
вказують на один і той самий `order=296`, обидва з рішеннями `manager_verified`
на 2100.00. Підрахунок за review дав би 2 покупки замість однієї.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from management.ig_bot_models import (
    IgCommercialEpisode,
    IgClient,
    IgDeal,
    IgFollowCtaDecision,
    IgPaymentConfirmationReview,
    IgPaymentReviewDecision,
)
from orders.models import Order


class BuyerTruthTestMixin:
    def _client(self, key):
        return IgClient.get_or_create_for_sender(key)

    def _order(self, number, *, payment_status="paid", total="2100.00", status="ship"):
        return Order.objects.create(
            order_number=number,
            full_name="Buyer",
            phone="380500000000",
            total_sum=Decimal(total),
            payment_status=payment_status,
            status=status,
        )

    def _review(self, client, *, dedupe, status, order=None, deal=None):
        return IgPaymentConfirmationReview.objects.create(
            client=client,
            deal=deal,
            order=order,
            dedupe_key=dedupe,
            status=status,
        )

    def _manager_decision(self, review, *, amount="2100.00", scope=None):
        return IgPaymentReviewDecision.objects.create(
            review=review,
            client=review.client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_VERIFIED,
            verification_source="manager",
            verification_scope=(
                scope or IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT
            ),
            confirmed_amount=Decimal(amount) if amount is not None else None,
            amount_source="manager_input",
            actor=self.manager,
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id=str(self.manager.pk),
            review_status_before=IgPaymentConfirmationReview.Status.PENDING,
            review_status_after=IgPaymentConfirmationReview.Status.CONFIRMED,
        )


class ConfirmedPurchasePredicateTests(BuyerTruthTestMixin, TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            "buyer-truth-manager", password="x", is_staff=True
        )

    # ------------------------------------------------ ручне підтвердження
    def test_manager_confirmed_review_without_deal_makes_client_a_buyer(self):
        """Прод-конфігурація клієнта #59: review без дила, оплата підтверджена руками."""
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        client = self._client("buyer-manual-confirmed")
        review = self._review(
            client,
            dedupe="buyer-manual-confirmed:1",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        )
        self._manager_decision(review)

        self.assertTrue(client_has_confirmed_purchase(client))

    def test_superseded_review_alone_does_not_make_client_a_buyer(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        client = self._client("buyer-superseded-only")
        review = self._review(
            client,
            dedupe="buyer-superseded-only:1",
            status=IgPaymentConfirmationReview.Status.SUPERSEDED,
        )
        self._manager_decision(review)

        self.assertFalse(client_has_confirmed_purchase(client))

    def test_pending_review_does_not_make_client_a_buyer(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        client = self._client("buyer-pending-review")
        self._review(
            client,
            dedupe="buyer-pending-review:1",
            status=IgPaymentConfirmationReview.Status.PENDING,
        )

        self.assertFalse(client_has_confirmed_purchase(client))

    def test_rejected_decision_does_not_make_client_a_buyer(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        client = self._client("buyer-rejected-decision")
        review = self._review(
            client,
            dedupe="buyer-rejected-decision:1",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        )
        IgPaymentReviewDecision.objects.create(
            review=review,
            client=client,
            decision=IgPaymentReviewDecision.Decision.MANAGER_REJECTED,
            verification_source="manager",
            verification_scope=IgPaymentReviewDecision.VerificationScope.FULL_PAYMENT,
            reason_code="payment_not_found",
            actor=self.manager,
            actor_source=IgPaymentReviewDecision.ActorSource.MANAGEMENT_USER,
            actor_external_id=str(self.manager.pk),
        )

        self.assertFalse(client_has_confirmed_purchase(client))

    # -------------------------------------------------- прив'язане замовлення
    def test_active_assignment_to_paid_order_makes_client_a_buyer(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase
        from management.services.ig_order_assignments import link_order_to_client

        client = self._client("buyer-assigned-paid-order")
        order = self._order("TWC-BUYER-PAID")
        link_order_to_client(order, client=client, actor=self.manager)

        self.assertTrue(client_has_confirmed_purchase(client))

    def test_assignment_to_unpaid_order_does_not_make_client_a_buyer(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase
        from management.services.ig_order_assignments import link_order_to_client

        client = self._client("buyer-assigned-unpaid-order")
        order = self._order(
            "TWC-BUYER-UNPAID", payment_status="unpaid", status="new"
        )
        link_order_to_client(order, client=client, actor=self.manager)

        self.assertFalse(client_has_confirmed_purchase(client))

    def test_unlinked_assignment_stops_counting_as_purchase(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase
        from management.services.ig_order_assignments import (
            link_order_to_client,
            unlink_order_from_client,
        )

        client = self._client("buyer-unlinked-order")
        order = self._order("TWC-BUYER-UNLINK")
        link_order_to_client(order, client=client, actor=self.manager)
        unlink_order_from_client(
            order,
            client=client,
            actor=self.manager,
            reason_code="manual_review",
            reason="wrong client",
        )

        self.assertFalse(client_has_confirmed_purchase(client))

    # ---------------------------------------------------- відсутність доказів
    def test_client_without_any_evidence_is_not_a_buyer(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        self.assertFalse(client_has_confirmed_purchase(self._client("buyer-nothing")))

    def test_bulk_annotation_does_not_mark_clients_without_assignments(self):
        """Пастка LEFT JOIN: `unassigned_at__isnull=True` матчить клієнтів БЕЗ привязок.

        На проді наївний фільтр давав 289 «покупців» із 289. Предикат мусить
        будуватися через `Exists`, як це вже зроблено в `annotate_verified_payment`.
        """
        from management.services.bot_payment_truth import annotate_confirmed_purchase
        from management.services.ig_order_assignments import link_order_to_client

        buyer = self._client("buyer-bulk-real")
        link_order_to_client(
            self._order("TWC-BUYER-BULK"), client=buyer, actor=self.manager
        )
        stranger = self._client("buyer-bulk-stranger")

        rows = {
            row.pk: row.has_confirmed_purchase
            for row in annotate_confirmed_purchase(IgClient.objects.all())
        }

        self.assertTrue(rows[buyer.pk])
        self.assertFalse(rows[stranger.pk])

    def test_provider_verified_deal_still_counts_as_purchase(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase

        client = self._client("buyer-provider-deal")
        IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=timezone.now(),
            amount=Decimal("790.00"),
        )

        self.assertTrue(client_has_confirmed_purchase(client))


class MoneyPathRegressionTests(BuyerTruthTestMixin, TestCase):
    """Строгий провайдерський предикат не має розширюватися разом із CRM-істиною."""

    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            "money-path-manager", password="x", is_staff=True
        )

    def test_manager_confirmed_buyer_is_not_provider_verified(self):
        from management.services.bot_payment_truth import client_has_verified_payment

        client = self._client("money-manual-confirmed")
        review = self._review(
            client,
            dedupe="money-manual-confirmed:1",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        )
        self._manager_decision(review)

        self.assertFalse(
            client_has_verified_payment(client),
            "ручне підтвердження не є провайдерською оплатою",
        )

    def test_payment_link_stays_allowed_for_manager_confirmed_buyer(self):
        """Повторна продажа покупцю мусить залишатися можливою."""
        from management.services.instagram_bot import payment_link_allowed
        from storefront.models import Category, Product

        client = self._client("money-repeat-buyer")
        review = self._review(
            client,
            dedupe="money-repeat-buyer:1",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        )
        self._manager_decision(review)
        from management.models import InstagramBotMessage
        from management.services.ig_commercial_episodes import (
            ensure_episode_for_review, start_repeat_episode,
        )

        ensure_episode_for_review(review)
        source = InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role="user", source="webhook",
            text="Хочу замовити ще одну футболку",
        )
        start_repeat_episode(
            client, repeat_kind="explicit_more", evidence_message_ids=[source.pk],
            confidence="1", analysis_model="deterministic_source",
            analysis_prompt_version="buyer-repeat-test",
        )
        client.refresh_from_db()
        category = Category.objects.create(name="Футболки", slug="tshirts-repeat")
        product = Product.objects.create(
            title="Футболка Харків",
            slug="tshirt-kharkiv-repeat",
            category=category,
            price=1050,
        )
        client.current_product = product
        client.current_size = "L"
        client.intent = IgClient.Intent.PAYMENT
        client.stage = IgClient.Stage.CHECKOUT
        client.save(update_fields=[
            "current_product", "current_size", "intent", "stage", "updated_at",
        ])

        self.assertTrue(
            payment_link_allowed(client, {"paylink": "full"}, "формую посилання"),
            "покупець з ручним підтвердженням має право на нову оплату",
        )


class BuyerAggregateTests(BuyerTruthTestMixin, TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            "aggregate-manager", password="x", is_staff=True
        )

    def test_two_reviews_on_one_order_count_as_one_purchase(self):
        """Прод-конфігурація #59: superseded + confirmed review на один заказ."""
        from management.services.bot_payment_truth import (
            recalculate_client_payment_aggregates,
        )

        client = self._client("aggregate-single-order")
        order = self._order("TWC-AGG-ONE")
        superseded = self._review(
            client,
            dedupe="aggregate-single-order:superseded",
            status=IgPaymentConfirmationReview.Status.SUPERSEDED,
            order=order,
        )
        self._manager_decision(superseded)
        confirmed = self._review(
            client,
            dedupe="aggregate-single-order:confirmed",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            order=order,
        )
        self._manager_decision(confirmed)

        recalculate_client_payment_aggregates(client)

        client.refresh_from_db()
        self.assertEqual(client.purchases_count, 1)
        self.assertEqual(client.total_spent, Decimal("2100.00"))
        self.assertTrue(client.conversion_flags.get("is_buyer"))

    def test_two_distinct_orders_count_as_two_purchases(self):
        from management.services.bot_payment_truth import (
            recalculate_client_payment_aggregates,
        )
        from management.services.ig_order_assignments import link_order_to_client

        client = self._client("aggregate-two-orders")
        first = self._order("TWC-AGG-A", total="500.00")
        second = self._order("TWC-AGG-B", total="700.00")
        link_order_to_client(first, client=client, actor=self.manager)
        link_order_to_client(second, client=client, actor=self.manager)

        recalculate_client_payment_aggregates(client)

        client.refresh_from_db()
        self.assertEqual(client.purchases_count, 2)
        self.assertEqual(client.total_spent, Decimal("1200.00"))

    def test_order_payable_total_excludes_discount(self):
        from management.services.bot_payment_truth import (
            recalculate_client_payment_aggregates,
        )
        from management.services.ig_order_assignments import link_order_to_client

        client = self._client("aggregate-discount")
        order = self._order("TWC-AGG-DISCOUNT", total="1000.00")
        order.discount_amount = Decimal("150.00")
        order.save(update_fields=["discount_amount"])
        link_order_to_client(order, client=client, actor=self.manager)

        recalculate_client_payment_aggregates(client)

        client.refresh_from_db()
        self.assertEqual(client.total_spent, Decimal("850.00"))

    def test_prepaid_order_without_amount_evidence_marks_amount_unknown(self):
        """Часткова оплата без підтвердженої суми не має вигадувати число."""
        from management.services.bot_payment_truth import (
            recalculate_client_payment_aggregates,
        )
        from management.services.ig_order_assignments import link_order_to_client

        client = self._client("aggregate-prepaid-unknown")
        order = self._order(
            "TWC-AGG-PREPAID", payment_status="prepaid", total="1500.00"
        )
        link_order_to_client(order, client=client, actor=self.manager)

        recalculate_client_payment_aggregates(client)

        client.refresh_from_db()
        self.assertEqual(client.purchases_count, 1)
        self.assertEqual(client.total_spent, Decimal("0.00"))
        self.assertTrue(client.conversion_flags.get("purchase_amount_unknown"))

    def test_manager_confirmed_prepayment_amount_is_used(self):
        from management.services.bot_payment_truth import (
            recalculate_client_payment_aggregates,
        )
        from management.services.ig_order_assignments import link_order_to_client

        client = self._client("aggregate-prepaid-known")
        order = self._order(
            "TWC-AGG-PREPAID-KNOWN", payment_status="prepaid", total="1500.00"
        )
        link_order_to_client(order, client=client, actor=self.manager)
        review = self._review(
            client,
            dedupe="aggregate-prepaid-known:1",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            order=order,
        )
        self._manager_decision(
            review,
            amount="600.00",
            scope=IgPaymentReviewDecision.VerificationScope.PREPAYMENT,
        )

        recalculate_client_payment_aggregates(client)

        client.refresh_from_db()
        self.assertEqual(client.purchases_count, 1)
        self.assertEqual(client.total_spent, Decimal("600.00"))
        self.assertFalse(client.conversion_flags.get("purchase_amount_unknown"))

    def test_non_buyer_aggregates_are_reset_to_zero(self):
        from management.services.bot_payment_truth import (
            recalculate_client_payment_aggregates,
        )

        client = self._client("aggregate-not-a-buyer")
        IgClient.objects.filter(pk=client.pk).update(
            purchases_count=3, total_spent=Decimal("999.00")
        )
        client.refresh_from_db()

        recalculate_client_payment_aggregates(client)

        client.refresh_from_db()
        self.assertEqual(client.purchases_count, 0)
        self.assertEqual(client.total_spent, Decimal("0.00"))
        self.assertFalse(client.conversion_flags.get("is_buyer"))


class CrmConsumerTests(BuyerTruthTestMixin, TestCase):
    """Кожен CRM-читач мусить бачити покупця, підтвердженого руками менеджера.

    До правки всі вони читали провайдерський предикат, і клієнт #59 — оплатив,
    отримав товар, обмінює розмір — виглядав як людина, що нічого не купувала.
    """

    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            "crm-consumer-manager", password="x", is_staff=True
        )
        self.buyer = self._client("crm-consumer-buyer")
        review = self._review(
            self.buyer,
            dedupe="crm-consumer-buyer:1",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
        )
        self._manager_decision(review)

    def test_analysis_band_is_paid_for_manager_confirmed_buyer(self):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.bot_sales_classifier import _analysis_band

        self.assertEqual(
            _analysis_band(self.buyer, {}),
            IgConversationAnalysisSnapshot.Band.PAID,
        )

    def test_aggregate_interaction_type_is_paid_order_waiting_for_buyer(self):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.bot_sales_classifier import _aggregate_interaction_type

        self.assertEqual(
            _aggregate_interaction_type(self.buyer, ()),
            IgConversationAnalysisSnapshot.InteractionType.PAID_ORDER_WAITING,
        )

    def test_required_truth_state_reports_confirmed_purchase(self):
        """Саме цей флаг робить IMP-014 спостережуваним у снапшоті."""
        from management.services.bot_conversation_analysis import _required_truth_state

        self.assertTrue(_required_truth_state(self.buyer)["verified_payment"])

    def test_followup_is_suppressed_for_manager_confirmed_buyer(self):
        from management.services.bot_followups import _client_allows_followup

        allowed, reason = _client_allows_followup(self.buyer)

        self.assertFalse(allowed)
        self.assertEqual(reason, "already_converted")

    def test_readiness_is_not_decayed_for_manager_confirmed_buyer(self):
        """F-SCORE-004: ввічливе «дякую» від покупця не має знімати 10 балів."""
        from management.services.bot_sales_classifier import classify_message

        IgClient.objects.filter(pk=self.buyer.pk).update(buying_readiness=50)
        self.buyer.refresh_from_db()

        classify_message(self.buyer, text="дякую", role="user")

        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.buying_readiness, 100)

    def test_funnel_reset_keeps_buyer_at_paid_stage(self):
        from management.services.ig_funnel_reset import reset_funnel

        result = reset_funnel(
            client_id=self.buyer.pk, actor=self.manager, reason="test reset"
        )

        self.assertTrue(result["ok"], result)
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.stage, IgClient.Stage.PAID)

    def test_funnel_reset_uses_linked_order_for_order_created_stage(self):
        from management.services.ig_funnel_reset import reset_funnel
        from management.services.ig_order_assignments import link_order_to_client

        link_order_to_client(
            self._order("TWC-RESET-LINKED"), client=self.buyer, actor=self.manager
        )

        result = reset_funnel(
            client_id=self.buyer.pk, actor=self.manager, reason="test reset"
        )

        self.assertTrue(result["ok"], result)
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.stage, IgClient.Stage.ORDER_CREATED)

    def test_funnel_reset_cancels_unreserved_follow_decisions_but_keeps_sent_history(self):
        from management.services.ig_funnel_reset import reset_funnel

        client = self._client("follow-reset-client")
        episode = IgCommercialEpisode.objects.create(
            client=client,
            sequence=1,
            open_slot=1,
            materialization_key="follow-reset:episode",
        )
        client.current_commercial_episode = episode
        client.save(update_fields=["current_commercial_episode", "updated_at"])
        prepared = IgFollowCtaDecision.objects.create(
            trigger_key="follow-reset-prepared",
            client=client,
            commercial_episode=episode,
            opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
            state=IgFollowCtaDecision.State.PREPARED,
            episode_slot_key="follow-reset-slot",
        )
        sent = IgFollowCtaDecision.objects.create(
            trigger_key="follow-reset-sent",
            client=client,
            commercial_episode=episode,
            opportunity=IgFollowCtaDecision.Opportunity.PAYMENT,
            state=IgFollowCtaDecision.State.SENT,
            episode_slot_key="follow-reset-sent-slot",
        )

        result = reset_funnel(client_id=client.pk, actor=self.manager, reason="follow reset")

        self.assertTrue(result["ok"], result)
        prepared.refresh_from_db()
        sent.refresh_from_db()
        self.assertEqual(prepared.state, IgFollowCtaDecision.State.CANCELLED)
        self.assertIsNone(prepared.episode_slot_key)
        self.assertEqual(sent.state, IgFollowCtaDecision.State.SENT)


class BuyerTruthBackfillCommandTests(BuyerTruthTestMixin, TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_user(
            "backfill-manager", password="x", is_staff=True
        )
        self.buyer = self._client("backfill-buyer")
        review = self._review(
            self.buyer,
            dedupe="backfill-buyer:1",
            status=IgPaymentConfirmationReview.Status.CONFIRMED,
            order=self._order("TWC-BACKFILL-01"),
        )
        self._manager_decision(review)

    def test_dry_run_reports_change_without_writing(self):
        from io import StringIO

        from django.core.management import call_command

        stdout = StringIO()
        call_command("backfill_ig_buyer_truth", "--dry-run", stdout=stdout)
        output = stdout.getvalue()

        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.purchases_count, 0)
        self.assertIn("dry-run", output)
        self.assertIn(str(self.buyer.pk), output)

    def test_apply_writes_aggregates(self):
        from io import StringIO

        from django.core.management import call_command

        call_command("backfill_ig_buyer_truth", "--apply", stdout=StringIO())

        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.purchases_count, 1)
        self.assertEqual(self.buyer.total_spent, Decimal("2100.00"))
        self.assertTrue(self.buyer.conversion_flags.get("is_buyer"))

    def test_apply_is_idempotent(self):
        from io import StringIO

        from django.core.management import call_command

        call_command("backfill_ig_buyer_truth", "--apply", stdout=StringIO())
        second = StringIO()
        call_command("backfill_ig_buyer_truth", "--apply", stdout=second)

        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.purchases_count, 1)
        self.assertIn("changed=0", second.getvalue())
