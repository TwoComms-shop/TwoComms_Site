"""W4/W4B — ядро добивки в обязательном порядке плана: 052 → 047/048/049.

Порядок не косметика. Удлинение каскада без частотных лимитов — это спам и
риск для приложения Meta, поэтому IMP-052 идёт строго до всего остального.

Контекст с прода, который определил приоритеты (разведка W5):
`InstagramBotMessage.role` — user 1165, manager 1152, **model 20**. Бот ответил
20 раз на 289 клиентов, 249 диалогов вёл человек. Значит вся добивка строится
на пути, который почти не исполнялся, и первое, что нужно, — чтобы он был
безопасным и наблюдаемым, а не длинным.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from management.ig_bot_models import (
    IgClient,
    IgDeal,
    IgFollowUpTask,
    IgPaymentConfirmationReview,
    IgPaymentReviewDecision,
)
from management.models import InstagramBotMessage


class FollowupCoreMixin:
    def _client(self, key, *, stage=None, objection=None, level=0):
        client = IgClient.get_or_create_for_sender(key)
        client.stage = stage or IgClient.Stage.CHECKOUT
        client.primary_objection = objection or IgClient.Objection.NONE
        client.followup_level = level
        # Внутри окна Meta, иначе сработает ветка окна и тест проверит не то.
        client.last_message_at = timezone.now()
        client.save(update_fields=[
            "stage", "primary_objection", "followup_level",
            "last_message_at", "updated_at",
        ])
        return client

    def _sent_task(self, client, *, kind=None, ago_hours=1, text="привіт"):
        task = IgFollowUpTask.objects.create(
            client=client,
            due_at=timezone.now() - timedelta(hours=ago_hours),
            status=IgFollowUpTask.Status.SENT,
            kind=kind or IgFollowUpTask.Kind.QUALIFICATION,
            message_text=text,
        )
        IgFollowUpTask.objects.filter(pk=task.pk).update(
            sent_at=timezone.now() - timedelta(hours=ago_hours)
        )
        task.refresh_from_db()
        return task


class FrequencyLimitTests(FollowupCoreMixin, TestCase):
    """IMP-052: частотный лимит — предохранитель, а не удобство."""

    def test_second_automated_touch_within_18_hours_is_refused(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("freq-recent")
        self._sent_task(client, ago_hours=2)

        allowed, reason = _client_allows_followup(client)

        self.assertFalse(allowed)
        self.assertEqual(reason, "frequency_limit")

    def test_touch_after_18_hours_is_allowed(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("freq-old")
        self._sent_task(client, ago_hours=19)

        allowed, reason = _client_allows_followup(client)

        self.assertTrue(allowed, reason)

    def test_sixth_touch_in_30_days_is_refused(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("freq-monthly")
        for index in range(5):
            self._sent_task(client, ago_hours=24 * (index + 2))

        allowed, reason = _client_allows_followup(client)

        self.assertFalse(allowed)
        self.assertEqual(reason, "frequency_limit")

    def test_five_touches_in_30_days_still_allow_one_more(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("freq-monthly-ok")
        for index in range(4):
            self._sent_task(client, ago_hours=24 * (index + 2))

        allowed, reason = _client_allows_followup(client)

        self.assertTrue(allowed, reason)

    def test_touches_older_than_30_days_do_not_count(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("freq-ancient")
        for index in range(6):
            self._sent_task(client, ago_hours=24 * (40 + index))

        allowed, reason = _client_allows_followup(client)

        self.assertTrue(allowed, reason)

    def test_manager_tasks_do_not_count_towards_the_limit(self):
        """Задача менеджеру — не автосообщение клиенту."""
        from management.services.bot_followups import _client_allows_followup

        client = self._client("freq-manager-task")
        self._sent_task(
            client, kind=IgFollowUpTask.Kind.MANAGER_TASK, ago_hours=2
        )

        allowed, reason = _client_allows_followup(client)

        self.assertTrue(allowed, reason)


class SuppressionListTests(FollowupCoreMixin, TestCase):
    """IMP-052: полный список подавления вместо частичного."""

    def test_opt_out_is_a_standalone_reason(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("suppress-opt-out")
        client.opted_out_at = timezone.now()
        client.save(update_fields=["opted_out_at", "updated_at"])

        allowed, reason = _client_allows_followup(client)

        self.assertFalse(allowed)
        self.assertEqual(reason, "opted_out")

    def test_opt_in_after_opt_out_restores_followups(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("suppress-opt-in-again")
        client.opted_out_at = timezone.now() - timedelta(days=2)
        client.opted_in_at = timezone.now()
        client.save(update_fields=["opted_out_at", "opted_in_at", "updated_at"])

        allowed, reason = _client_allows_followup(client)

        self.assertTrue(allowed, reason)

    def test_cold_stage_is_suppressed(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client("suppress-cold", stage=IgClient.Stage.COLD)

        allowed, reason = _client_allows_followup(client)

        self.assertFalse(allowed)
        self.assertEqual(reason, "cold")

    def test_lead_to_manager_is_suppressed(self):
        from management.services.bot_followups import _client_allows_followup

        client = self._client(
            "suppress-lead-to-manager", stage=IgClient.Stage.LEAD_TO_MANAGER
        )

        allowed, reason = _client_allows_followup(client)

        self.assertFalse(allowed)
        self.assertEqual(reason, "lead_to_manager")

    def test_wholesale_interaction_is_suppressed(self):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.bot_followups import _client_allows_followup

        client = self._client("suppress-wholesale")
        message = InstagramBotMessage.objects.create(
            client=client, role=InstagramBotMessage.Role.USER, text="є опт?"
        )
        IgConversationAnalysisSnapshot.objects.create(
            client=client,
            last_analyzed_message=message,
            interaction_type=(
                IgConversationAnalysisSnapshot.InteractionType.WHOLESALE_B2B
            ),
            score_band=IgConversationAnalysisSnapshot.Band.QUALIFIED,
            dedupe_key="suppress-wholesale:1",
        )

        allowed, reason = _client_allows_followup(client)

        self.assertFalse(allowed)
        self.assertEqual(reason, "wholesale_b2b")

    def test_collaboration_interaction_is_suppressed(self):
        from management.ig_bot_models import IgConversationAnalysisSnapshot
        from management.services.bot_followups import _client_allows_followup

        client = self._client("suppress-collab")
        message = InstagramBotMessage.objects.create(
            client=client, role=InstagramBotMessage.Role.USER, text="колаб?"
        )
        IgConversationAnalysisSnapshot.objects.create(
            client=client,
            last_analyzed_message=message,
            interaction_type=(
                IgConversationAnalysisSnapshot.InteractionType.COLLABORATION
            ),
            score_band=IgConversationAnalysisSnapshot.Band.QUALIFIED,
            dedupe_key="suppress-collab:1",
        )

        allowed, reason = _client_allows_followup(client)

        self.assertFalse(allowed)
        self.assertEqual(reason, "collaboration")


class DuplicateTextTests(FollowupCoreMixin, TestCase):
    """IMP-052: дедуп текста — клиент не должен получить то же дважды."""

    def test_identical_text_is_not_scheduled_twice(self):
        from management.services.bot_followups import schedule_followup

        client = self._client("dedupe-text")
        self._sent_task(client, ago_hours=30, text="Ще актуально?")

        task = schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            delay=timedelta(hours=2),
            reason="qualification_unanswered",
            message_text="Ще актуально?",
        )

        self.assertIsNone(task)

    def test_different_text_is_scheduled(self):
        from management.services.bot_followups import schedule_followup

        client = self._client("dedupe-text-different")
        self._sent_task(client, ago_hours=30, text="Ще актуально?")

        task = schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            delay=timedelta(hours=2),
            reason="qualification_unanswered",
            message_text="Підкажіть розмір, і я відкладу",
        )

        self.assertIsNotNone(task)


class MetaWindowTaskTests(FollowupCoreMixin, TestCase):
    """IMP-049: выход за окно Meta не должен делать работу невидимой."""

    def _outside_window_client(self, key):
        client = self._client(key)
        client.last_message_at = timezone.now() - timedelta(hours=30)
        client.save(update_fields=["last_message_at", "updated_at"])
        return client

    def test_task_outside_the_window_stays_pending_for_a_manager(self):
        from management.services.bot_followups import schedule_followup

        client = self._outside_window_client("meta-window-pending")

        task = schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            delay=timedelta(hours=2),
            reason="qualification_unanswered",
        )

        self.assertIsNotNone(task)
        self.assertEqual(task.kind, IgFollowUpTask.Kind.MANAGER_TASK)
        self.assertEqual(
            task.status,
            IgFollowUpTask.Status.PENDING,
            "≈половина «подумаю»-добивок исчезала молча как SKIPPED",
        )
        self.assertEqual(task.skip_reason, "")

    def test_window_closed_is_recorded_as_a_reason_not_as_a_skip(self):
        from management.services.bot_followups import schedule_followup

        client = self._outside_window_client("meta-window-reason")

        task = schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            delay=timedelta(hours=2),
            reason="qualification_unanswered",
        )

        self.assertEqual(task.reason, "meta_window_closed")
        self.assertIsNotNone(task.meta_window_deadline)

    def test_manager_task_is_not_sent_to_the_client(self):
        """Регресс: задача менеджеру остаётся PENDING, но клиенту не уходит."""
        from unittest.mock import patch

        from management.services.bot_followups import (
            process_due_followups,
            schedule_followup,
        )

        client = self._outside_window_client("meta-window-not-sent")
        task = schedule_followup(
            client,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            delay=timedelta(hours=-1),
            reason="qualification_unanswered",
        )
        self.assertEqual(task.kind, IgFollowUpTask.Kind.MANAGER_TASK)

        with patch("management.services.instagram_bot.send_text") as send:
            process_due_followups()

        self.assertEqual(send.call_count, 0)
        task.refresh_from_db()
        self.assertNotEqual(task.status, IgFollowUpTask.Status.SENT)


class FinalOfferReachabilityTests(FollowupCoreMixin, TestCase):
    """IMP-047: ветка `pct == 10` была мёртвым кодом."""

    def test_price_objection_reaches_the_final_ten_percent_offer(self):
        from management.services.bot_followups import schedule_rescue_offer

        client = self._client(
            "final-offer-price", objection=IgClient.Objection.PRICE, level=1
        )
        client.discount_offered_percent = 5
        client.save(update_fields=["discount_offered_percent", "updated_at"])
        InstagramBotMessage.objects.create(client=client, sender_id="final-offer-price",
                                           role="user", text="Не можу собі дозволити за таку суму", status="done")

        task = schedule_rescue_offer(client, explicit_negotiation=True)

        self.assertIsNotNone(task)
        self.assertEqual(task.kind, IgFollowUpTask.Kind.FINAL)
        self.assertEqual(task.discount_percent, 10)
        self.assertEqual(
            task.manager_approval_status,
            IgFollowUpTask.ManagerApprovalStatus.PENDING,
        )

    def test_rescue_is_available_at_the_qualifying_stage(self):
        from management.services.bot_followups import schedule_rescue_offer

        client = self._client(
            "final-offer-qualifying", stage=IgClient.Stage.QUALIFYING, level=1
        )
        InstagramBotMessage.objects.create(client=client, sender_id="final-offer-qualifying",
                                           role="user", text="Для мене це занадто дорого", status="done")

        task = schedule_rescue_offer(client)

        self.assertIsNotNone(task)
        self.assertEqual(task.discount_percent, 5)
        self.assertEqual(
            task.manager_approval_status,
            IgFollowUpTask.ManagerApprovalStatus.PENDING,
        )

    def test_rescue_stays_unavailable_for_a_cold_client(self):
        from management.services.bot_followups import schedule_rescue_offer

        client = self._client(
            "final-offer-cold", stage=IgClient.Stage.COLD, level=1
        )

        self.assertIsNone(schedule_rescue_offer(client))

    def test_discount_never_exceeds_ten_percent(self):
        from management.services.bot_followups import next_discount_percent

        client = self._client("final-offer-cap", level=3)
        client.discount_offered_percent = 10
        client.save(update_fields=["discount_offered_percent", "updated_at"])

        self.assertEqual(
            next_discount_percent(client, explicit_negotiation=True), 0
        )


class DealPassthroughTests(FollowupCoreMixin, TestCase):
    """IMP-048: `deal=` не передавался, из-за чего платёжная ветка была мертва."""

    def test_payment_followup_is_chosen_when_the_deal_awaits_payment(self):
        from management.services.bot_followups import schedule_after_bot_reply

        client = self._client("deal-passthrough")
        deal = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.AWAITING_PAYMENT,
            payment_status="unpaid",
            amount=Decimal("990.00"),
        )

        task = schedule_after_bot_reply(client, reply="ось посилання", deal=deal)

        self.assertIsNotNone(task)
        self.assertEqual(task.kind, IgFollowUpTask.Kind.PAYMENT)
        self.assertEqual(task.deal_id, deal.pk)

    def test_reply_handler_passes_the_current_deal(self):
        """Проверяем сам вызов: без `deal=` ветка не выбирается никогда."""
        import inspect

        from management.services import instagram_bot

        source = inspect.getsource(instagram_bot)
        index = source.find("bot_followups.schedule_after_bot_reply(")
        self.assertGreater(index, -1)
        call = source[index:index + 260]
        self.assertIn("deal=", call)


class PaymentConfirmationEventTests(TestCase):
    """IMP-021: подтверждение оплаты клиенту не было детерминированным."""

    def test_payment_confirmed_kind_exists(self):
        from management.ig_bot_models import IgOrderCustomerEvent

        self.assertIn(
            "payment_confirmed",
            {value for value, _label in IgOrderCustomerEvent.Kind.choices},
        )

    def test_payment_confirmation_message_summarises_the_order(self):
        from management.services.ig_order_fulfillment import _message
        from orders.models import Order

        order = Order.objects.create(
            order_number="TWC-PAYCONF-01",
            full_name="Buyer",
            phone="380500000000",
            total_sum=Decimal("2100.00"),
            payment_status="paid",
            status="new",
        )

        text = _message("payment_confirmed", "uk", order, "")

        self.assertIn("TWC-PAYCONF-01", text)
        self.assertIn("2100", text)
        self.assertIn("оплат", text.lower())

    def test_payment_confirmation_is_localized(self):
        from management.services.ig_order_fulfillment import _message
        from orders.models import Order

        order = Order.objects.create(
            order_number="TWC-PAYCONF-02",
            full_name="Buyer",
            phone="380500000000",
            total_sum=Decimal("990.00"),
            payment_status="paid",
            status="new",
        )

        for locale, marker in (("en", "payment"), ("ru", "оплат")):
            with self.subTest(locale=locale):
                self.assertIn(
                    marker, _message("payment_confirmed", locale, order, "").lower()
                )

    def test_paid_order_materializes_a_confirmation_event(self):
        from django.contrib.auth import get_user_model as _get_user_model

        from management.ig_bot_models import IgOrderCustomerEvent
        from management.services.ig_order_assignments import link_order_to_client
        from management.services.ig_order_fulfillment import ensure_assignment_events
        from orders.models import Order

        manager = _get_user_model().objects.create_user(
            "payconf-manager", password="x", is_staff=True
        )
        client = IgClient.get_or_create_for_sender("payconf-client")
        order = Order.objects.create(
            order_number="TWC-PAYCONF-03",
            full_name="Buyer",
            phone="380500000000",
            total_sum=Decimal("1500.00"),
            payment_status="paid",
            status="new",
        )
        assignment = link_order_to_client(order, client=client, actor=manager)

        ensure_assignment_events(assignment)

        kinds = set(
            IgOrderCustomerEvent.objects.filter(order=order).values_list(
                "kind", flat=True
            )
        )
        self.assertIn(IgOrderCustomerEvent.Kind.PAYMENT_CONFIRMED, kinds)

    def test_unpaid_order_does_not_confirm_a_payment(self):
        from django.contrib.auth import get_user_model as _get_user_model

        from management.ig_bot_models import IgOrderCustomerEvent
        from management.services.ig_order_assignments import link_order_to_client
        from management.services.ig_order_fulfillment import ensure_assignment_events
        from orders.models import Order

        manager = _get_user_model().objects.create_user(
            "payconf-unpaid-manager", password="x", is_staff=True
        )
        client = IgClient.get_or_create_for_sender("payconf-unpaid-client")
        order = Order.objects.create(
            order_number="TWC-PAYCONF-04",
            full_name="Buyer",
            phone="380500000000",
            total_sum=Decimal("1500.00"),
            payment_status="unpaid",
            status="new",
        )
        assignment = link_order_to_client(order, client=client, actor=manager)

        ensure_assignment_events(assignment)

        kinds = set(
            IgOrderCustomerEvent.objects.filter(order=order).values_list(
                "kind", flat=True
            )
        )
        self.assertNotIn(IgOrderCustomerEvent.Kind.PAYMENT_CONFIRMED, kinds)
