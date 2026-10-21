"""Delivered missing-time question -> owned time answer -> existing PR1 consent."""
from datetime import datetime, timedelta, timezone as datetime_timezone
import json
from unittest.mock import patch

from django.db import connection, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management.ig_payment_reminder_dialog_models import IgPaymentReminderTimeRequest as TimeRequest
from management.models import (
    IgCheckoutInvoiceGeneration, IgClient, IgFollowUpTask, IgFunnelResetAudit,
    IgPaymentReminderAnswer, IgPaymentReminderInvitation, IgPaymentReminderReceipt,
    IgRevisionDeliveryEffect, InstagramBotMessage,
)
from management.services import ig_payment_reminders as reminders
from management.services.ig_checkout_generation import terminalize_generation_attempt
from management.services.ig_payment_reminder_time import parse_reminder_time_reply
from management.tests_ig_hosted_settlement import HostedSettlementFixture
from management import tests_ig_payment_reminders as fixtures
from orders.models import Order, PaymentAttempt


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test",
    IG_PAYMENT_REMINDERS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100)
class NativePaymentReminderTimeDialogTests(TransactionTestCase):
    """Reuse real invoice producers without inheriting their test methods."""
    reset_sequences = True
    quoted = fixtures.RequestedPaymentReminderTests.quoted
    source = fixtures.RequestedPaymentReminderTests.source
    revision = fixtures.RequestedPaymentReminderTests.revision
    pay = fixtures.RequestedPaymentReminderTests.pay

    def setUp(self):
        fixtures.RequestedPaymentReminderTests.setUp(self)
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.native_send_serial = 0

    def execute_source(self, source, *, revision=None, token=None, missing_mid=False, unknown=False, before_http=None):
        from management.services.ig_revision_live import execute_claimed_revision
        if revision is None:
            revision, token = self.revision(source, now=source.provider_created_at)
        physical = []

        def lookup(_settings, **kwargs):
            if before_http:
                before_http()
            return "synthetic-payment-time-dialog-token"

        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.native_send_serial += 1
            physical.append(json.loads(data))
            if unknown:
                raise TimeoutError("Native clarification request reached provider; outcome unknown")
            return 200, json.dumps({} if missing_mid else {"message_id": f"native-payment-time-{self.native_send_serial}"})

        with patch("django.utils.timezone.now", return_value=source.provider_created_at), \
                patch("management.services.instagram_bot.get_page_token", side_effect=lookup), \
                patch("management.services.instagram_bot._provider_http", side_effect=provider), \
                patch("management.services.call_ai_analysis.gemini_generate_text",
                    side_effect=AssertionError("Owned reminder dialog must use zero Gemini")) as gemini:
            result = execute_claimed_revision(revision.pk, token, self.settings)
        gemini.assert_not_called()
        return {"result": result, "physical": physical, "source": source, "revision": revision, "token": token}

    def clarification(self, *, locale="uk", delivered=True, missing_mid=False, unknown=False):
        source = self.source({"uk": "Нагадай про оплату", "ru": "Напомни об оплате", "en": "Remind me to pay"}[locale])
        if delivered:
            case = self.execute_source(source, missing_mid=missing_mid, unknown=unknown)
        else:
            revision, token = self.revision(source)
            result = reminders.prepare_request(revision, source, settings_obj=self.settings, now=self.now)
            self.assertTrue(result.handled, result)
            case = {"source": source, "revision": revision, "token": token, "prepared": result, "physical": []}
        case["revision"].refresh_from_db()
        diagnostic = {"locale": locale, "source_id": source.pk, "revision_id": case["revision"].pk,
            "revision_state": case["revision"].state,
            "effects": list(IgRevisionDeliveryEffect.objects.filter(revision=case["revision"]).values(
                "id", "state", "source_message_id", "part_count", "part_index", "kind", "purpose")),
            "physical_count": len(case["physical"])}
        if delivered:
            diagnostic.update(result_state=case["result"].state, result_reasons=case["result"].reasons)
        else:
            diagnostic.update(prepared_handled=case["prepared"].handled, prepared_reason=case["prepared"].reason)
        self.assertTrue(TimeRequest.objects.filter(request_source=source).exists(), diagnostic)
        pending = TimeRequest.objects.get(request_source=source)
        self.assertEqual(pending.locale, locale)
        self.assertFalse(IgPaymentReminderInvitation.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).exists())
        if delivered and not missing_mid and not unknown:
            self.assertEqual(case["result"].state, "completed", case["result"].reasons)
            self.assertEqual(len(case["physical"]), 1)
            self.assertEqual(case["physical"][0]["message"], {"text": pending.body})
            effect = IgRevisionDeliveryEffect.objects.get(revision=case["revision"], source_message=source)
            self.assertTrue(effect.provider_message_id)
            case["effect"] = effect
        return {**case, "pending": pending}

    def proposed_time(self, text="через 1 годину", *, at=None, namespace="instagram_login:owner-1", client=None, extra_text=None):
        at = at or self.now + timedelta(seconds=1)
        source = self.source(text, at=at, namespace=namespace, client=client)
        extra = (self.source(extra_text, at=at),) if extra_text else ()
        revision, token = self.revision(source, now=at, extra_sources=extra)
        with patch("django.utils.timezone.now", return_value=at), \
                patch("management.services.instagram_bot._provider_http") as provider, \
                patch("management.services.call_ai_analysis.gemini_generate_text") as gemini:
            result = reminders.prepare_request(revision, source, settings_obj=self.settings, now=at)
        provider.assert_not_called()
        gemini.assert_not_called()
        return result, source, revision, token

    def assert_no_agreement(self):
        self.assertFalse(IgPaymentReminderInvitation.objects.exists())
        self.assertFalse(IgPaymentReminderAnswer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).exists())

    def native_dialog(self, locale):
        from management.services.ig_assisted_commerce import _configuration_capture
        from management.services.ig_commerce_projection import capture_current_selection_lines
        cart_before = capture_current_selection_lines(self.client_row.pk, now=self.now)
        before_invoices = IgCheckoutInvoiceGeneration.objects.count()
        before_attempts = PaymentAttempt.objects.count()
        prompt = self.clarification(locale=locale)
        at = self.now + timedelta(seconds=1)
        time_source = self.source({"uk": "через 1 годину", "ru": "через 1 час", "en": "in 1 hour"}[locale], at=at)
        time_source.reply_to_provider_message_id = prompt["effect"].provider_message_id
        time_source.save(update_fields=["reply_to_provider_message_id"])
        proposed = self.execute_source(time_source)
        self.assertEqual(proposed["result"].state, "completed", proposed["result"].reasons)
        self.assertEqual(len(proposed["physical"]), 1)
        inv = IgPaymentReminderInvitation.objects.get(request_source=time_source)
        receipt = IgPaymentReminderReceipt.objects.get(invitation=inv)
        self.assertEqual((inv.locale, inv.due_at), (locale, at + timedelta(hours=1)))
        self.assertEqual(proposed["physical"][0]["message"]["text"], inv.body)
        self.assertEqual(proposed["physical"][0]["message"]["quick_replies"], [{"content_type": "text",
            "title": inv.button_title, "payload": inv.accept_payload}])
        self.assertTrue(inv.accept_payload.startswith("PR1:"))
        self.assertFalse(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).exists())
        answer = self.source(inv.button_title, payload=inv.accept_payload, at=at + timedelta(seconds=1))
        answer.reply_to_provider_message_id = receipt.provider_message_id
        answer.save(update_fields=["reply_to_provider_message_id"])
        accepted = self.execute_source(answer)
        self.assertEqual(accepted["result"].state, "completed", accepted["result"].reasons)
        self.assertEqual(len(accepted["physical"]), 1)
        agreement = IgPaymentReminderAnswer.objects.get(source=answer, kind="accept")
        self.assertEqual(agreement.task.due_at, inv.due_at)
        self.assertEqual(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).count(), 1)
        self.assertEqual(accepted["physical"][0]["message"]["quick_replies"], [reminders._revoke_button(inv)])
        ack = IgRevisionDeliveryEffect.objects.get(revision=accepted["revision"], source_message=answer)
        revoke = self.source("Cancel", payload=inv.revoke_payload, at=at + timedelta(seconds=2))
        revoke.reply_to_provider_message_id = ack.provider_message_id
        revoke.save(update_fields=["reply_to_provider_message_id"])
        cancelled = self.execute_source(revoke)
        self.assertEqual(cancelled["result"].state, "completed", cancelled["result"].reasons)
        agreement.task.refresh_from_db()
        self.assertEqual(agreement.task.status, "cancelled")
        self.assertEqual(IgPaymentReminderAnswer.objects.filter(invitation=inv, kind="revoke").count(), 1)
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), before_invoices)
        self.assertEqual(PaymentAttempt.objects.count(), before_attempts)
        self.assertFalse(Order.objects.exists())
        cart_after = capture_current_selection_lines(self.client_row.pk, now=revoke.provider_created_at)
        self.assertEqual(_configuration_capture(cart_before), _configuration_capture(cart_after))

    def test_uk_native_missing_time_bare_time_pr1_click_ack_and_revoke_without_gemini(self):
        self.native_dialog("uk")

    def test_ru_native_missing_time_bare_time_pr1_click_ack_and_revoke_without_gemini(self):
        self.native_dialog("ru")

    def test_en_native_missing_time_bare_time_pr1_click_ack_and_revoke_without_gemini(self):
        self.native_dialog("en")

    def test_time_only_without_owned_delivered_question_remains_normal_intent(self):
        result, _source, _revision, _token = self.proposed_time("15:30")
        self.assertFalse(result.handled, result)
        self.assert_no_agreement()
        self.assertFalse(TimeRequest.objects.exists())

    def test_prepared_but_unsent_question_cannot_own_bare_time(self):
        self.clarification(delivered=False)
        result, _source, _revision, _token = self.proposed_time()
        self.assertFalse(result.invitation_id, result)
        self.assert_no_agreement()

    def test_unknown_native_question_cannot_own_time_or_blindly_replay(self):
        case = self.clarification(unknown=True)
        self.assertEqual(len(case["physical"]), 1)
        self.assertFalse(self.proposed_time()[0].invitation_id)
        again = self.execute_source(case["source"], revision=case["revision"], token=case["token"], unknown=True)
        self.assertEqual(again["physical"], [])
        self.assert_no_agreement()

    def test_native_question_without_mid_cannot_authorize_bare_time(self):
        case = self.clarification(missing_mid=True)
        self.assertEqual(len(case["physical"]), 1)
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_time_continuation_card_without_native_mid_cannot_authorize_pr1_click(self):
        self.clarification()
        source = self.source("через 1 годину", at=self.now + timedelta(seconds=1))
        proposed = self.execute_source(source, missing_mid=True)
        self.assertEqual(len(proposed["physical"]), 1)
        invitation = IgPaymentReminderInvitation.objects.get(request_source=source)
        self.assertFalse(IgPaymentReminderReceipt.objects.filter(invitation=invitation).exists())
        answer = self.source(invitation.button_title, payload=invitation.accept_payload,
            at=self.now + timedelta(seconds=2))
        revision, _token = self.revision(answer, now=answer.provider_created_at)
        with patch("django.utils.timezone.now", return_value=answer.provider_created_at):
            reminders.handle_reply(answer, revision=revision, settings_obj=self.settings, now=answer.provider_created_at)
        self.assertFalse(IgPaymentReminderAnswer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).exists())

    def test_changed_original_request_source_invalidates_dialog(self):
        case = self.clarification()
        case["source"].text = "Який склад тканини?"
        case["source"].save(update_fields=["text"])
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_original_source_change_at_prompt_socket_boundary_prevents_native_question(self):
        source = self.source("Нагадай про оплату")
        def change_source():
            source.text = "Який склад тканини?"
            source.save(update_fields=["text"])
        case = self.execute_source(source, before_http=change_source)
        self.assertEqual(case["physical"], [])
        self.assertTrue(TimeRequest.objects.filter(request_source=source).exists())
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_foreign_provider_namespace_cannot_answer_owned_time_question(self):
        self.clarification()
        self.assertFalse(self.proposed_time(namespace="instagram_login:other-owner")[0].invitation_id)
        self.assert_no_agreement()

    def test_foreign_customer_cannot_answer_another_customers_time_question(self):
        self.clarification()
        other = IgClient.objects.create(igsid="foreign-reminder-time-customer", language="uk")
        self.assertFalse(self.proposed_time(client=other)[0].invitation_id)
        self.assert_no_agreement()

    def test_reset_fences_delivered_time_question(self):
        case = self.clarification()
        IgFunnelResetAudit.objects.create(client=self.client_row, reset_after_message_id=case["source"].pk,
            reason="payment reminder time reset")
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_payment_after_question_prevents_time_continuation(self):
        self.clarification()
        self.pay()
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_changed_native_cart_quantity_cannot_reuse_original_invoice_question(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        self.clarification()
        before = capture_current_selection_lines(self.client_row.pk, now=self.now)
        source = self.source("измените позицию 1, количество 2", at=self.now + timedelta(seconds=1))
        self.client_row.refresh_from_db()
        with transaction.atomic():
            admitted = reduce_inbound_commerce_source(self.client_row, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(admitted.ready, admitted.reason)
        source.status = "done"
        source.save(update_fields=["status"])
        after = capture_current_selection_lines(self.client_row.pk, now=source.provider_created_at)
        self.assertEqual(before["lines"][0]["fields"]["quantity"]["value"], 1)
        self.assertEqual(after["lines"][0]["fields"]["quantity"]["value"], 2, after)
        self.assertFalse(self.proposed_time(at=self.now + timedelta(seconds=2))[0].invitation_id)
        self.assert_no_agreement()

    def test_replaced_real_invoice_cannot_borrow_old_delivered_question(self):
        self.clarification()
        cancelled = terminalize_generation_attempt(self.attempt.pk,
            terminal_status=PaymentAttempt.Status.CANCELLED, reason="provider_cancelled",
            terminal_source="provider_pull", provider_status="cancelled",
            provider_payload=HostedSettlementFixture.payload(self, self.attempt, status="cancelled"), now=self.now)
        self.assertEqual(cancelled["outcome"], "terminalized")
        self.proposal.refresh_from_db()
        replacement = HostedSettlementFixture.invoice(self, self.proposal, "reminder-dialog-new-invoice")
        self.assertNotEqual(replacement.pk, self.attempt.pk)
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_manager_takeover_after_question_blocks_time_continuation(self):
        self.clarification()
        self.client_row.manager_takeover = True
        self.client_row.save(update_fields=["manager_takeover"])
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_bot_pause_after_question_blocks_time_continuation(self):
        self.clarification()
        self.client_row.bot_paused = True
        self.client_row.save(update_fields=["bot_paused"])
        self.assertFalse(self.proposed_time()[0].invitation_id)
        self.assert_no_agreement()

    def test_intervening_unrelated_intent_stops_bare_time_ownership(self):
        self.clarification()
        unrelated = self.source("Який склад тканини?", at=self.now + timedelta(seconds=1))
        unrelated.status = "done"
        unrelated.save(update_fields=["status"])
        result, _source, _revision, _token = self.proposed_time(at=self.now + timedelta(seconds=2))
        self.assertFalse(result.invitation_id, result)
        self.assert_no_agreement()

    def test_expired_thirty_minute_question_cannot_schedule_from_bare_time(self):
        case = self.clarification()
        self.assertLessEqual(case["pending"].expires_at, self.now + timedelta(minutes=30))
        result, _source, _revision, _token = self.proposed_time(at=case["pending"].expires_at + timedelta(seconds=1))
        self.assertFalse(result.invitation_id, result)
        self.assert_no_agreement()

    def test_mixed_negative_and_quoted_time_answers_never_become_implicit_consent(self):
        self.clarification()
        for text in ("15:30 і додайте футболку", "не о 15:30", "«15:30»", "if I pay, at 15:30", "15:30, скільки доставка?"):
            with self.subTest(text=text):
                self.assertFalse(self.proposed_time(text)[0].invitation_id)
                self.assert_no_agreement()

    def test_batched_time_and_unrelated_question_cannot_take_deterministic_shortcut(self):
        self.clarification()
        result, _source, _revision, _token = self.proposed_time(extra_text="І яка ціна доставки?")
        self.assertFalse(result.invitation_id, result)
        self.assert_no_agreement()

    def test_time_reply_provider_clock_anchors_relative_due_and_keeps_original_deadlines(self):
        case = self.clarification(locale="en")
        answered_at = self.now + timedelta(minutes=10)
        result, source, _revision, _token = self.proposed_time("in 1 hour", at=answered_at)
        inv = IgPaymentReminderInvitation.objects.get(pk=result.invitation_id)
        self.assertEqual(inv.due_at, source.provider_created_at + timedelta(hours=1))
        self.assertEqual(inv.window_deadline, case["pending"].window_deadline)
        self.assertLessEqual(inv.window_deadline, case["source"].provider_created_at + timedelta(hours=23))
        self.assertLessEqual(inv.window_deadline, self.proposal.created_at + timedelta(hours=12))
        self.assertFalse(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).exists())

    def test_valid_daytime_inside_23h_but_after_12h_offer_cannot_extend_original_offer(self):
        self.clarification(locale="en")
        at = self.now + timedelta(seconds=1)
        parsed = parse_reminder_time_reply("tomorrow at 10:00", at, locale="en")
        self.assertTrue(parsed.due_at)
        self.assertLess(parsed.due_at, self.now + timedelta(hours=23))
        self.assertGreater(parsed.due_at, self.proposal.created_at + timedelta(hours=12))
        self.assertFalse(self.proposed_time("tomorrow at 10:00", at=at)[0].invitation_id)
        self.assert_no_agreement()

    def test_replaying_sent_question_and_same_time_revision_does_not_duplicate_cards(self):
        case = self.clarification()
        replay = self.execute_source(case["source"], revision=case["revision"], token=case["token"])
        self.assertEqual(replay["physical"], [])
        self.assertEqual(TimeRequest.objects.count(), 1)
        source = self.source("через 1 годину", at=self.now + timedelta(seconds=1))
        proposed = self.execute_source(source)
        self.assertEqual(proposed["result"].state, "completed", proposed["result"].reasons)
        replay = self.execute_source(source, revision=proposed["revision"], token=proposed["token"])
        self.assertEqual(replay["physical"], [])
        self.assertEqual(IgPaymentReminderInvitation.objects.count(), 1)
        self.assertEqual(IgPaymentReminderReceipt.objects.count(), 1)
        self.assertFalse(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).exists())


class PaymentReminderBareTimeClockTests(SimpleTestCase):
    def test_repeated_and_nonexistent_kyiv_dst_clocks_are_not_guessed(self):
        for text, anchor in (
            ("2026-10-25 at 03:30", datetime(2026, 10, 24, 9, tzinfo=datetime_timezone.utc)),
            ("2026-03-29 at 03:30", datetime(2026, 3, 28, 9, tzinfo=datetime_timezone.utc)),
        ):
            with self.subTest(text=text):
                parsed = parse_reminder_time_reply(text, anchor, locale="en")
                self.assertTrue(parsed.handled)
                self.assertIsNone(parsed.due_at)
                self.assertEqual(parsed.reason, "ambiguous_clock")

    def test_bare_time_parser_never_supplies_clock_or_silently_moves_quiet_time(self):
        anchor = datetime(2026, 10, 12, 9, tzinfo=datetime_timezone.utc)
        for text, timestamp, reason in (("15:30", None, "provider_clock_missing"),
            ("23:00", anchor, "quiet_hours"), ("11:00", anchor, "past_time")):
            with self.subTest(text=text):
                parsed = parse_reminder_time_reply(text, timestamp, locale="uk")
                self.assertTrue(parsed.handled)
                self.assertIsNone(parsed.due_at)
                self.assertEqual(parsed.reason, reason)
