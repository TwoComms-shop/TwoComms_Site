"""IMP-053: follow-up cascades are data, not an if/elif side effect."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase

from management.models import (
    IgBotNotification,
    IgClient,
    IgCheckoutProposal,
    IgDeal,
    IgFollowUpTask,
    IgPaymentProjection,
    InstagramBotSettings,
)
from management.services.instagram_bot import ProviderDeliveryReceipt


KYIV = ZoneInfo("Europe/Kyiv")


class FollowupPolicyTableTests(TestCase):
    def test_paid_delivery_steps_use_a_dedicated_fulfillment_kind(self):
        from management.services.bot_followups import (
            FOLLOWUP_POLICIES,
            _persisted_step_kind,
        )

        kind_values = {value for value, _label in IgFollowUpTask.Kind.choices}
        self.assertIn("fulfillment", kind_values)
        self.assertEqual(
            [
                _persisted_step_kind(step)
                for step in FOLLOWUP_POLICIES["paid_missing_delivery"].steps
            ],
            ["fulfillment", "fulfillment", "fulfillment"],
        )

    def test_all_nine_designed_scenarios_have_explicit_steps_and_terminals(self):
        from management.services.bot_followups import FOLLOWUP_POLICIES

        self.assertEqual(
            set(FOLLOWUP_POLICIES),
            {
                "payment_link_unpaid",
                "price_quoted_silence",
                "thinking_hesitation",
                "price_objection",
                "missing_customer_size",
                "restock_wait",
                "paid_missing_delivery",
                "delivery_ready_unpaid",
                "first_reply_silence",
            },
        )
        for key, policy in FOLLOWUP_POLICIES.items():
            with self.subTest(policy=key):
                self.assertTrue(policy.steps)
                self.assertTrue(policy.terminal_conditions)
                self.assertEqual(
                    [step.index for step in policy.steps],
                    list(range(len(policy.steps))),
                )
                for step in policy.steps:
                    self.assertIn(step.trigger, {"time", "event", "reactive"})
                    self.assertTrue(step.condition)
                    self.assertTrue(step.copy_key)

    def test_policy_offsets_match_the_approved_funnel_design(self):
        from management.services.bot_followups import FOLLOWUP_POLICIES

        expected = {
            "payment_link_unpaid": [
                timedelta(minutes=25),
                timedelta(hours=4),
                timedelta(hours=23),
                None,
                timedelta(hours=72),
            ],
            "price_quoted_silence": [
                timedelta(hours=2),
                timedelta(hours=24),
                timedelta(hours=72),
            ],
            "thinking_hesitation": [
                timedelta(hours=20),
                timedelta(hours=72),
                timedelta(days=7),
            ],
            "price_objection": [timedelta(hours=24), timedelta(hours=96)],
            "missing_customer_size": [
                timedelta(minutes=40),
                timedelta(hours=6),
                timedelta(hours=36),
            ],
            "restock_wait": [timedelta(0), None, timedelta(days=14)],
            "paid_missing_delivery": [
                timedelta(minutes=20),
                timedelta(hours=3),
                timedelta(hours=20),
            ],
            "delivery_ready_unpaid": [timedelta(minutes=30)],
            "first_reply_silence": [timedelta(hours=2), timedelta(hours=20)],
        }
        self.assertEqual(
            {
                key: [step.offset for step in policy.steps]
                for key, policy in FOLLOWUP_POLICIES.items()
            },
            expected,
        )


class FollowupPolicyResolutionTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 3, 10, 0, tzinfo=KYIV)
        self.client_record = IgClient.get_or_create_for_sender("policy-client")
        self.client_record.last_message_at = self.now
        self.client_record.save(update_fields=["last_message_at", "updated_at"])

    def _deal(self, **overrides):
        values = {
            "client": self.client_record,
            "status": IgDeal.Status.QUOTED,
        }
        values.update(overrides)
        return IgDeal.objects.create(**values)

    def test_resolver_uses_commerce_facts_before_generic_stage(self):
        from management.services.bot_followups import resolve_followup_scenario

        deal = self._deal(status=IgDeal.Status.AWAITING_PAYMENT)
        self.client_record.stage = IgClient.Stage.CHECKOUT
        self.client_record.primary_objection = IgClient.Objection.PRICE
        self.client_record.save(
            update_fields=["stage", "primary_objection", "updated_at"]
        )

        self.assertEqual(
            resolve_followup_scenario(self.client_record, deal=deal),
            "payment_link_unpaid",
        )

    def test_resolver_separates_price_thinking_size_and_first_reply(self):
        from management.services.bot_followups import resolve_followup_scenario

        cases = (
            (IgClient.Stage.CHECKOUT, IgClient.Objection.PRICE, "", "price_objection"),
            (IgClient.Stage.CHECKOUT, IgClient.Objection.THINKING, "", "thinking_hesitation"),
            (IgClient.Stage.PRODUCT_MATCHED, IgClient.Objection.NONE, "", "missing_customer_size"),
            (IgClient.Stage.NEW, IgClient.Objection.NONE, "", "first_reply_silence"),
        )
        for stage, objection, size, expected in cases:
            with self.subTest(expected=expected):
                self.client_record.stage = stage
                self.client_record.primary_objection = objection
                self.client_record.current_size = size
                self.client_record.save(
                    update_fields=["stage", "primary_objection", "current_size", "updated_at"]
                )
                self.assertEqual(
                    resolve_followup_scenario(self.client_record),
                    expected,
                )

    def test_resolver_distinguishes_paid_missing_delivery_from_ready_unpaid(self):
        from management.services.bot_followups import resolve_followup_scenario

        paid = self._deal(status=IgDeal.Status.PAID)
        self.assertEqual(
            resolve_followup_scenario(self.client_record, deal=paid),
            "paid_missing_delivery",
        )

        unpaid_ready = self._deal(
            status=IgDeal.Status.QUOTED,
            np_full_name="Олена Тест",
            np_phone="+380500000000",
            np_city="Київ",
            np_office="1",
        )
        self.assertEqual(
            resolve_followup_scenario(self.client_record, deal=unpaid_ready),
            "delivery_ready_unpaid",
        )

    def test_ordinary_thinking_silence_does_not_schedule_another_step(self):
        from management.services.bot_followups import _schedule_next_policy_step

        sent = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.SENT,
            sent_at=self.now,
            kind=IgFollowUpTask.Kind.THINKING,
            reason="thinking_hesitation",
            level=0,
        )

        self.assertFalse(
            _schedule_next_policy_step(sent, self.client_record, now=self.now)
        )
        self.assertFalse(IgFollowUpTask.objects.filter(
            client=self.client_record,
            status=IgFollowUpTask.Status.PENDING,
        ).exists())
        sent.refresh_from_db()
        self.assertEqual((sent.status, sent.reason, sent.level), ("sent", "thinking_hesitation", 0))

    def test_ordinary_silence_completion_does_not_make_customer_cold(self):
        from management.services.bot_followups import _complete_policy_after_send

        before = (self.client_record.stage, self.client_record.lost_reason)
        terminal = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.SENT,
            sent_at=self.now,
            kind=IgFollowUpTask.Kind.THINKING,
            reason="thinking_hesitation",
            level=2,
        )

        self.assertTrue(_complete_policy_after_send(terminal, self.client_record))
        self.client_record.refresh_from_db()
        self.assertEqual((self.client_record.stage, self.client_record.lost_reason), before)


class FollowupPolicyIntegrationTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 3, 10, 0, tzinfo=KYIV)
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.save(update_fields=["is_enabled", "updated_at"])
        self.client_record = IgClient.get_or_create_for_sender("policy-integration")
        self.client_record.last_message_at = self.now
        self.client_record.language = "uk"
        self.client_record.save(
            update_fields=["last_message_at", "language", "updated_at"]
        )

    def _paid_missing_delivery_deal(self):
        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.PAID,
            payment_status="paid",
            paid_at=self.now,
        )
        IgPaymentProjection.objects.create(
            client=self.client_record,
            deal=deal,
            truth=IgDeal.PaymentTruth.CONFIRMED,
            gross_amount="1090.00",
            paid_at=self.now,
        )
        return deal

    def _source_budget_objection(self):
        from management.models import InstagramBotMessage
        from management.services.ig_objections import observe_inbound_objection

        message = InstagramBotMessage.objects.create(client=self.client_record, sender_id=self.client_record.igsid,
            role="user", source="webhook", status="done", mid="price-source-" + self._testMethodName,
            provider_namespace="instagram_login:policy-fixture", provider_created_at=self.now,
            text="Для мене це занадто дорого, не вкладаюся в бюджет.")
        observe_inbound_objection(self.client_record, message, "price", readiness=0)

    def _event_task(self, *, issue_at=None):
        issue_at = issue_at or self.now - timedelta(hours=24)
        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
            invoice_id="event-boundary-invoice",
            invoice_url="https://pay.example/event-boundary-invoice",
            invoice_expires_at=issue_at + timedelta(hours=24),
        )
        return IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
            level=3,
            event_key=f"invoice_expired:{deal.pk}:event-boundary-invoice",
            trigger=IgFollowUpTask.Trigger.EVENT,
            event_occurred_at=issue_at + timedelta(hours=24),
            event_payload={
                "event": "invoice_expired",
                "deal_id": deal.pk,
                "invoice_id": "event-boundary-invoice",
            },
            policy_started_at=issue_at,
            policy_version="followup-v1",
        )

    def _proposal_event_task(
        self, *, revision=1, payload_revision=None, status=None, event_occurred_at=None
    ):
        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.QUOTED,
        )
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal,
            revision=revision,
            catalog_total=Decimal("1090.00"),
            quoted_total=Decimal("1090.00"),
            requested_payment_amount=Decimal("1090.00"),
            items_digest=f"proposal-revision-{revision}",
        )
        proposal.expires_at = self.now - timedelta(minutes=1)
        if status is not None:
            proposal.status = status
        proposal.save(update_fields=["expires_at", "status", "updated_at"])
        return IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
            level=3,
            event_key=f"proposal_expired:{deal.pk}:{proposal.pk}:test",
            trigger=IgFollowUpTask.Trigger.EVENT,
            event_occurred_at=event_occurred_at or proposal.expires_at,
            event_payload={
                "event": "proposal_expired",
                "deal_id": deal.pk,
                "proposal_id": str(proposal.pk),
                "revision": revision if payload_revision is None else payload_revision,
            },
            policy_started_at=self.now - timedelta(minutes=26),
            policy_version="followup-v1",
        )

    def test_fulfillment_guard_allows_verified_buyer_but_sales_followup_does_not(self):
        from management.services.bot_followups import _client_allows_followup

        deal = self._paid_missing_delivery_deal()

        allowed, reason = _client_allows_followup(
            self.client_record,
            deal=deal,
            kind="fulfillment",
        )
        sales_allowed, sales_reason = _client_allows_followup(
            self.client_record,
            deal=deal,
            kind=IgFollowUpTask.Kind.PAYMENT,
        )

        self.assertTrue(allowed, reason)
        self.assertFalse(sales_allowed)
        self.assertEqual(sales_reason, "already_converted")

    def test_fulfillment_bypasses_sales_frequency_limit(self):
        from management.services.bot_followups import _client_allows_followup

        deal = self._paid_missing_delivery_deal()
        IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now - timedelta(hours=1),
            sent_at=self.now - timedelta(hours=1),
            status=IgFollowUpTask.Status.SENT,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
        )

        allowed, reason = _client_allows_followup(
            self.client_record,
            deal=deal,
            kind="fulfillment",
        )

        self.assertTrue(allowed, reason)

    @patch(
        "management.services.instagram_bot.send_text",
        return_value=ProviderDeliveryReceipt(True, "", "", "policy-g1"),
    )
    def test_fulfillment_g1_schedules_g2_without_mutating_sales_counters(self, _send_text):
        from management.services.bot_followups import process_due_followups

        deal = self._paid_missing_delivery_deal()
        self.client_record.followup_level = 4
        self.client_record.discount_offered_percent = 5
        self.client_record.save(
            update_fields=["followup_level", "discount_offered_percent", "updated_at"]
        )
        first = IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind="fulfillment",
            reason="paid_missing_delivery",
            level=0,
            meta_window_deadline=self.now + timedelta(hours=23),
        )

        self.assertEqual(
            process_due_followups(self.settings, now=self.now, limit=1),
            1,
        )

        first.refresh_from_db()
        self.client_record.refresh_from_db()
        second = IgFollowUpTask.objects.get(
            client=self.client_record,
            status=IgFollowUpTask.Status.PENDING,
            reason="paid_missing_delivery",
            level=1,
        )
        self.assertEqual(first.status, IgFollowUpTask.Status.SENT)
        self.assertEqual(second.kind, "fulfillment")
        self.assertEqual(second.due_at, self.now + timedelta(hours=2, minutes=40))
        self.assertEqual(self.client_record.followup_level, 4)
        self.assertEqual(self.client_record.discount_offered_percent, 5)

    @patch("management.services.instagram_bot.send_text")
    def test_fulfillment_is_skipped_when_delivery_becomes_complete(self, send_text):
        from management.services.bot_followups import process_due_followups

        deal = self._paid_missing_delivery_deal()
        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind="fulfillment",
            reason="paid_missing_delivery",
            level=1,
            meta_window_deadline=self.now + timedelta(hours=23),
        )
        deal.np_full_name = "Олена Тест"
        deal.np_phone = "+380500000000"
        deal.np_city = "Київ"
        deal.np_office = "1"
        deal.save(
            update_fields=["np_full_name", "np_phone", "np_city", "np_office", "updated_at"]
        )

        self.assertEqual(
            process_due_followups(self.settings, now=self.now, limit=1),
            0,
        )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(task.skip_reason, "policy_condition_changed")
        send_text.assert_not_called()

    @patch("management.services.instagram_bot._deliver_manager_notification", return_value=True)
    @patch(
        "management.services.instagram_bot.send_text",
        return_value=ProviderDeliveryReceipt(True, "", "", "policy-g3"),
    )
    def test_fulfillment_g3_persists_idempotent_manager_escalation(
        self,
        _send_text,
        _deliver_notification,
    ):
        from management.services.bot_followups import process_due_followups

        deal = self._paid_missing_delivery_deal()
        IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind="fulfillment",
            reason="paid_missing_delivery",
            level=2,
            meta_window_deadline=self.now + timedelta(hours=23),
        )

        self.assertEqual(
            process_due_followups(self.settings, now=self.now, limit=1),
            1,
        )

        notification = IgBotNotification.objects.get(
            dedupe_key=f"fulfillment_missing_delivery:{deal.pk}:g3"
        )
        self.assertEqual(notification.event_type, "fulfillment_missing_delivery")
        self.assertEqual(notification.client_id, self.client_record.pk)
        self.assertIn(
            f"/bot/?client={self.client_record.pk}",
            str(notification.payload.get("text") or ""),
        )

    def test_schedule_after_reply_selects_payment_policy_from_deal_truth(self):
        from management.services.bot_followups import schedule_after_bot_reply

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
        )

        with patch("management.services.bot_followups._now", return_value=self.now):
            task = schedule_after_bot_reply(self.client_record, deal=deal)

        self.assertIsNotNone(task)
        self.assertEqual(task.reason, "payment_link_unpaid")
        self.assertEqual(task.level, 0)
        self.assertEqual(task.due_at, self.now + timedelta(minutes=25))

    def test_schedule_after_reply_uses_missing_size_policy(self):
        from management.services.bot_followups import schedule_after_bot_reply

        self.client_record.stage = IgClient.Stage.PRODUCT_MATCHED
        self.client_record.current_size = ""
        self.client_record.save(
            update_fields=["stage", "current_size", "updated_at"]
        )

        with patch("management.services.bot_followups._now", return_value=self.now):
            task = schedule_after_bot_reply(self.client_record)

        self.assertIsNotNone(task)
        self.assertEqual(task.reason, "missing_customer_size")
        self.assertEqual(task.level, 0)
        self.assertEqual(task.due_at, self.now + timedelta(minutes=40))

    @patch(
        "management.services.instagram_bot.send_text",
        return_value=ProviderDeliveryReceipt(True, "", "", "policy-next"),
    )
    def test_unbound_legacy_ordinary_step_never_sends_or_starts_ladder(self, send_text):
        from management.services.bot_followups import process_due_followups

        first = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="first_reply_silence",
            level=0,
            meta_window_deadline=self.now + timedelta(hours=23),
        )

        self.assertEqual(
            process_due_followups(self.settings, now=self.now, limit=1),
            0,
        )

        first.refresh_from_db()
        self.assertEqual(first.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(first.skip_reason, "legacy_ordinary_followup_unbound")
        self.assertFalse(first.sent_message_id)
        self.assertFalse(IgFollowUpTask.objects.filter(
            client=self.client_record,
            status=IgFollowUpTask.Status.PENDING,
            reason="first_reply_silence",
        ).exists())
        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=1), 0)
        send_text.assert_not_called()

    @patch("management.services.instagram_bot.send_text")
    def test_changed_policy_condition_skips_send(self, send_text):
        from management.services.bot_followups import process_due_followups

        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="missing_customer_size",
            level=0,
            meta_window_deadline=self.now + timedelta(hours=23),
        )
        self.client_record.current_size = "M"
        self.client_record.save(update_fields=["current_size", "updated_at"])

        self.assertEqual(
            process_due_followups(self.settings, now=self.now, limit=1),
            0,
        )

        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(task.skip_reason, "policy_condition_changed")
        send_text.assert_not_called()

    def test_copy_key_selects_scenario_specific_size_copy(self):
        from management.services.bot_followups import compose_followup

        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="missing_customer_size",
            level=0,
        )

        text = compose_followup(task).lower()

        self.assertIn("зріст і вагу", text)
        self.assertIn("мірки", text)

    def test_policy_copy_follows_ru_and_en_client_language(self):
        from management.services.bot_followups import compose_followup

        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="missing_customer_size",
            level=0,
        )
        expected = {
            "ru": ("рост и вес", "замеры"),
            "en": ("height and weight", "measurements"),
        }
        for language, fragments in expected.items():
            with self.subTest(language=language):
                self.client_record.language = language
                self.client_record.save(update_fields=["language", "updated_at"])
                task.client = self.client_record
                text = compose_followup(task).lower()
                for fragment in fragments:
                    self.assertIn(fragment, text)

    def test_out_of_window_policy_step_keeps_reason_and_prepared_copy(self):
        from management.services.bot_followups import _schedule_next_policy_step

        deal = IgDeal.objects.create(client=self.client_record, status=IgDeal.Status.AWAITING_PAYMENT)
        sent = IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            status=IgFollowUpTask.Status.SENT,
            sent_at=self.now,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
            level=2,
        )

        self.assertTrue(
            _schedule_next_policy_step(sent, self.client_record, now=self.now)
        )

        manager_task = IgFollowUpTask.objects.get(
            client=self.client_record,
            status=IgFollowUpTask.Status.PENDING,
        )
        self.assertEqual(manager_task.kind, IgFollowUpTask.Kind.MANAGER_TASK)
        self.assertEqual(manager_task.reason, "payment_link_unpaid")
        self.assertEqual((manager_task.deal_id, manager_task.level), (deal.pk, 4))
        self.assertEqual(manager_task.skip_reason, "meta_window_closed")
        self.assertTrue(manager_task.message_text.strip())

    def test_delivery_ready_policy_continues_with_payment_step_a2(self):
        from management.services.bot_followups import _schedule_next_policy_step

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.QUOTED,
            np_full_name="Олена Тест",
            np_phone="+380500000000",
            np_city="Київ",
            np_office="1",
        )
        sent = IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            status=IgFollowUpTask.Status.SENT,
            sent_at=self.now,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="delivery_ready_unpaid",
            level=0,
        )

        self.assertTrue(
            _schedule_next_policy_step(sent, self.client_record, now=self.now)
        )

        nxt = IgFollowUpTask.objects.get(
            client=self.client_record,
            status=IgFollowUpTask.Status.PENDING,
        )
        self.assertEqual(nxt.reason, "payment_link_unpaid")
        self.assertEqual(nxt.level, 1)
        self.assertGreaterEqual(nxt.due_at - self.now, timedelta(hours=18))

    def test_invoice_expiry_materializes_one_event_task(self):
        from management.services.bot_followups import materialize_invoice_expired

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
            invoice_id="invoice-expired-1",
            invoice_url="https://pay.example/expired-1",
            invoice_expires_at=self.now - timedelta(minutes=1),
        )

        first = materialize_invoice_expired(deal, now=self.now)
        second = materialize_invoice_expired(deal, now=self.now)

        self.assertIsNotNone(first)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.event_key, f"invoice_expired:{deal.pk}:invoice-expired-1")
        self.assertEqual(
            IgFollowUpTask.objects.filter(event_key=first.event_key).count(), 1
        )
        self.assertEqual(first.trigger, IgFollowUpTask.Trigger.EVENT)
        self.assertEqual(first.event_occurred_at, deal.invoice_expires_at)
        self.assertEqual(first.event_payload["deal_id"], deal.pk)
        self.assertEqual(first.event_payload["invoice_id"], deal.invoice_id)
        self.assertEqual(first.event_payload["event"], "invoice_expired")
        self.assertEqual(
            first.policy_started_at,
            deal.invoice_expires_at - timedelta(hours=24),
        )

    def test_invoice_issue_registers_future_expiry_event_without_polling(self):
        from management.services.bot_followups import schedule_invoice_expiry_event

        expires_at = self.now + timedelta(hours=24)
        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
            invoice_id="invoice-future-event",
            invoice_url="https://pay.example/future-event",
            invoice_expires_at=expires_at,
        )

        task = schedule_invoice_expiry_event(deal, now=self.now)

        self.assertIsNotNone(task)
        self.assertEqual(task.trigger, IgFollowUpTask.Trigger.EVENT)
        self.assertEqual(task.due_at, expires_at)
        self.assertEqual(task.event_occurred_at, expires_at)
        self.assertEqual(task.policy_started_at, self.now)

    @patch(
        "management.services.instagram_bot.send_text",
        return_value=__import__(
            "management.services.instagram_bot", fromlist=["ProviderDeliveryReceipt"]
        ).ProviderDeliveryReceipt(True, "", "", "meta-followup-1"),
    )
    def test_expired_event_is_sent_once_with_provider_receipt(self, _send_text):
        from management.services.bot_followups import (
            process_due_followups,
            schedule_invoice_expiry_event,
        )

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
            invoice_id="invoice-expired-2",
            invoice_url="https://pay.example/expired-2",
            invoice_expires_at=self.now - timedelta(minutes=1),
        )
        task = schedule_invoice_expiry_event(deal, now=self.now)

        self.assertIsNotNone(task)
        self.assertEqual(
            process_due_followups(self.settings, now=self.now, limit=1), 1
        )
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SENT)
        self.assertEqual(task.provider_message_id, "meta-followup-1")
        self.assertEqual(task.sent_message.provider_message_id, "meta-followup-1")
        self.assertEqual(task.claim_token, "")
        self.assertIsNone(task.claim_until)

    @patch(
        "management.services.instagram_bot.send_text",
        return_value=__import__(
            "management.services.instagram_bot", fromlist=["ProviderDeliveryReceipt"]
        ).ProviderDeliveryReceipt(True, "", "", "meta-discount-1"),
    )
    def test_delivered_discount_followup_records_funnel_fact_atomically(self, _send_text):
        from management.models import IgFunnelStepEvent
        from management.services.bot_followups import process_due_followups

        self.client_record.stage = IgClient.Stage.CHECKOUT
        self.client_record.primary_objection = IgClient.Objection.PRICE
        self.client_record.save(update_fields=["stage", "primary_objection", "updated_at"])
        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind=IgFollowUpTask.Kind.RESCUE,
            reason="price_objection",
            level=0,
            discount_percent=5,
            manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.APPROVED,
            meta_window_deadline=self.now + timedelta(hours=23),
        )

        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=1), 1)

        event = IgFunnelStepEvent.objects.get(
            episode__client=self.client_record,
            event_type=IgFunnelStepEvent.Type.DISCOUNT_OFFERED,
        )
        self.assertEqual(event.evidence["followup_task_id"], task.pk)
        self.assertEqual(event.evidence["provider_message_id"], "meta-discount-1")
        self.assertTrue(event.evidence["delivery_confirmed"])

    @patch("management.services.instagram_bot.send_text")
    def test_due_discount_waits_for_one_manager_decision_alert(self, send_text):
        from management.services.bot_followups import process_due_followups

        self._source_budget_objection()
        self.client_record.stage = IgClient.Stage.CHECKOUT
        self.client_record.primary_objection = IgClient.Objection.PRICE
        self.client_record.save(update_fields=["stage", "primary_objection", "updated_at"])
        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind=IgFollowUpTask.Kind.RESCUE,
            reason="price_objection",
            discount_percent=5,
            meta_window_deadline=self.now + timedelta(hours=23),
        )

        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=1), 0)
        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=1), 0)

        send_text.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(
            task.manager_approval_status,
            IgFollowUpTask.ManagerApprovalStatus.PENDING,
        )
        self.assertIsNotNone(task.manager_approval_requested_at)
        alert = IgBotNotification.objects.get(dedupe_key=f"discount_approval:{task.pk}")
        self.assertEqual(alert.event_type, "discount_approval")
        self.assertTrue(alert.payload["requires_human_review"])
        buttons = alert.payload["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(buttons[0]["callback_data"], f"igdisc:approve:{task.pk}")
        self.assertEqual(buttons[1]["callback_data"], f"igdisc:reject:{task.pk}")
        self.assertEqual(
            IgBotNotification.objects.filter(dedupe_key=f"discount_approval:{task.pk}").count(),
            1,
        )

    def test_cancelled_discount_resolves_queued_approval_before_delivery(self):
        from management.models import IgBotNotificationAudit
        from management.services.bot_followups import cancel_pending, process_due_followups

        self._source_budget_objection()
        self.client_record.stage = IgClient.Stage.CHECKOUT
        self.client_record.primary_objection = IgClient.Objection.PRICE
        self.client_record.save(update_fields=[
            "stage", "primary_objection", "updated_at"
        ])
        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind=IgFollowUpTask.Kind.RESCUE,
            reason="price_objection",
            discount_percent=5,
            meta_window_deadline=self.now + timedelta(hours=23),
        )
        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=1), 0)
        alert = IgBotNotification.objects.get(dedupe_key=f"discount_approval:{task.pk}")
        self.assertEqual(alert.status, IgBotNotification.Status.PENDING)

        self.assertEqual(cancel_pending(self.client_record, reason="client_reply"), 1)

        alert.refresh_from_db()
        self.assertEqual(alert.status, IgBotNotification.Status.RESOLVED)
        self.assertEqual(alert.payload["review_status"], "cancelled")
        self.assertTrue(
            IgBotNotificationAudit.objects.filter(
                notification=alert,
                action="discount_auto_cancelled",
            ).exists()
        )

    def test_manager_only_discount_task_does_not_request_automatic_approval(self):
        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind=IgFollowUpTask.Kind.MANAGER_TASK,
            reason="meta_window_closed",
            discount_percent=5,
            manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
        )

        from management.services.bot_followups import process_due_followups

        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=1), 0)
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"discount_approval:{task.pk}"
            ).exists()
        )

    def test_expired_discount_is_cancelled_before_approval_alert(self):
        self._source_budget_objection()
        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            status=IgFollowUpTask.Status.PENDING,
            kind=IgFollowUpTask.Kind.RESCUE,
            reason="price_objection",
            discount_percent=5,
            manager_approval_status=IgFollowUpTask.ManagerApprovalStatus.PENDING,
            meta_window_deadline=self.now - timedelta(seconds=1),
        )

        from management.services.bot_followups import process_due_followups

        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=1), 0)
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.CANCELLED)
        self.assertEqual(task.skip_reason, "meta_window_closed")
        self.assertFalse(
            IgBotNotification.objects.filter(
                dedupe_key=f"discount_approval:{task.pk}"
            ).exists()
        )

    def test_legacy_restock_event_without_catalog_source_creates_no_task(self):
        from management.services.bot_followups import materialize_restock

        first = materialize_restock(
            self.client_record,
            product_id=41,
            size="m",
            event_id="restock:event:41:m:1",
            now=self.now,
        )
        second = materialize_restock(
            self.client_record,
            product_id=41,
            size="m",
            event_id="restock:event:41:m:1",
            now=self.now,
        )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertFalse(IgFollowUpTask.objects.filter(
            client=self.client_record, reason__in=("restock_wait", "restock_permission_review"),
        ).exists())

    def test_invoice_expiry_continuation_preserves_absolute_t72_offset(self):
        from management.services.bot_followups import _schedule_next_policy_step

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
        )
        event = IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            sent_at=self.now,
            status=IgFollowUpTask.Status.SENT,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
            level=3,
            event_key=f"invoice_expired:{deal.pk}:absolute-offset",
            trigger=IgFollowUpTask.Trigger.EVENT,
            event_occurred_at=self.now - timedelta(hours=24),
            event_payload={
                "event": "invoice_expired",
                "deal_id": deal.pk,
                "invoice_id": "absolute-offset",
            },
            policy_started_at=self.now - timedelta(hours=24),
        )

        self.assertTrue(
            _schedule_next_policy_step(event, self.client_record, now=self.now)
        )
        final = IgFollowUpTask.objects.get(
            client=self.client_record,
            status=IgFollowUpTask.Status.PENDING,
            level=4,
        )
        self.assertEqual(final.due_at, self.now + timedelta(hours=48))
        self.assertEqual(final.policy_started_at, self.now - timedelta(hours=24))

    def test_restock_confirmation_is_terminal_and_does_not_schedule_no_restock_copy(self):
        from management.services.bot_followups import (
            _complete_policy_after_send,
            _schedule_next_policy_step,
        )

        event = IgFollowUpTask.objects.create(
            client=self.client_record,
            due_at=self.now,
            sent_at=self.now,
            status=IgFollowUpTask.Status.SENT,
            kind=IgFollowUpTask.Kind.QUALIFICATION,
            reason="restock_wait",
            level=1,
            event_key="restock:terminal-event",
        )

        self.assertTrue(_complete_policy_after_send(event, self.client_record))
        self.assertFalse(
            _schedule_next_policy_step(event, self.client_record, now=self.now)
        )

    def test_expiry_event_is_not_materialized_by_due_polling(self):
        from management.services.bot_followups import process_due_followups

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
            invoice_id="polling-must-not-be-source",
            invoice_url="https://pay.example/polling-must-not-be-source",
            invoice_expires_at=self.now - timedelta(minutes=1),
        )

        self.assertEqual(process_due_followups(self.settings, now=self.now, limit=5), 0)
        self.assertFalse(
            IgFollowUpTask.objects.filter(
                deal=deal, trigger=IgFollowUpTask.Trigger.EVENT
            ).exists()
        )

    def test_legacy_event_without_boundary_fails_closed(self):
        from management.services.bot_followups import event_followup_fact_guard

        deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
        )
        task = IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=deal,
            due_at=self.now,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
            level=3,
            event_key="legacy:event-without-boundary",
        )

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "event_boundary_missing")

    def test_paid_invoice_event_is_rejected_before_send(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._event_task()
        deal = task.deal
        deal.status = IgDeal.Status.PAID
        deal.payment_status = "paid"
        deal.invoice_url = "https://pay.example/event-boundary-invoice"
        deal.invoice_expires_at = self.now - timedelta(minutes=1)
        deal.save(
            update_fields=[
                "status",
                "payment_status",
                "invoice_url",
                "invoice_expires_at",
                "updated_at",
            ]
        )

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "invoice_paid")

    def test_replaced_invoice_event_is_rejected_before_send(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._event_task()
        deal = task.deal
        deal.invoice_url = "https://pay.example/replacement-invoice"
        deal.invoice_id = "replacement-invoice"
        deal.invoice_expires_at = self.now - timedelta(minutes=1)
        deal.save(
            update_fields=["invoice_id", "invoice_url", "invoice_expires_at", "updated_at"]
        )

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "invoice_superseded")

    def test_cancelled_invoice_event_is_rejected_before_send(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._event_task()
        task.deal.status = IgDeal.Status.CANCELLED
        task.deal.save(update_fields=["status", "updated_at"])

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "invoice_cancelled")

    def test_invoice_event_boundary_must_match_current_expiry(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._event_task()
        task.deal.invoice_expires_at = self.now + timedelta(minutes=5)
        task.deal.save(update_fields=["invoice_expires_at", "updated_at"])

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "invoice_boundary_changed")

    def test_stale_proposal_revision_is_rejected_before_send(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._proposal_event_task(revision=2, payload_revision=1)

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "proposal_revision_changed")

    def test_terminal_proposal_event_is_rejected_before_send(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._proposal_event_task(status=IgCheckoutProposal.Status.REVOKED)

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "proposal_terminal")

    def test_proposal_event_boundary_must_match_current_expiry(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._proposal_event_task(
            event_occurred_at=self.now - timedelta(minutes=2)
        )

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "proposal_boundary_changed")

    def test_proposal_event_rejects_terminal_deal_truth(self):
        from management.services.bot_followups import event_followup_fact_guard

        task = self._proposal_event_task()
        task.deal.status = IgDeal.Status.CANCELLED
        task.deal.save(update_fields=["status", "updated_at"])

        allowed, reason = event_followup_fact_guard(task, now=self.now)

        self.assertFalse(allowed)
        self.assertEqual(reason, "proposal_deal_terminal")

    @patch(
        "management.services.instagram_bot.send_text",
        return_value=ProviderDeliveryReceipt(True, "", "", "must-not-send"),
    )
    def test_event_fact_is_rechecked_after_processing_claim(self, send_text):
        from management.services import bot_followups

        task = self._event_task()
        real_recheck = bot_followups._recheck_followup_send_claim

        def pay_after_processing(*args, **kwargs):
            claimed = real_recheck(*args, **kwargs)
            IgDeal.objects.filter(pk=task.deal_id).update(status=IgDeal.Status.PAID)
            return claimed

        with patch(
            "management.services.bot_followups._recheck_followup_send_claim",
            side_effect=pay_after_processing,
        ):
            processed = bot_followups.process_due_followups(
                self.settings,
                now=self.now,
                limit=1,
            )

        self.assertEqual(processed, 0)
        send_text.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(task.skip_reason, "invoice_paid")

    def test_event_continuation_uses_policy_anchor_after_worker_delay(self):
        from management.services.bot_followups import _schedule_next_policy_step

        issue_at = self.now - timedelta(hours=24)
        event = self._event_task(issue_at=issue_at)
        worker_now = self.now + timedelta(hours=2)

        self.assertTrue(_schedule_next_policy_step(event, self.client_record, now=worker_now))
        nxt = IgFollowUpTask.objects.get(
            client=self.client_record,
            status=IgFollowUpTask.Status.PENDING,
            reason="payment_link_unpaid",
            level=4,
        )
        self.assertEqual(nxt.due_at, issue_at + timedelta(hours=72))
        self.assertEqual(nxt.policy_started_at, issue_at)

    def test_staff_can_continue_a_completed_event_without_mutating_its_fact(self):
        from management.services.bot_followups import continue_event_followup

        event = self._event_task()
        event.status = IgFollowUpTask.Status.COMPLETED
        event.save(update_fields=["status", "updated_at"])
        original_boundary = {
            "event_key": event.event_key,
            "event_payload": dict(event.event_payload),
            "event_occurred_at": event.event_occurred_at,
            "policy_started_at": event.policy_started_at,
            "policy_version": event.policy_version,
        }

        result = continue_event_followup(
            event.pk,
            actor_id=17,
            note="Менеджер підтвердив продовження",
            now=self.now,
        )

        self.assertTrue(result["ok"], result)
        event.refresh_from_db()
        self.assertEqual(
            {
                "event_key": event.event_key,
                "event_payload": event.event_payload,
                "event_occurred_at": event.event_occurred_at,
                "policy_started_at": event.policy_started_at,
                "policy_version": event.policy_version,
            },
            original_boundary,
        )
        self.assertTrue(
            IgFollowUpTask.objects.filter(
                client=self.client_record,
                reason="payment_link_unpaid",
                level=4,
                status=IgFollowUpTask.Status.PENDING,
            ).exists()
        )


class FollowupEventContinuationBoundaryTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 5, 12, 0, tzinfo=KYIV)
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.save(update_fields=["is_enabled", "updated_at"])
        self.client_record = IgClient.get_or_create_for_sender("event-boundary-client")
        self.deal = IgDeal.objects.create(
            client=self.client_record,
            status=IgDeal.Status.AWAITING_PAYMENT,
            invoice_id="event-boundary-invoice",
        )

    def _event_task(self, *, issue_at=None):
        issue_at = issue_at or self.now - timedelta(hours=24)
        return IgFollowUpTask.objects.create(
            client=self.client_record,
            deal=self.deal,
            due_at=self.now,
            kind=IgFollowUpTask.Kind.PAYMENT,
            reason="payment_link_unpaid",
            level=3,
            event_key="invoice_expired:event-boundary-invoice",
            trigger="event",
            event_occurred_at=issue_at + timedelta(hours=24),
            event_payload={
                "event": "invoice_expired",
                "deal_id": self.deal.pk,
                "invoice_id": "event-boundary-invoice",
            },
            policy_started_at=issue_at,
            policy_version="followup-v1",
        )

    def test_event_boundary_is_immutable_after_materialization(self):
        task = self._event_task()

        self.assertEqual(task.trigger, "event")
        self.assertEqual(task.event_occurred_at, self.now)
        self.assertEqual(task.event_payload["invoice_id"], "event-boundary-invoice")
        self.assertEqual(task.policy_started_at, self.now - timedelta(hours=24))
        self.assertEqual(task.policy_version, "followup-v1")

        task.event_payload = {"invoice_id": "replacement"}
        with self.assertRaises(ValidationError):
            task.save(update_fields=["event_payload"])

        with self.assertRaises(ValueError):
            IgFollowUpTask.objects.filter(pk=task.pk).update(trigger="time")

        task.refresh_from_db()
        task.policy_started_at = self.now
        with self.assertRaises(ValueError):
            IgFollowUpTask.objects.bulk_update([task], ["policy_started_at"])


class LegacyPaymentProviderBoundaryTests(TestCase):
    """Exercise the worker's actual per-request boundary after PROCESSING."""
    def setUp(self):
        from management.models import IgDealItem, InstagramBotMessage
        self.now = datetime(2026, 8, 3, 11, 0, tzinfo=KYIV)
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.save(update_fields=["is_enabled"])
        self.customer = IgClient.get_or_create_for_sender("legacy-payment-physical-fence")
        self.customer.stage = IgClient.Stage.PAYMENT_PENDING
        self.customer.last_message_at = self.now
        self.customer.save(update_fields=["stage", "last_message_at"])
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="done", provider_namespace="instagram_login:timer-fixture",
            mid="unpaid-timer-source", text="Хочу оформити цю футболку.", provider_created_at=self.now)
        self.deal = IgDeal.objects.create(client=self.customer, status=IgDeal.Status.AWAITING_PAYMENT,
            amount="1000", requested_payment_amount="1000", invoice_id="timer-invoice-1",
            invoice_url="https://pay.example/timer-invoice-1", invoice_expires_at=self.now+timedelta(hours=24))
        self.item = IgDealItem.objects.create(deal=self.deal, title="Футболка", qty=1, unit_price="1000")
        self.task = IgFollowUpTask.objects.create(client=self.customer, deal=self.deal, due_at=self.now,
            kind=IgFollowUpTask.Kind.PAYMENT, reason="payment_link_unpaid", level=0,
            policy_started_at=self.now-timedelta(minutes=25), meta_window_deadline=self.now+timedelta(hours=23))
        self.physical_dispatches = []
        self.boundary_reason = ""

    def run_at_provider_boundary(self, change=lambda self: None):
        from management.services.bot_followups import process_due_followups
        def send_text(*args, **kwargs):
            self.task.refresh_from_db()
            self.assertEqual(self.task.status, IgFollowUpTask.Status.PROCESSING)
            change(self)
            # Same callback the real transport enters immediately before HTTP.
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=1,
                    delivered_chunk_count=0, provider_message_ids=[]) as permission:
                self.boundary_reason = permission.reason
                if not permission:
                    return ProviderDeliveryReceipt(False, "cancelled", permission.reason, "")
                self.physical_dispatches.append(args[2])
                return ProviderDeliveryReceipt(True, "", "", "timer-physical-receipt")
        with patch("management.services.instagram_bot.send_text", side_effect=send_text):
            result = process_due_followups(self.settings, now=self.now, limit=1)
        self.task.refresh_from_db()
        return result

    def assert_blocked(self, reason):
        self.assertEqual(self.physical_dispatches, [])
        self.assertEqual(self.boundary_reason, reason)
        self.assertEqual(self.task.status, IgFollowUpTask.Status.SKIPPED)
        self.assertEqual(self.task.skip_reason, reason)

    def test_unchanged_same_claim_can_send_once(self):
        self.assertEqual(self.run_at_provider_boundary(), 1)
        self.assertEqual(len(self.physical_dispatches), 1)
        self.assertEqual(self.task.provider_message_id, "timer-physical-receipt")

    def test_confirmed_200_prepayment_after_processing_blocks_stale_unpaid_stage(self):
        def paid(case):
            IgPaymentProjection.objects.create(client=case.customer, deal=case.deal,
                truth=IgDeal.PaymentTruth.CONFIRMED, gross_amount="200", paid_at=case.now)
        self.assertEqual(self.run_at_provider_boundary(paid), 0)
        self.assert_blocked("already_converted")

    def test_confirmed_full_payment_after_processing_blocks_stale_unpaid_stage(self):
        def paid(case):
            IgPaymentProjection.objects.create(client=case.customer, deal=case.deal,
                truth=IgDeal.PaymentTruth.CONFIRMED, gross_amount="1000", paid_at=case.now)
        self.assertEqual(self.run_at_provider_boundary(paid), 0)
        self.assert_blocked("already_converted")

    def test_new_customer_message_after_processing_blocks_send(self):
        from management.models import InstagramBotMessage
        def inbound(case):
            InstagramBotMessage.objects.create(client=case.customer, sender_id=case.customer.igsid,
                role="user", source="webhook", status="done", provider_namespace="instagram_login:timer-fixture",
                mid="newer-source", text="Я передумав.", provider_created_at=case.now)
        self.assertEqual(self.run_at_provider_boundary(inbound), 0)
        self.assert_blocked("payment_followup_context_changed")

    def test_same_source_id_changed_text_after_processing_blocks_send(self):
        def edited(case):
            case.source.text = "Не хочу оформлювати цю футболку."
            case.source.save(update_fields=["text"])
        self.assertEqual(self.run_at_provider_boundary(edited), 0)
        self.assert_blocked("payment_followup_context_changed")

    def test_cart_item_changed_after_processing_blocks_send(self):
        def cart(case):
            case.item.qty = 2
            case.item.save(update_fields=["qty", "line_total"])
        self.assertEqual(self.run_at_provider_boundary(cart), 0)
        self.assert_blocked("payment_followup_context_changed")

    def test_invoice_superseded_after_processing_blocks_send(self):
        def invoice(case):
            IgDeal.objects.filter(pk=case.deal.pk).update(invoice_id="timer-invoice-2",
                invoice_url="https://pay.example/timer-invoice-2", invoice_expires_at=case.now+timedelta(hours=24))
        self.assertEqual(self.run_at_provider_boundary(invoice), 0)
        self.assert_blocked("payment_followup_context_changed")

    def test_policy_purpose_changed_after_processing_cannot_bypass_legacy_fence(self):
        def purpose(case):
            IgFollowUpTask.objects.filter(pk=case.task.pk).update(reason="different_policy")
        self.assertEqual(self.run_at_provider_boundary(purpose), 0)
        self.assert_blocked("payment_followup_purpose_changed")

    def test_expiry_crossed_before_provider_cannot_send_cached_active_link_copy(self):
        from management.services import bot_followups as policy
        self.task.level = 2
        self.task.save(update_fields=["level"])
        self.deal.invoice_expires_at = self.now+timedelta(seconds=1)
        self.deal.save(update_fields=["invoice_expires_at"])
        original = policy._followup_promotion_provider_boundary
        def later_boundary(*args, **kwargs):
            kwargs["checked_at"] = self.now+timedelta(seconds=2)
            return original(*args, **kwargs)
        with patch.object(policy, "_followup_promotion_provider_boundary", side_effect=later_boundary):
            self.assertEqual(self.run_at_provider_boundary(), 0)
        self.assert_blocked("payment_followup_copy_changed")

    def test_model_output_does_not_change_the_customer_source_fence(self):
        from management.models import InstagramBotMessage
        def model(case):
            InstagramBotMessage.objects.create(client=case.customer, sender_id="brand", role="model",
                source="bot", status="done", text="Дякую.", mid="independent-model-output")
        self.assertEqual(self.run_at_provider_boundary(model), 1)
        self.assertEqual(len(self.physical_dispatches), 1)

    def test_missing_or_foreign_in_process_claim_fails_closed(self):
        from management.services import bot_followups as policy
        for captured in (None, policy._LegacyPaymentSendClaim(self.task.pk, self.customer.pk, "other-owner", "unused")):
            self.task.status, self.task.claim_token = IgFollowUpTask.Status.PROCESSING, "owner"
            self.task.claim_until = self.now+timedelta(minutes=4)
            self.task.save(update_fields=["status", "claim_token", "claim_until"])
            with policy._followup_promotion_provider_boundary(self.task.pk, "owner", checked_at=self.now,
                    payment_claim=captured) as permission:
                self.assertFalse(permission)
                self.assertEqual(permission.reason, "payment_followup_claim_unbound")

    def test_live_far_future_third_reminder_has_no_artificial_deadline(self):
        from management.services.bot_followups import _policy_followup_copy
        self.task.level = 2
        for language, active, urgency in (("uk", "ще активне", ("скоро", "сьогодні")),
                ("ru", "ещё активна", ("скоро", "сегодня")), ("en", "still active", ("soon", "today"))):
            text = _policy_followup_copy(self.task, language, now=self.now)
            self.assertIn(active, text)
            self.assertTrue(all(word not in text.casefold() for word in urgency))
