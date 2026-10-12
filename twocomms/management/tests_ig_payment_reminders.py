"""Requested payment reminders retain real invoice and native delivery authority."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from management.models import (
    BotPolicyPublication, IgCheckoutInvoiceGeneration, IgCheckoutProposal,
    IgClient, IgCommercialEpisode, IgCustomerTurn, IgFollowUpTask,
    IgFunnelResetAudit, IgMarketingConsentInvitation, IgPaymentReminderAnswer,
    IgPaymentReminderInvitation, IgPaymentReminderReceipt, IgTurnMessage,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services import ig_payment_reminders as reminders
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_checkout_generation import terminalize_generation_attempt
from management.services.ig_revision_outbox import (
    PublicationBinding, claim_next_effect, finish_effect, mark_provider_started,
    plan_revision_effects,
)
from management.services.ig_turn_revisions import (
    claim_revision_preparation, claim_sealed_revision, create_collecting_revision,
    seal_revision,
)
from management.tests_ig_hosted_settlement import HostedSettlementFixture
from orders.models import PaymentAttempt
from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from storefront.models import Category, Product, ProductFitOption
from productcolors.models import Color, ProductColorVariant
from storefront.views.monobank import _apply_payment_attempt_status


@override_settings(
    GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test", IG_PAYMENT_REMINDERS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
)
class RequestedPaymentReminderTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = datetime(2026, 10, 12, 9, 0, tzinfo=datetime_timezone.utc)
        self.clock = self.now
        clock = patch("django.utils.timezone.now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        transport = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login",
            "IG_INSTAGRAM_BOT": "synthetic-payment-reminder-test-token"})
        transport.start()
        self.addCleanup(transport.stop)
        snapshot = {"schema_version": 1, "instructions": []}
        snapshot_hash = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.publication = BotPolicyPublication.objects.create(version=1, kind="publish",
            schema_version=1, snapshot=snapshot, snapshot_hash=snapshot_hash,
            compiler_version="instruction-set-v1", instruction_count=0)
        self.settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True,
            ig_user_id="owner-1", reply_permission_epoch=4,
            active_instruction_publication=self.publication)
        self.client_row = IgClient.objects.create(igsid="requested-reminder-client",
            reply_permission_epoch=3, language="uk", last_user_message_at=self.now)
        category = Category.objects.create(name="Reminder", slug="requested-reminder")
        self.product = Product.objects.create(title="Reminder shirt", slug="requested-reminder-shirt",
            category=category, price=900, status="published")
        self.fit = ProductFitOption.objects.create(product=self.product, code="classic",
            label="Classic", is_active=True, is_default=True)
        color = Color.objects.create(name="Чорний", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(product=self.product,
            color=color, stock=10, is_default=True)
        self.item = {"product_id": self.product.pk, "color_variant_id": self.variant.pk, "qty": 1, "size": "M",
                     "fit_option_code": self.fit.code}
        self.source_serial = 0
        self.proposal = self.quoted()
        self.attempt = HostedSettlementFixture.invoice(self, self.proposal, "requested-reminder-invoice")
        self.generation = IgCheckoutInvoiceGeneration.objects.get(payment_attempt=self.attempt)
        self.client_row.refresh_from_db()
        self.episode = IgCommercialEpisode.objects.get(pk=self.client_row.current_commercial_episode_id)
        self.binding = PublicationBinding(self.publication.pk, self.publication.version,
                                         self.publication.snapshot_hash)

    def quoted(self):
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from management.services.ig_revision_checkout import checkout_authority_control
        source = self.source(f"добавьте {self.product.title}, количество 1, размер M, цвет чёрный, крій classic")
        with transaction.atomic():
            admitted = reduce_inbound_commerce_source(self.client_row, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(admitted.ready, admitted.reason)
        source.status = "done"
        source.save(update_fields=["status"])
        self.client_row.refresh_from_db()
        captured = capture_current_selection_lines(self.client_row.pk)
        control, reasons = checkout_authority_control(self.client_row, {}, source_cart_capture=captured)
        self.assertFalse(reasons, reasons)
        return create_or_update_proposal(client=self.client_row, pay_type="online_full",
            item_specs=control["items"], source_cart_binding=control["source_cart_binding"],
            source_cart_capture=control["source_cart_capture"], checkout_owner_scope=control["checkout_owner_scope"])

    @staticmethod
    def native_buttons(result):
        from management.services.instagram_bot import _provider_quick_replies_payload
        return _provider_quick_replies_payload(result.quick_replies)

    def source(self, text, *, payload="", client=None, at=None, namespace="instagram_login:owner-1"):
        self.source_serial += 1
        owner = client or self.client_row
        return InstagramBotMessage.objects.create(client=owner, sender_id=owner.igsid,
            provider_namespace=namespace, role="user", source="webhook", text=text,
            quick_reply_payload=payload, mid=f"reminder-input-{self.source_serial}",
            status="pending", provider_created_at=at or self.now)

    def revision(self, source, *, extra_sources=(), now=None):
        now = now or self.now
        sources = [source, *extra_sources]
        turn = IgCustomerTurn.objects.create(client=source.client, primary_source_message=source,
            window_started_at=now, window_deadline=now)
        for index, row in enumerate(sources, 1):
            IgTurnMessage.objects.create(turn=turn, message=row, ordinal=index, role=row.role)
        built = create_collecting_revision(turn, sources, now=now, bypass_quiet=True)
        self.assertIsNotNone(built.revision, built.reason)
        prep = claim_revision_preparation(built.revision.pk, now=now)
        sealed = seal_revision(built.revision.pk, prep.token, now=now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claim = claim_sealed_revision(sealed.revision.pk, now=now)
        self.assertIsNotNone(claim.revision, claim.reason)
        return claim.revision, claim.token

    def request(self, text="Нагадай про оплату через 1 годину", *, at=None, extra_text=None):
        source = self.source(text, at=at)
        extra = (self.source(extra_text),) if extra_text else ()
        revision, token = self.revision(source, extra_sources=extra)
        result = reminders.prepare_request(revision, source, settings_obj=self.settings, now=self.now)
        return result, source, revision, token

    def sent_invitation(self, *, text="Нагадай про оплату через 1 годину", outcome="response", mid="reminder-invitation-mid", wrong_body=False):
        result, source, revision, token = self.request(text)
        self.assertTrue(result.handled, result.reason)
        self.assertTrue(result.invitation_id, result.reason)
        payload = {"recipient": {"id": self.client_row.igsid}, "message": {
            "text": "different body" if wrong_body else result.reply_text,
            "quick_replies": self.native_buttons(result)}}
        planned = plan_revision_effects(revision.pk, token, source_message_id=source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest="e" * 64,
            effects=[{"group": "substantive_text", "kind": "text", "payload": payload}])
        self.assertTrue(planned.created, planned.reasons)
        claim = claim_next_effect(revision.pk, token, "substantive_text", now=self.now)
        self.assertIsNotNone(claim.effect, claim.reason)
        started = mark_provider_started(claim.effect.pk, claim.token, token, now=self.now)
        self.assertEqual(started.reason, "provider_started")
        finish_effect(claim.effect.pk, claim.token, provider_namespace="instagram_login:owner-1",
            http_status=200 if outcome == "response" else None,
            provider_message_id=mid, transport_outcome=outcome, now=self.now)
        reminders.record_delivery(revision.pk)
        invitation = IgPaymentReminderInvitation.objects.get(pk=result.invitation_id)
        return invitation, revision

    def accept(self, invitation, *, text=None, payload=None, client=None, at=None, namespace="instagram_login:owner-1"):
        answer_at = at or self.now + timedelta(seconds=1)
        source = self.source(text or invitation.button_title,
            payload=invitation.accept_payload if payload is None else payload,
            client=client, at=answer_at, namespace=namespace)
        revision, _token = self.revision(source, now=answer_at)
        result = reminders.handle_reply(source, revision=revision, settings_obj=self.settings, now=answer_at)
        return result, source

    def accepted(self, **kwargs):
        invitation, revision = self.sent_invitation(**kwargs)
        self.assertTrue(IgPaymentReminderReceipt.objects.filter(invitation=invitation).exists())
        result, source = self.accept(invitation)
        self.assertTrue(result.handled, result.reason)
        answer = IgPaymentReminderAnswer.objects.get(source=source)
        self.assertIsNotNone(answer.task_id)
        return invitation, answer.task, revision

    def sent_acceptance(self, task):
        from management.models import IgCustomerTurnRevision
        answer = IgPaymentReminderAnswer.objects.get(task=task, kind="accept")
        revision = IgCustomerTurnRevision.objects.get(pk=answer.envelope["source"]["revision_id"])
        result = reminders.handle_reply(answer.source, revision=revision,
            settings_obj=self.settings, now=answer.answered_at)
        self.assertEqual(len(result.quick_replies), 1)
        planned = plan_revision_effects(revision.pk, revision.claim_token,
            source_message_id=answer.source_id, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest="e" * 64,
            effects=[{"group": "substantive_text", "kind": "text", "payload": {
                "recipient": {"id": self.client_row.igsid}, "message": {
                    "text": result.reply_text, "quick_replies": self.native_buttons(result)}}}])
        self.assertTrue(planned.created, planned.reasons)
        claim = claim_next_effect(revision.pk, revision.claim_token,
            "substantive_text", now=answer.answered_at)
        self.assertIsNotNone(claim.effect, claim.reason)
        started = mark_provider_started(claim.effect.pk, claim.token,
            revision.claim_token, now=answer.answered_at)
        self.assertEqual(started.reason, "provider_started")
        finished = finish_effect(claim.effect.pk, claim.token,
            provider_namespace="instagram_login:owner-1", http_status=200,
            provider_message_id="accepted-reminder-cancel-card-mid", now=answer.answered_at)
        self.assertTrue(finished.changed, finished.reason)
        return "accepted-reminder-cancel-card-mid"

    def expire(self, *, reason="invoice_expired"):
        expired_at = self.generation.expires_at + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=expired_at):
            outcome = terminalize_generation_attempt(self.attempt.pk,
                terminal_status=PaymentAttempt.Status.EXPIRED, reason=reason,
                terminal_source="system_expiry", require_due=True, now=expired_at)
        self.assertEqual(outcome["outcome"], "terminalized")
        self.proposal.refresh_from_db()
        self.generation.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.generation.state, "expired")
        self.assertIsNone(self.proposal.current_invoice_generation_id)
        self.assertIsNone(self.proposal.payment_attempt_id)
        self.assertTrue(self.generation.events.filter(kind="terminalized",
            payload__state="expired", payload__reason=reason).exists())

    def pay(self):
        payload = HostedSettlementFixture.payload(self, self.attempt)
        order, created = _apply_payment_attempt_status(self.attempt, "success",
            payload=payload, source="provider_pull")
        self.assertTrue(created)
        self.assertIsNotNone(order)
        self.attempt.refresh_from_db()
        self.assertIsNotNone(self.attempt.order_id)
        return order

    def dispatch(self, task, *, before_http=None, outcome="sent", at=None):
        from management.services import instagram_bot
        from management.models import IgCustomerTurnRevision
        due = at or task.due_at
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(
            state="processed", active_slot=None, claim_token="", lease_until=None)
        requests = []

        def lookup_token(_settings, **kwargs):
            if before_http is not None:
                before_http()
            return "unit-test-provider-token"

        def transport(_settings, _url, *, data, **kwargs):
            requests.append(json.loads(data))
            if outcome == "unknown":
                raise TimeoutError("unknown physical delivery")
            return 200, json.dumps({"message_id": "agreed-payment-reminder-mid"})

        with patch("django.utils.timezone.now", return_value=due), patch.object(
            instagram_bot, "get_page_token", side_effect=lookup_token), patch.object(
            instagram_bot, "_provider_http", side_effect=transport):
            count = reminders.process_due_payment_reminders(self.settings, now=due, limit=50)
        return count, requests

    def test_explicit_uk_ru_en_requests_use_provider_clock_and_one_positive_button(self):
        for text, locale, due in (
            ("Нагадай про оплату о 15:00", "uk", self.now + timedelta(hours=3)),
            ("Напомни об оплате через 1 час", "ru", self.now + timedelta(hours=1)),
            ("Remind me to pay in 60 minutes", "en", self.now + timedelta(hours=1)),
        ):
            with self.subTest(locale=locale):
                result, source, _, _ = self.request(text)
                self.assertTrue(result.handled, result.reason)
                invitation = IgPaymentReminderInvitation.objects.get(pk=result.invitation_id)
                self.assertEqual((invitation.locale, invitation.due_at), (locale, due))
                self.assertEqual(invitation.request_source_id, source.pk)
                self.assertEqual(len(result.quick_replies), 1)
                self.assertEqual(self.native_buttons(result)[0]["payload"], invitation.accept_payload)
                self.assertNotIn(invitation.revoke_payload, json.dumps(self.native_buttons(result)))
                self.assertIn(due.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%H:%M"), result.reply_text)
        self.assertFalse(IgMarketingConsentInvitation.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_relative_request_uses_provider_timestamp_instead_of_ingestion_clock(self):
        anchor = self.now - timedelta(minutes=10)
        result, _, _, _ = self.request("Напомни об оплате через 1 час", at=anchor)
        invitation = IgPaymentReminderInvitation.objects.get(pk=result.invitation_id)
        self.assertEqual(invitation.due_at, anchor + timedelta(hours=1))
        self.assertNotEqual(invitation.due_at, self.now + timedelta(hours=1))

    def test_mixed_hypothetical_and_negated_sources_do_not_create_invitations(self):
        for text in ("Не нагадуй про оплату через 1 годину", "If I order, remind me to pay at 15:00",
                     "Напомни об оплате в 15:00 и добавь ещё одну футболку"):
            with self.subTest(text=text):
                result, _, _, _ = self.request(text)
                self.assertFalse(result.invitation_id)
        self.assertFalse(IgPaymentReminderInvitation.objects.exists())

    def test_quiet_hours_past_and_window_overflow_are_not_silently_shifted(self):
        for text in ("Напомни об оплате в 23:00", "Remind me to pay at 11:00",
                     "Нагадай про оплату завтра о 15:00"):
            with self.subTest(text=text):
                result, _, _, _ = self.request(text)
                self.assertTrue(result.handled, result.reason)
                self.assertFalse(result.invitation_id)
                self.assertTrue(result.reply_text)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_real_native_sent_effect_and_signed_answer_create_one_existing_payment_task(self):
        invitation, task, revision = self.accepted()
        self.assertEqual((task.kind, task.trigger, task.due_at), ("payment", "reactive", invitation.due_at))
        self.assertEqual(task.deal_id, self.proposal.deal_id)
        self.assertEqual(IgPaymentReminderReceipt.objects.count(), 1)
        self.assertEqual(IgPaymentReminderAnswer.objects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), 1)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
        answer = IgPaymentReminderAnswer.objects.get(task=task)
        repeated = reminders.handle_reply(answer.source, settings_obj=self.settings, now=self.now + timedelta(seconds=2))
        self.assertTrue(repeated.handled, repeated.reason)
        duplicate, _ = self.accept(invitation, at=self.now + timedelta(seconds=3))
        self.assertTrue(duplicate.handled, duplicate.reason)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(IgPaymentReminderAnswer.objects.count(), 1)
        reminders.record_delivery(revision.pk)
        self.assertEqual(IgPaymentReminderReceipt.objects.count(), 1)

    def test_free_text_confirmation_cannot_grant_reminder(self):
        invitation, _ = self.sent_invitation()
        result, _ = self.accept(invitation, text="Так", payload="")
        self.assertFalse(IgPaymentReminderAnswer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.assertFalse(result.invitation_id)

    def test_foreign_recipient_namespace_and_tampered_payload_fail_closed(self):
        invitation, _ = self.sent_invitation()
        other = IgClient.objects.create(igsid="other-reminder-recipient")
        for kwargs in ({"client": other}, {"namespace": "instagram_login:other-owner"},
                       {"payload": invitation.accept_payload[:-1] + ("0" if invitation.accept_payload[-1] != "0" else "1")}):
            with self.subTest(kwargs=kwargs):
                self.accept(invitation, **kwargs)
                self.assertFalse(IgPaymentReminderAnswer.objects.exists())
                self.assertFalse(IgFollowUpTask.objects.exists())

    def test_unknown_or_missing_mid_native_delivery_cannot_authorize_answer(self):
        invitation, _ = self.sent_invitation(outcome="timeout", mid="")
        self.assertFalse(IgPaymentReminderReceipt.objects.exists())
        self.accept(invitation)
        self.assertFalse(IgPaymentReminderAnswer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_native_sent_receipt_with_different_body_cannot_authorize_answer(self):
        invitation, _ = self.sent_invitation(wrong_body=True)
        self.assertFalse(IgPaymentReminderReceipt.objects.exists())
        self.accept(invitation)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_reset_between_invitation_and_confirmation_blocks_answer(self):
        invitation, _ = self.sent_invitation()
        IgFunnelResetAudit.objects.create(client=self.client_row,
            reset_after_message_id=invitation.request_source_id, reason="reminder reset")
        self.accept(invitation)
        self.assertFalse(IgPaymentReminderAnswer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_failed_or_mixed_sealed_request_has_no_invitation(self):
        source = self.source("Нагадай про оплату через 1 годину")
        source.status = "failed"
        source.save(update_fields=["status"])
        revision, _token = self.revision(source)
        result = reminders.prepare_request(revision, source, settings_obj=self.settings, now=self.now)
        self.assertFalse(result.invitation_id)
        result, _, _, _ = self.request(extra_text="І додайте ще одну футболку")
        self.assertFalse(result.invitation_id)
        self.assertFalse(IgPaymentReminderInvitation.objects.exists())

    def test_manager_review_and_unknown_generation_never_get_invitation(self):
        for state in ("provider_inflight", "provider_ambiguous", "late_provider_review", "ambiguity_review"):
            with self.subTest(state=state):
                IgCheckoutInvoiceGeneration.objects.filter(pk=self.generation.pk).update(state=state)
                result, _, _, _ = self.request()
                self.assertFalse(result.invitation_id)
        self.assertFalse(IgPaymentReminderInvitation.objects.exists())

    def test_missing_current_episode_or_generation_never_borrows_historical_invoice(self):
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=None)
        result, _, _, _ = self.request()
        self.assertFalse(result.invitation_id)
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=self.episode)
        cancelled = terminalize_generation_attempt(self.attempt.pk,
            terminal_status=PaymentAttempt.Status.CANCELLED, reason="provider_cancelled",
            terminal_source="provider_pull", provider_status="cancelled",
            provider_payload=HostedSettlementFixture.payload(self, self.attempt, status="cancelled"),
            now=self.now)
        self.assertEqual(cancelled["outcome"], "terminalized")
        self.proposal.refresh_from_db()
        self.assertIsNone(self.proposal.current_invoice_generation_id)
        self.assertIsNone(self.proposal.payment_attempt_id)
        result, _, _, _ = self.request()
        self.assertFalse(result.invitation_id)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_natural_expiry_preserves_agreed_reminder_without_link_or_invoice_reissue(self):
        invitation, task, _ = self.accepted()
        self.assertGreater(task.due_at, self.generation.expires_at)
        self.expire()
        invoice_count = IgCheckoutInvoiceGeneration.objects.count()
        attempt_count = PaymentAttempt.objects.count()
        count, requests = self.dispatch(task)
        self.assertEqual(count, 1)
        self.assertEqual(len(requests), 1)
        body = requests[0]["message"]["text"]
        self.assertNotIn("http", body)
        self.assertNotIn(self.attempt.invoice_url, body)
        self.assertRegex(body.lower(), r"минув|сплив|неактив|expired|ист[её]к")
        self.assertNotRegex(body.lower(), r"товари.*збереж|товары.*сохран|stock|reserved|ціна.*збереж")
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), invoice_count)
        self.assertEqual(PaymentAttempt.objects.count(), attempt_count)
        task.refresh_from_db()
        self.assertEqual(task.status, "sent")
        self.assertEqual(task.provider_message_id, "agreed-payment-reminder-mid")
        count, requests = self.dispatch(task)
        self.assertEqual((count, requests), (0, []))

    def test_elapsed_expiry_before_cron_uses_no_url_copy(self):
        _, task, _ = self.accepted()
        self.generation.refresh_from_db()
        self.assertEqual(self.generation.state, "invoice_created")
        count, requests = self.dispatch(task)
        self.assertEqual((count, len(requests)), (1, 1))
        self.assertNotIn("http", requests[0]["message"]["text"])
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), 1)

    def test_signed_answer_after_natural_expiry_keeps_original_due_and_no_reissue(self):
        invitation, _ = self.sent_invitation()
        self.expire()
        answer_at = self.generation.expires_at + timedelta(minutes=1)
        result, source = self.accept(invitation, at=answer_at)
        self.assertTrue(result.handled, result.reason)
        answer = IgPaymentReminderAnswer.objects.get(source=source)
        self.assertEqual(answer.task.due_at, invitation.due_at)
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), 1)
        count, requests = self.dispatch(answer.task)
        self.assertEqual((count, len(requests)), (1, 1))
        self.assertNotIn("http", requests[0]["message"]["text"])

    def test_reissued_invoice_never_inherits_original_agreed_reminder(self):
        _, task, _ = self.accepted()
        self.expire()
        payload = {"full_name": "Іван Петренко", "phone": "+380501112233",
            "email": "buyer@example.com", "city": "Київ",
            "np_settlement_ref": "settlement-ref", "np_city_ref": "city-ref",
            "np_city_token": build_city_choice_token({"label": "Київ",
                "settlement_ref": "settlement-ref", "city_ref": "city-ref"}),
            "np_office": "Відділення №12", "np_warehouse_ref": "warehouse-ref",
            "np_warehouse_token": build_warehouse_choice_token({"label": "Відділення №12",
                "ref": "warehouse-ref", "kind": "branch", "city_ref": "city-ref"}),
            "payment_choice": "online_full", "reissue_generation": str(self.generation.generation)}
        with patch("django.utils.timezone.now", return_value=self.generation.expires_at + timedelta(seconds=2)), patch(
            "storefront.views.monobank._monobank_api_request", return_value={
                "invoiceId": "replacement-reminder-invoice", "pageUrl": "https://pay.example/replacement-reminder-invoice"}):
            response = self.client.post(reverse("ig_checkout_proposal",
                kwargs={"proposal_id": self.proposal.public_id}), payload, HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), 2)
        count, requests = self.dispatch(task)
        self.assertEqual((count, requests), (0, []))
        task.refresh_from_db()
        self.assertIn(task.status, {"skipped", "cancelled"})

    def test_actual_new_purchase_episode_invalidates_old_agreement(self):
        from management.services.ig_commercial_episodes import start_repeat_episode
        _, task, _ = self.accepted()
        source = self.source("Хочу ще одну футболку на подарунок")
        new = start_repeat_episode(self.client_row, repeat_kind="gift",
            evidence_message_ids=[source.pk], confidence=Decimal("1.00"),
            analysis_model="source_parser", analysis_prompt_version="payment_reminder_tests")
        self.assertNotEqual(new.pk, self.episode.pk)
        count, requests = self.dispatch(task)
        self.assertEqual((count, requests), (0, []))

    def test_paid_invoice_before_answer_creates_no_reminder_task(self):
        invitation, _ = self.sent_invitation()
        self.pay()
        self.accept(invitation)
        self.assertFalse(IgPaymentReminderAnswer.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_paid_between_claim_and_physical_http_sends_nothing(self):
        _, task, _ = self.accepted()
        count, requests = self.dispatch(task, before_http=self.pay)
        self.assertEqual((count, requests), (0, []))
        task.refresh_from_db()
        self.assertIn(task.status, {"cancelled", "skipped"})

    def test_reset_between_claim_and_physical_http_sends_nothing(self):
        _, task, _ = self.accepted()
        def reset():
            IgFunnelResetAudit.objects.create(client=self.client_row,
                reset_after_message_id=InstagramBotMessage.objects.filter(client=self.client_row).latest("pk").pk,
                reason="late reminder reset")
        count, requests = self.dispatch(task, before_http=reset)
        self.assertEqual((count, requests), (0, []))

    def test_takeover_between_token_lookup_and_physical_http_sends_nothing(self):
        _, task, _ = self.accepted()
        def takeover():
            IgClient.objects.filter(pk=self.client_row.pk).update(manager_takeover=True,
                reply_permission_epoch=self.client_row.reply_permission_epoch + 1)
        count, requests = self.dispatch(task, before_http=takeover)
        self.assertEqual((count, requests), (0, []))

    def test_service_case_opened_during_token_lookup_blocks_physical_http(self):
        from management.models import IgPostSaleCase
        _, task, _ = self.accepted()
        def service_case():
            source = self.source("Хочу повернути отриману футболку")
            IgPostSaleCase.objects.create(client=self.client_row, commercial_episode=self.episode,
                source_message=source, case_type="return", status="open")
        count, requests = self.dispatch(task, before_http=service_case)
        self.assertEqual((count, requests), (0, []))

    def test_unknown_physical_delivery_is_not_blindly_replayed(self):
        _, task, _ = self.accepted()
        count, requests = self.dispatch(task, outcome="unknown")
        self.assertEqual((count, len(requests)), (0, 1))
        task.refresh_from_db()
        self.assertEqual(task.status, "ambiguous")
        count, requests = self.dispatch(task, at=task.due_at + timedelta(minutes=1))
        self.assertEqual((count, requests), (0, []))

    def test_worker_late_more_than_five_minutes_does_not_break_time_promise(self):
        _, task, _ = self.accepted()
        count, requests = self.dispatch(task, at=task.due_at + timedelta(minutes=5, seconds=1))
        self.assertEqual((count, requests), (0, []))
        task.refresh_from_db()
        self.assertIn(task.status, {"skipped", "cancelled"})

    def test_expired_processing_claim_is_ambiguous_and_never_requeued(self):
        _, task, _ = self.accepted()
        IgFollowUpTask.objects.filter(pk=task.pk).update(status="processing", attempt_count=1,
            claim_token="stranded-payment-reminder", claim_until=task.due_at - timedelta(seconds=1))
        count, requests = self.dispatch(task)
        self.assertEqual((count, requests), (0, []))
        task.refresh_from_db()
        self.assertEqual(task.status, "ambiguous")
        self.assertEqual(task.provider_message_id, "")

    def test_admin_projection_never_exposes_payment_url_or_signed_action_payload(self):
        invitation, task, _ = self.accepted()
        projection = reminders.payment_reminder_projection(self.client_row, now=self.now + timedelta(seconds=2))
        serialized = json.dumps(projection, ensure_ascii=False)
        self.assertNotIn(self.attempt.invoice_url, serialized)
        self.assertNotIn(invitation.accept_payload, serialized)
        self.assertNotIn(invitation.revoke_payload, serialized)
        self.assertFalse(IgMarketingConsentInvitation.objects.exists())

    def test_native_mid_survives_history_failure_and_recovers_without_another_http(self):
        _, task, _ = self.accepted()
        create = InstagramBotMessage.objects.create
        def fail_model_history(**kwargs):
            if kwargs.get("role") == "model":
                raise RuntimeError("local history projection failed after native receipt")
            return create(**kwargs)
        with patch.object(InstagramBotMessage.objects, "create", side_effect=fail_model_history):
            count, requests = self.dispatch(task)
        self.assertEqual((count, len(requests)), (1, 1))
        task.refresh_from_db()
        self.assertEqual((task.status, task.provider_message_id), ("sent", "agreed-payment-reminder-mid"))
        self.assertIsNone(task.sent_message_id)
        count, requests = self.dispatch(task, at=task.due_at + timedelta(seconds=10))
        self.assertEqual((count, requests), (0, []))
        task.refresh_from_db()
        self.assertIsNotNone(task.sent_message_id)
        self.assertEqual(task.sent_message.provider_message_id, task.provider_message_id)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.client_row, role="model",
            provider_message_id=task.provider_message_id).count(), 1)

    def test_explicit_no_buy_during_token_lookup_blocks_physical_http(self):
        _, task, _ = self.accepted()
        def no_buy():
            IgClient.objects.filter(pk=self.client_row.pk).update(primary_objection="no_buy", lost_reason="stop")
        count, requests = self.dispatch(task, before_http=no_buy)
        self.assertEqual((count, requests), (0, []))

    def test_signed_revocation_cancels_only_its_agreed_task(self):
        invitation, task, _ = self.accepted()
        cancel_card_mid = self.sent_acceptance(task)
        unrelated = IgFollowUpTask.objects.create(client=self.client_row, due_at=task.due_at,
            kind="manager_task", reason="parcel_reminder:separate-order")
        source = self.source("Скасувати нагадування", payload=invitation.revoke_payload,
            at=self.now + timedelta(seconds=2))
        source.reply_to_provider_message_id = cancel_card_mid
        source.save(update_fields=["reply_to_provider_message_id"])
        revision, _token = self.revision(source)
        result = reminders.handle_reply(source, revision=revision, settings_obj=self.settings,
            now=self.now + timedelta(seconds=2))
        self.assertTrue(result.handled, result.reason)
        task.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(unrelated.status, "pending")
        self.assertEqual(IgPaymentReminderAnswer.objects.filter(kind="revoke").count(), 1)

    def test_revocation_requires_delivered_cancel_card_and_signed_native_action(self):
        invitation, task, _ = self.accepted()
        self.accept(invitation, text="Скасувати нагадування", payload="")
        self.assertFalse(IgPaymentReminderAnswer.objects.filter(kind="revoke").exists())
        self.accept(invitation, text="Скасувати нагадування", payload=invitation.revoke_payload)
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertFalse(IgPaymentReminderAnswer.objects.filter(kind="revoke").exists())

    def test_historical_paid_episode_does_not_suppress_current_unpaid_repeat(self):
        from management.services.ig_commercial_episodes import start_repeat_episode
        old_proposal = self.proposal
        old_attempt = self.attempt
        old_order = self.pay()
        source = self.source("Хочу ще одну таку футболку")
        self.episode = start_repeat_episode(self.client_row, repeat_kind="explicit_more",
            evidence_message_ids=[source.pk], confidence=Decimal("1.00"),
            analysis_model="source_parser", analysis_prompt_version="payment_reminder_tests")
        self.client_row.refresh_from_db()
        self.proposal = self.quoted()
        self.attempt = HostedSettlementFixture.invoice(self, self.proposal, "current-repeat-reminder-invoice")
        self.generation = IgCheckoutInvoiceGeneration.objects.get(payment_attempt=self.attempt)
        result, _, _, _ = self.request()
        self.assertTrue(result.invitation_id, result.reason)
        invitation = IgPaymentReminderInvitation.objects.get(pk=result.invitation_id)
        self.assertEqual(invitation.commercial_episode_id, self.episode.pk)
        self.assertEqual(invitation.deal_id, self.proposal.deal_id)
        old_proposal.refresh_from_db()
        old_attempt.refresh_from_db()
        self.assertEqual(old_proposal.status, "paid")
        self.assertEqual(old_attempt.order_id, old_order.pk)
        self.assertNotEqual(invitation.proposal_id, old_proposal.pk)


@override_settings(
    GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test", IG_PAYMENT_REMINDERS_ENABLED=True,
    IG_ASSISTED_CHECKOUT_V2="enforced", IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100,
)
class RequestedPaymentReminderLiveExecutionTests(TransactionTestCase):
    """Exercise the real revision entry and postback owners outside a transaction."""
    reset_sequences = True
    setUp = RequestedPaymentReminderTests.setUp
    quoted = RequestedPaymentReminderTests.quoted
    source = RequestedPaymentReminderTests.source
    revision = RequestedPaymentReminderTests.revision

    def test_live_revision_request_and_signed_postback_send_without_gemini(self):
        from management.services.ig_revision_live import execute_claimed_revision
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        request = self.source("Нагадай про оплату через 1 годину")
        request_revision, request_token = self.revision(request)
        cart_before = capture_current_selection_lines(self.client_row.pk)
        physical = []
        def provider(_settings, _url, *, data, **kwargs):
            payload = json.loads(data)
            physical.append(payload)
            return 200, json.dumps({"message_id": f"live-payment-reminder-{len(physical)}"})
        with patch("management.services.instagram_bot.get_page_token", return_value="synthetic-test-token"), patch(
            "management.services.instagram_bot._provider_http", side_effect=provider), patch(
            "management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("explicit reminder must use no Gemini")) as gemini:
            first = execute_claimed_revision(request_revision.pk, request_token, self.settings)
            self.assertEqual(first.state, "completed", first.reasons)
            self.assertEqual(first.sent_parts, 1)
            self.assertEqual(len(physical), 1)
            request_revision.refresh_from_db()
            self.assertTrue(IgPaymentReminderInvitation.objects.filter(request_source=request).exists(),
                json.dumps({"state": first.state, "reasons": first.reasons,
                    "physical": physical, "action_receipts": request_revision.action_receipts,
                    "cart_before": cart_before, "cart_after": capture_current_selection_lines(self.client_row.pk)},
                    ensure_ascii=False, default=str))
            invitation = IgPaymentReminderInvitation.objects.get(request_source=request)
            receipt = IgPaymentReminderReceipt.objects.get(invitation=invitation)
            self.assertEqual(receipt.provider_message_id, "live-payment-reminder-1")
            self.assertEqual(physical[0]["message"]["text"], invitation.body)
            self.assertEqual(physical[0]["message"]["quick_replies"], [{"content_type": "text",
                "title": invitation.button_title, "payload": invitation.accept_payload}])
            self.assertFalse(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).exists())
            answer = self.source(invitation.button_title, payload=invitation.accept_payload,
                at=self.now + timedelta(seconds=1))
            answer.reply_to_provider_message_id = receipt.provider_message_id
            answer.save(update_fields=["reply_to_provider_message_id"])
            answer_revision, answer_token = self.revision(answer, now=self.now + timedelta(seconds=1))
            with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=1)):
                second = execute_claimed_revision(answer_revision.pk, answer_token, self.settings)
            self.assertEqual(second.state, "completed", second.reasons)
            self.assertEqual(second.sent_parts, 1)
            self.assertEqual(len(physical), 2)
            accepted = IgPaymentReminderAnswer.objects.get(source=answer, kind="accept")
            self.assertEqual(accepted.task.due_at, self.now + timedelta(hours=1))
            self.assertEqual(IgFollowUpTask.objects.filter(reason=reminders.ORIGIN).count(), 1)
            self.assertEqual(IgCheckoutInvoiceGeneration.objects.count(), 1)
            self.assertEqual(len(physical[1]["message"]["quick_replies"]), 1)
            cancel_payload = physical[1]["message"]["quick_replies"][0]["payload"]
            self.assertEqual(cancel_payload, invitation.revoke_payload)
            revoke = self.source("Скасувати нагадування", payload=cancel_payload,
                at=self.now + timedelta(seconds=2))
            revoke.reply_to_provider_message_id = "live-payment-reminder-2"
            revoke.save(update_fields=["reply_to_provider_message_id"])
            revoke_revision, revoke_token = self.revision(revoke, now=self.now + timedelta(seconds=2))
            with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=2)):
                third = execute_claimed_revision(revoke_revision.pk, revoke_token, self.settings)
            self.assertEqual(third.state, "completed", third.reasons)
            self.assertEqual(len(physical), 3)
            accepted.task.refresh_from_db()
            self.assertEqual(accepted.task.status, "cancelled")
            self.assertEqual(IgPaymentReminderAnswer.objects.filter(kind="revoke").count(), 1)
            gemini.assert_not_called()
