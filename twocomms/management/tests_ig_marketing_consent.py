"""Native purchase/source consent ledger; only provider transport is replaced."""
from datetime import timedelta
import hashlib
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.ig_consent_models import IgMarketingConsentAnswer, IgMarketingConsentInvitation
from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
from management.services import ig_marketing_consent as consent
from management.services.ig_order_assignments import link_order_to_client, unlink_order_from_client
from orders.models import Order


class _MarketingConsentFixture:
    """Reusable receipt/answer setup; no synthetic grant or authority patch."""

    def consent_source(self, customer, *, text="Please reply in English. Thank you for my order.",
                       at=None, payload="", **changes):
        from management.services.instagram_bot import ingress_provider_namespace
        self.serial += 1
        values = dict(client=customer, sender_id=customer.igsid, role="user", source="webhook",
            status="done", provider_namespace=ingress_provider_namespace(self.settings_row),
            mid=f"consent-source-{self.serial}", text=text,
            provider_created_at=at or timezone.now(), quick_reply_payload=payload)
        values.update(changes)
        return InstagramBotMessage.objects.create(**values)

    def grant_business_consent(self, client, order, assignment, *, text=None):
        """Exercise actual queue, Meta receipt adapter and signed inbound owner."""
        source = self.consent_source(client, **({"text": text} if text is not None else {}))
        client.last_user_message_at = source.provider_created_at
        client.save(update_fields=["last_user_message_at", "updated_at"])
        invitation = consent.queue_post_purchase_consent(client, order, assignment)
        self.assertIsNotNone(invitation, "Actual paid assignment/source must permit consent invitation")
        with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http",
                return_value=(200, json.dumps({"message_id": f"mid.consent.{invitation.pk.hex}"}))) as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "sent")
        http.assert_called_once()
        answer = self.consent_source(client, text="Keep me updated", payload=invitation.accept_payload)
        outcome = consent.handle_consent_reply(answer)
        self.assertEqual(outcome.reason, "business_consent_accept")
        self.assertTrue(consent.has_post_purchase_consent(client, order.pk))
        return invitation, answer


@override_settings(IG_POST_PURCHASE_BUSINESS_CONSENT_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class MarketingConsentAuthorityTests(_MarketingConsentFixture, TestCase):
    def setUp(self):
        cache.clear()
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True, ig_user_id="1")
        self.actor = get_user_model().objects.create_user(username="consent-manager", is_staff=True)
        self.serial = 0
        self.now = timezone.now() - timedelta(seconds=1)
        self.customer, self.order, self.assignment, self.source = self.native_purchase()

    def native_purchase(self, *, text="Please reply in English. Thank you for my order.", profile="uk",
                        assignment_source="manager_manual"):
        self.serial += 1
        customer = IgClient.objects.create(igsid=f"consent-customer-{self.serial}", language=profile)
        order = Order.objects.create(order_number=f"CONSENT-{self.serial}", full_name="Website buyer",
            phone="380501112233", total_sum="790.00", payment_status="paid", source="web", status="new")
        assignment = link_order_to_client(order, client=customer, actor=self.actor, source=assignment_source)
        source = self.consent_source(customer, text=text, at=self.now - timedelta(minutes=1))
        customer.last_user_message_at = source.provider_created_at
        customer.save(update_fields=["last_user_message_at", "updated_at"])
        return customer, order, assignment, source

    def queue(self):
        invitation = consent.queue_post_purchase_consent(self.customer, self.order, self.assignment, now=self.now)
        self.assertIsNotNone(invitation)
        return invitation

    def deliver(self, invitation, *, response=None):
        def transport(*args, **kwargs):
            return response or (200, json.dumps({"message_id": f"mid.invite.{invitation.pk.hex}"}))
        with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http", side_effect=transport) as http:
            state = consent.process_consent_invitation(invitation.pk)
        invitation.refresh_from_db()
        return state, http

    def reply(self, invitation, choice="accept", **changes):
        return self.consent_source(self.customer, text="Button response",
            payload=getattr(invitation, choice + "_payload"), **changes)

    def assert_no_permission(self, *, order=None):
        order = order or self.order
        self.assertFalse(consent.has_post_purchase_consent(self.customer, order.pk))
        self.assertIsNone(consent.post_purchase_consent_source(self.customer, order.pk))

    def test_paid_queue_is_idempotent_and_is_not_a_marketing_permission(self):
        invitation = self.queue()
        self.assertEqual(self.queue().pk, invitation.pk)
        self.assertEqual(IgMarketingConsentInvitation.objects.count(), 1)
        self.assertEqual(invitation.state, "pending")
        self.assertEqual(invitation.source_message_id, self.source.pk)
        self.assertEqual(invitation.provider_basis, "standard_window")
        self.assertEqual(invitation.native_grant, "unverified")
        self.assert_no_permission()
        self.assertEqual(consent.business_consent_projection(self.customer, self.order.pk)["response"]["status"], "unknown")

    def test_unpaid_or_foreign_assignment_cannot_queue(self):
        self.order.payment_status = "unpaid"
        self.order.save(update_fields=["payment_status"])
        self.assertIsNone(consent.queue_post_purchase_consent(self.customer, self.order, self.assignment))
        self.order.payment_status = "paid"
        self.order.save(update_fields=["payment_status"])
        other = IgClient.objects.create(igsid="consent-other-owner")
        self.assertIsNone(consent.queue_post_purchase_consent(other, self.order, self.assignment))
        self.assertEqual(IgMarketingConsentInvitation.objects.count(), 0)

    def test_fabricated_client_window_does_not_replace_real_admitted_source(self):
        for changes in ({"source": "manual_refresh"}, {"provider_namespace": "instagram_login:foreign"},
                        {"mid": ""}, {"provider_created_at": self.now - timedelta(days=2)}):
            with self.subTest(changes=changes):
                original = {key: getattr(self.source, key) for key in changes}
                for key, value in changes.items():
                    setattr(self.source, key, value)
                self.source.save(update_fields=list(changes))
                self.customer.last_user_message_at = self.now
                self.customer.save(update_fields=["last_user_message_at"])
                self.assertIsNone(consent.queue_post_purchase_consent(self.customer, self.order, self.assignment, now=self.now))
                for key, value in original.items():
                    setattr(self.source, key, value)
                self.source.save(update_fields=list(original))
        self.assert_no_permission()

    def test_real_meta_quick_reply_receipt_and_no_second_physical_attempt(self):
        invitation = self.queue()
        state, http = self.deliver(invitation)
        self.assertEqual(state, "sent")
        http.assert_called_once()
        payload = json.loads(http.call_args.kwargs["data"].decode())
        self.assertEqual(payload["recipient"]["id"], self.customer.igsid)
        self.assertEqual(payload["message"]["text"], invitation.message_snapshot)
        self.assertEqual([item["payload"] for item in payload["message"]["quick_replies"]],
            [invitation.accept_payload])
        self.assertNotIn("tag", payload)
        self.assertEqual(invitation.provider_message_id, f"mid.invite.{invitation.pk.hex}")
        self.assertIsNotNone(invitation.sent_at)
        with patch("management.services.instagram_bot._provider_http") as again:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "sent")
        again.assert_not_called()
        self.assert_no_permission()

    def test_actual_signed_answer_records_source_and_duplicate_accept_is_idempotent(self):
        invitation = self.queue()
        self.assertEqual(self.deliver(invitation)[0], "sent")
        source = self.reply(invitation)
        for _ in range(2):
            outcome = consent.handle_consent_reply(source)
            self.assertEqual(outcome.reason, "business_consent_accept")
            self.assertEqual(outcome.quick_replies[0].payload, invitation.revoke_payload)
        repeat = self.reply(invitation)
        self.assertEqual(consent.handle_consent_reply(repeat).reason, "business_consent_accept")
        self.assertEqual(IgMarketingConsentAnswer.objects.count(), 1)
        answer = IgMarketingConsentAnswer.objects.get()
        self.assertEqual((answer.source_message_id, answer.source_mid, answer.provider_namespace),
            (source.pk, source.mid, source.provider_namespace))
        projection = consent.business_consent_projection(self.customer, self.order.pk)
        self.assertEqual(projection["response"]["status"], "accepted")
        proof = consent.post_purchase_consent_source(self.customer, self.order.pk)
        self.assertEqual(proof["purpose"], consent.PURPOSE)
        self.assertEqual(proof["source_message_id"], source.pk)
        self.assertEqual(proof["native_grant"], "unverified")

    def test_bare_yes_unsigned_or_altered_button_never_grants_permission(self):
        invitation = self.queue()
        self.deliver(invitation)
        for payload in ("", "accept", "twc-consent:1:unsigned", invitation.accept_payload + "x"):
            source = self.consent_source(self.customer, text="Yes", payload=payload)
            outcome = consent.handle_consent_reply(source)
            self.assertTrue(outcome is None or outcome.reason == "business_consent_invalid")
        self.assertEqual(IgMarketingConsentAnswer.objects.count(), 0)
        self.assert_no_permission()

    def test_ignoring_and_unrelated_positive_negative_or_empty_text_never_answers(self):
        invitation = self.queue()
        self.assertEqual(self.deliver(invitation)[0], "sent")
        self.assert_no_permission()  # Merely receiving or ignoring the card.
        for text in ("Так", "Да", "Yes", "Так, хочу", "Yes, keep me updated", "Ні",
                "Не зараз", "No", "Where is my order?", "Дякую!", "", "   "):
            with self.subTest(text=text):
                source = self.consent_source(self.customer, text=text)
                self.assertIsNone(consent.handle_consent_reply(source))
                self.assert_no_permission()
        self.assertEqual(IgMarketingConsentAnswer.objects.count(), 0)

    def test_frozen_legacy_pending_body_and_payloads_render_one_affirmative_without_rewrite(self):
        old_copy = ("Want details of our story bonus after your order arrives, plus TwoComms news and offers? It is optional, and you can opt out anytime.",
            "Yes, keep me updated", "Not now", "Opt out")
        with patch.dict(consent.COPY, {"en": old_copy}):
            invitation = self.queue()
        invitation.refresh_from_db()
        snapshot = consent._snapshot(invitation)
        self.assertEqual(invitation.message_snapshot, old_copy[0])
        self.assertTrue(consent._invitation_valid(invitation))
        rendered = consent.consent_quick_reply_message(invitation)
        self.assertEqual(rendered.text, old_copy[0])
        self.assertEqual(len(rendered.quick_replies), 1)
        self.assertEqual(rendered.quick_replies[0].payload, invitation.accept_payload)
        self.assertEqual(self.deliver(invitation)[0], "sent")
        invitation.refresh_from_db()
        self.assertEqual(consent._snapshot(invitation), snapshot)
        self.assertTrue(consent._invitation_valid(invitation))
        # An older delivered card's signed decline is still a real refusal.
        outcome = consent.handle_consent_reply(self.reply(invitation, "decline"))
        self.assertEqual(outcome.reason, "business_consent_decline")
        self.assertEqual(outcome.quick_replies, ())
        self.assert_no_permission()

    def test_answer_must_be_owned_admitted_sender_mid_namespace_and_time(self):
        invitation = self.queue()
        self.deliver(invitation)
        foreign = IgClient.objects.create(igsid="consent-foreign-reply")
        for changes in ({"client": foreign}, {"sender_id": foreign.igsid}, {"role": "model"},
                {"source": "manual_refresh"}, {"provider_namespace": "instagram_login:2"}, {"mid": ""},
                {"provider_created_at": invitation.issued_at - timedelta(seconds=1)},
                {"provider_created_at": timezone.now() + timedelta(hours=1)}):
            with self.subTest(changes=changes):
                source = self.reply(invitation, **changes)
                self.assertEqual(consent.handle_consent_reply(source).reason, "business_consent_invalid")
        self.assertEqual(IgMarketingConsentAnswer.objects.count(), 0)
        self.assert_no_permission()

    def test_signed_answer_before_real_delivery_receipt_is_not_consent(self):
        invitation = self.queue()
        self.assertEqual(consent.handle_consent_reply(self.reply(invitation)).reason, "business_consent_invalid")
        self.assertEqual(IgMarketingConsentAnswer.objects.count(), 0)
        self.assert_no_permission()

    def test_decline_and_duplicate_decline_are_terminal_without_global_optout(self):
        invitation = self.queue()
        self.deliver(invitation)
        source = self.reply(invitation, "decline")
        for _ in range(2):
            self.assertEqual(consent.handle_consent_reply(source).reason, "business_consent_decline")
        self.assertEqual(consent.handle_consent_reply(self.reply(invitation)).reason, "business_consent_decline")
        self.assertEqual(IgMarketingConsentAnswer.objects.count(), 1)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.opted_out_at)
        self.assertEqual(consent.business_consent_projection(self.customer, self.order.pk)["response"]["status"], "declined")
        self.assert_no_permission()

    def test_revoke_and_duplicate_revoke_keep_append_only_accept_history(self):
        invitation, _ = self.grant_business_consent(self.customer, self.order, self.assignment)
        source = self.reply(invitation, "revoke")
        for _ in range(2):
            self.assertEqual(consent.handle_consent_reply(source).reason, "business_consent_revoke")
        self.assertEqual(consent.handle_consent_reply(self.reply(invitation)).reason, "business_consent_revoke")
        self.assertEqual(list(invitation.answers.order_by("pk").values_list("choice", flat=True)), ["accept", "revoke"])
        self.assertEqual(consent.business_consent_projection(self.customer, self.order.pk)["response"]["status"], "revoked")
        self.assert_no_permission()

    def test_expired_invitation_never_accepts_or_sends(self):
        invitation = self.queue()
        self.deliver(invitation)
        source = self.reply(invitation)
        other, order, assignment, _source = self.native_purchase()
        pending = consent.queue_post_purchase_consent(other, order, assignment, now=self.now)
        self.assertIsNotNone(pending)
        with patch("django.utils.timezone.now", return_value=invitation.expires_at), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.handle_consent_reply(source).reason, "business_consent_invalid")
            self.assertEqual(consent.business_consent_projection(self.customer, self.order.pk)["permission"]["status"], "expired")
            self.assertEqual(consent.process_consent_invitation(pending.pk), "failed")
        http.assert_not_called()
        self.assertEqual(IgMarketingConsentAnswer.objects.count(), 0)

    def test_changed_payment_source_or_provider_namespace_blocks_pending_socket(self):
        for change in ("payment", "source", "namespace"):
            with self.subTest(change=change):
                customer, order, assignment, source = self.native_purchase()
                invitation = consent.queue_post_purchase_consent(customer, order, assignment, now=self.now)
                self.assertIsNotNone(invitation)
                if change == "payment":
                    order.payment_status = "unpaid"
                    order.save(update_fields=["payment_status"])
                elif change == "source":
                    source.text = "Corrected source after invitation capture"
                    source.save(update_fields=["text"])
                else:
                    self.settings_row.ig_user_id = "2"
                    self.settings_row.save(update_fields=["ig_user_id"])
                with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                        "management.services.instagram_bot._provider_http") as http:
                    self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
                http.assert_not_called()
                self.assertFalse(consent.has_post_purchase_consent(customer, order.pk))
                self.settings_row.ig_user_id = "1"
                self.settings_row.save(update_fields=["ig_user_id"])

    def test_reset_and_reassignment_invalidate_old_invitation_scope(self):
        from management.services.ig_funnel_reset import reset_funnel
        invitation, _ = self.grant_business_consent(self.customer, self.order, self.assignment)
        result = reset_funnel(client_id=self.customer.pk, actor=self.actor)
        self.assertTrue(result["ok"], result)
        self.assertEqual(consent.handle_consent_reply(self.reply(invitation)).reason, "business_consent_invalid")
        self.assert_no_permission()
        second, order, assignment, _source = self.native_purchase()
        invitation, _ = self.grant_business_consent(second, order, assignment)
        unlink_order_from_client(order, client=second, actor=self.actor, expected_version=assignment.version,
            reason_code="manager_correction", reason="Wrong customer")
        link_order_to_client(order, client=self.customer, actor=self.actor)
        self.assertFalse(consent.has_post_purchase_consent(second, order.pk))
        source = self.consent_source(second, payload=invitation.accept_payload)
        self.assertEqual(consent.handle_consent_reply(source).reason, "business_consent_invalid")

    def test_optout_pause_and_takeover_block_without_creating_permission(self):
        for field, value in (("opted_out_at", timezone.now()), ("bot_paused", True), ("manager_takeover", True)):
            with self.subTest(field=field):
                setattr(self.customer, field, value)
                self.customer.save(update_fields=[field])
                self.assertIsNone(consent.queue_post_purchase_consent(self.customer, self.order, self.assignment))
                self.assert_no_permission()
                setattr(self.customer, field, None if field == "opted_out_at" else False)
                self.customer.save(update_fields=[field])
        invitation, _ = self.grant_business_consent(self.customer, self.order, self.assignment)
        self.customer.opted_out_at = timezone.now()
        self.customer.save(update_fields=["opted_out_at"])
        self.assertEqual(consent.handle_consent_reply(self.reply(invitation)).reason, "business_consent_invalid")
        self.assert_no_permission()

    def test_revocation_between_queue_and_provider_boundary_blocks_socket(self):
        invitation = self.queue()
        real_adapter = consent.send_quick_replies
        def revoke_before_boundary(*args, **kwargs):
            self.customer.opted_out_at = timezone.now()
            self.customer.save(update_fields=["opted_out_at"])
            return real_adapter(*args, **kwargs)
        with patch.object(consent, "send_quick_replies", side_effect=revoke_before_boundary), patch(
                "management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
        http.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual(invitation.provider_message_id, "")
        self.assert_no_permission()

    def test_new_invitation_only_queues_before_shipment_or_any_parcel_history(self):
        from management.ig_bot_models import IgOrderShipment
        cases = (("status", "ship"), ("status", "done"), ("status", "cancelled"),
            ("tracking_number", "20450000000001"), ("nova_poshta_document_ref", "document-proof"),
            ("shipment_status", "Отримано"), ("tracking_status_code", 9),
            ("tracking_terminal_at", timezone.now()), ("journal", "20450000000002"))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                customer, order, assignment, _source = self.native_purchase()
                if field == "journal":
                    IgOrderShipment.objects.create(order=order, tracking_number=value,
                        direction=IgOrderShipment.Direction.OUTBOUND, purpose=IgOrderShipment.Purpose.INITIAL)
                else:
                    setattr(order, field, value)
                    order.save(update_fields=[field])
                self.assertIsNone(consent.queue_post_purchase_consent(customer, order, assignment, now=self.now))
                self.assertFalse(IgMarketingConsentInvitation.objects.filter(order=order).exists())
        self.order.status = "prep"
        self.order.save(update_fields=["status"])
        self.assertIsNotNone(consent.queue_post_purchase_consent(self.customer, self.order, self.assignment, now=self.now))

    def test_shipment_after_queue_fails_known_unsent_without_provider_receipt(self):
        invitation = self.queue()
        self.order.status = "ship"
        self.order.save(update_fields=["status"])
        state, http = self.deliver(invitation)
        self.assertEqual(state, "failed")
        http.assert_not_called()
        self.assertEqual((invitation.last_error, invitation.attempts, invitation.provider_message_id),
            ("purchase_already_shipped", 0, ""))
        self.assert_no_permission()

    def test_already_shipped_pending_scope_is_terminal_even_after_reply_window_closes(self):
        invitation = self.queue()
        self.order.status = "done"
        self.order.save(update_fields=["status"])
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(days=1)), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
        http.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual((invitation.last_error, invitation.attempts, invitation.provider_message_id),
            ("purchase_already_shipped", 0, ""))
        self.assert_no_permission()

    def test_shipment_between_claim_and_http_boundary_blocks_real_socket(self):
        invitation = self.queue()
        real_adapter = consent.send_quick_replies
        def ship_before_boundary(*args, **kwargs):
            self.order.tracking_number = "20450000000003"
            self.order.save(update_fields=["tracking_number"])
            return real_adapter(*args, **kwargs)
        with patch.object(consent, "send_quick_replies", side_effect=ship_before_boundary), patch(
                "management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
        http.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual((invitation.last_error, invitation.provider_message_id), ("purchase_already_shipped", ""))
        self.assert_no_permission()

    def test_signed_historical_accept_and_separate_revoke_remain_valid_after_shipment(self):
        invitation = self.queue()
        self.assertEqual(self.deliver(invitation)[0], "sent")
        self.order.status = "done"
        self.order.save(update_fields=["status"])
        accepted = consent.handle_consent_reply(self.reply(invitation))
        self.assertEqual(accepted.reason, "business_consent_accept")
        self.assertEqual(len(accepted.quick_replies), 1)
        self.assertEqual(accepted.quick_replies[0].payload, invitation.revoke_payload)
        self.assertTrue(consent.has_post_purchase_consent(self.customer, self.order.pk))
        revoked = consent.handle_consent_reply(self.reply(invitation, "revoke"))
        self.assertEqual(revoked.reason, "business_consent_revoke")
        self.assertEqual(revoked.quick_replies, ())
        self.assertEqual(list(invitation.answers.order_by("pk").values_list("choice", flat=True)), ["accept", "revoke"])
        self.assert_no_permission()

    def test_current_owned_complaint_cannot_become_an_invitation_source(self):
        self.consent_source(self.customer, text="My order arrived damaged.", at=self.now, status="pending")
        self.assertIsNone(consent.queue_post_purchase_consent(self.customer, self.order, self.assignment, now=self.now))
        self.assertFalse(IgMarketingConsentInvitation.objects.exists())
        self.assert_no_permission()

    def test_current_complaint_holds_pending_without_claim_or_physical_attempt(self):
        invitation = self.queue()
        self.consent_source(self.customer, text="My order arrived damaged.")
        state, http = self.deliver(invitation)
        self.assertEqual(state, "service_hold")
        http.assert_not_called()
        self.assertEqual((invitation.state, invitation.last_error, invitation.attempts),
            ("pending", "service_complaint_open", 0))
        self.assertEqual(invitation.provider_message_id, "")
        self.assertIsNone(invitation.provider_started_at)
        self.assert_no_permission()

    def test_complaint_arriving_at_http_boundary_fails_known_unsent_claim_without_replay(self):
        invitation = self.queue()
        real_adapter = consent.send_quick_replies
        def complain_before_boundary(*args, **kwargs):
            self.consent_source(self.customer, text="My order arrived damaged.", status="pending")
            return real_adapter(*args, **kwargs)
        with patch.object(consent, "send_quick_replies", side_effect=complain_before_boundary), patch(
                "management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
        http.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual((invitation.state, invitation.last_error, invitation.attempts, invitation.lease_token),
            ("failed", "service_complaint_open", 1, ""))
        self.assertIsNone(invitation.lease_until)
        self.assertIsNotNone(invitation.provider_started_at)
        self.assertEqual((invitation.provider_message_id, invitation.receipt_hmac), ("", ""))
        self.assert_no_permission()

    def test_cancelled_boundary_with_returned_mid_is_unknown_and_never_reopened(self):
        from management.services.ig_message_templates import TemplateDelivery
        invitation = self.queue()
        real_adapter = consent.send_quick_replies
        def contradictory_receipt(*args, **kwargs):
            self.consent_source(self.customer, text="My order arrived damaged.", status="pending")
            actual = real_adapter(*args, **kwargs)
            self.assertEqual(actual.kind, "cancelled")
            return TemplateDelivery(False, "cancelled", "permission_changed",
                provider_message_id="mid.consent.contradictory-partial")
        with patch.object(consent, "send_quick_replies", side_effect=contradictory_receipt), patch(
                "management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "unknown")
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "unknown")
        http.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual((invitation.state, invitation.attempts, invitation.provider_message_id),
            ("unknown", 1, "mid.consent.contradictory-partial"))
        self.assertIsNotNone(invitation.provider_started_at)
        self.assertIsNone(invitation.sent_at)
        self.assertEqual(invitation.receipt_hmac, "")
        self.assert_no_permission()

    def test_optional_queue_waits_for_captured_user_processing_to_finish(self):
        for status in ("pending", "processing"):
            with self.subTest(status=status):
                self.source.status = status
                self.source.save(update_fields=["status"])
                self.assertIsNone(consent.queue_post_purchase_consent(self.customer, self.order, self.assignment, now=self.now))
                self.assertFalse(IgMarketingConsentInvitation.objects.exists())
        self.source.status = "done"
        self.source.save(update_fields=["status"])
        self.assertIsNotNone(consent.queue_post_purchase_consent(self.customer, self.order, self.assignment, now=self.now))

    def test_source_reprocessing_before_claim_waits_without_physical_attempt(self):
        invitation = self.queue()
        self.source.status = "processing"
        self.source.save(update_fields=["status"])
        state, http = self.deliver(invitation)
        self.assertEqual(state, "waiting_source")
        http.assert_not_called()
        self.assertEqual((invitation.state, invitation.last_error, invitation.attempts),
            ("pending", "source_reply_pending", 0))
        self.source.status = "done"
        self.source.save(update_fields=["status"])
        self.assertEqual(self.deliver(invitation)[0], "sent")
        self.assert_no_permission()

    def test_source_reprocessing_at_boundary_fails_known_unsent_claim_without_replay(self):
        invitation = self.queue()
        real_adapter = consent.send_quick_replies
        def processing_before_boundary(*args, **kwargs):
            self.source.status = "processing"
            self.source.save(update_fields=["status"])
            return real_adapter(*args, **kwargs)
        with patch.object(consent, "send_quick_replies", side_effect=processing_before_boundary), patch(
                "management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
        http.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual((invitation.state, invitation.last_error, invitation.attempts),
            ("failed", "source_reply_pending", 1))
        self.assertIsNotNone(invitation.provider_started_at)
        self.assertEqual(invitation.provider_message_id, "")
        self.assert_no_permission()

    def test_service_hold_cooldown_allows_next_customer_in_bounded_drain(self):
        invitation = self.queue()
        customer, order, assignment, _source = self.native_purchase()
        eligible = consent.queue_post_purchase_consent(customer, order, assignment)
        self.assertIsNotNone(eligible)
        self.consent_source(self.customer, text="My order arrived damaged.")
        with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http",
                return_value=(200, '{"message_id":"mid.consent.next-customer"}')) as http:
            first = consent.reconcile_consent_invitations(limit=1)
            self.assertEqual((first["considered"], first["states"]), (1, {"service_hold": 1}))
            http.assert_not_called()
            second = consent.reconcile_consent_invitations(limit=1)
            self.assertEqual((second["considered"], second["states"]), (1, {"sent": 1}))
            self.assertEqual(consent.reconcile_consent_invitations(limit=1)["considered"], 0)
        http.assert_called_once()
        invitation.refresh_from_db()
        eligible.refresh_from_db()
        self.assertEqual((invitation.state, invitation.attempts, invitation.provider_message_id), ("pending", 0, ""))
        self.assertEqual((eligible.state, eligible.provider_message_id), ("sent", "mid.consent.next-customer"))
        with patch("django.utils.timezone.now", return_value=invitation.updated_at + consent.SERVICE_HOLD_BACKOFF), patch(
                "management.services.instagram_bot._provider_http") as retry:
            third = consent.reconcile_consent_invitations(limit=1)
        retry.assert_not_called()
        self.assertEqual((third["considered"], third["states"]), (1, {"service_hold": 1}))
        self.assert_no_permission()

    def test_closed_captured_source_window_cannot_starve_current_customer(self):
        oldest = self.queue()
        oldest.refresh_from_db()
        old_snapshot = consent._snapshot(oldest)
        old_receipt = (oldest.state, oldest.attempts, oldest.provider_message_id,
            oldest.receipt_hmac, oldest.sent_at, oldest.provider_started_at, oldest.updated_at)
        future = self.now + timedelta(days=1)
        with patch("django.utils.timezone.now", return_value=future):
            customer, order, assignment, _stale_source = self.native_purchase()
            current_source = self.consent_source(customer, at=future)
            customer.last_user_message_at = current_source.provider_created_at
            customer.save(update_fields=["last_user_message_at"])
            eligible = consent.queue_post_purchase_consent(customer, order, assignment, now=future)
            self.assertIsNotNone(eligible)
            # A recent cache timestamp cannot revive the captured old source.
            self.customer.last_user_message_at = future
            self.customer.save(update_fields=["last_user_message_at"])
            with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                    "management.services.instagram_bot._provider_http",
                    return_value=(200, '{"message_id":"mid.consent.current-window"}')) as http:
                result = consent.reconcile_consent_invitations(limit=1)
                self.assertEqual((result["considered"], result["states"]), (1, {"sent": 1}))
                self.assertEqual(consent.reconcile_consent_invitations(limit=1)["considered"], 0)
        http.assert_called_once()
        payload = json.loads(http.call_args.kwargs["data"].decode())
        self.assertEqual(payload["recipient"]["id"], customer.igsid)
        eligible.refresh_from_db()
        oldest.refresh_from_db()
        self.assertEqual((eligible.state, eligible.provider_message_id), ("sent", "mid.consent.current-window"))
        self.assertEqual(consent._snapshot(oldest), old_snapshot)
        self.assertEqual((oldest.state, oldest.attempts, oldest.provider_message_id,
            oldest.receipt_hmac, oldest.sent_at, oldest.provider_started_at, oldest.updated_at), old_receipt)
        self.assert_no_permission()

    def test_abandoned_processing_is_reaped_even_when_captured_window_is_closed(self):
        class ProcessCrash(BaseException):
            pass
        invitation = self.queue()
        with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http", side_effect=ProcessCrash) as first_http:
            with self.assertRaises(ProcessCrash):
                consent.process_consent_invitation(invitation.pk)
        first_http.assert_called_once()
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(days=1)), patch(
                "management.services.instagram_bot._provider_http") as again:
            result = consent.reconcile_consent_invitations(limit=1)
        again.assert_not_called()
        self.assertEqual((result["considered"], result["states"]), (1, {"unknown": 1}))
        invitation.refresh_from_db()
        self.assertEqual((invitation.state, invitation.last_error), ("unknown", "abandoned_provider_attempt"))
        self.assert_no_permission()

    def test_unrelated_new_user_supersedes_captured_source_before_claim(self):
        for source_time, status in ((timezone.now(), "pending"),
                (self.source.provider_created_at - timedelta(seconds=1), "processing")):
            with self.subTest(source_time=source_time, status=status):
                customer, order, assignment, _source = self.native_purchase()
                invitation = consent.queue_post_purchase_consent(customer, order, assignment, now=self.now)
                self.assertIsNotNone(invitation)
                self.consent_source(customer, text="Could you change the delivery address?", at=source_time, status=status)
                state, http = self.deliver(invitation)
                self.assertEqual(state, "failed")
                http.assert_not_called()
                self.assertEqual((invitation.last_error, invitation.attempts, invitation.provider_message_id),
                    ("invitation_source_superseded", 0, ""))
                self.assertFalse(consent.has_post_purchase_consent(customer, order.pk))

    def test_unrelated_new_user_at_boundary_supersedes_old_card_without_socket(self):
        invitation = self.queue()
        real_adapter = consent.send_quick_replies
        def reply_before_boundary(*args, **kwargs):
            self.consent_source(self.customer, text="Could you change the delivery address?", status="pending")
            return real_adapter(*args, **kwargs)
        with patch.object(consent, "send_quick_replies", side_effect=reply_before_boundary), patch(
                "management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http") as http:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "failed")
        http.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual((invitation.last_error, invitation.provider_message_id), ("invitation_source_superseded", ""))
        self.assert_no_permission()

    def test_missing_mid_unknown_has_no_replay_and_no_answer_permission(self):
        invitation = self.queue()
        state, http = self.deliver(invitation, response=(200, "{}"))
        self.assertEqual(state, "unknown")
        http.assert_called_once()
        with patch("management.services.instagram_bot._provider_http") as again:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "unknown")
        again.assert_not_called()
        self.assertEqual(consent.handle_consent_reply(self.reply(invitation)).reason, "business_consent_invalid")
        self.assert_no_permission()

    def test_abandoned_attempt_becomes_unknown_after_lease_without_replay(self):
        class ProcessCrash(BaseException):
            pass
        invitation = self.queue()
        with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http", side_effect=ProcessCrash) as http:
            with self.assertRaises(ProcessCrash):
                consent.process_consent_invitation(invitation.pk)
        http.assert_called_once()
        invitation.refresh_from_db()
        self.assertEqual(invitation.state, "processing")
        with patch("django.utils.timezone.now", return_value=invitation.lease_until + timedelta(seconds=1)), patch(
                "management.services.instagram_bot._provider_http") as again:
            self.assertEqual(consent.process_consent_invitation(invitation.pk), "unknown")
        again.assert_not_called()
        invitation.refresh_from_db()
        self.assertEqual(invitation.last_error, "abandoned_provider_attempt")
        self.assert_no_permission()

    def test_actual_source_language_overrides_stale_profile_in_all_three_locales(self):
        for locale, text, profile in (("uk", "Будь ласка, відповідайте українською. Дякую за замовлення.", "ru"),
                ("ru", "Пожалуйста, отвечайте на русском. Спасибо за заказ.", "uk"),
                ("en", "Please reply in English. Thank you for my order.", "ru")):
            with self.subTest(locale=locale):
                customer, order, assignment, source = self.native_purchase(text=text, profile=profile)
                invitation = consent.queue_post_purchase_consent(customer, order, assignment, now=self.now)
                self.assertIsNotNone(invitation)
                self.assertEqual(invitation.locale, locale)
                self.assertEqual(invitation.source_message_id, source.pk)
                self.assertEqual(invitation.message_snapshot, consent.COPY[locale][0])
                self.assertIn("\n\n", invitation.message_snapshot)
                self.assertNotIn("%", invitation.message_snapshot)
                state, http = self.deliver(invitation)
                self.assertEqual(state, "sent")
                message = json.loads(http.call_args.kwargs["data"].decode())["message"]
                self.assertEqual(message["text"], invitation.message_snapshot)
                buttons = message["quick_replies"]
                self.assertEqual([(button["title"], button["payload"]) for button in buttons],
                    [(consent.COPY[locale][1], invitation.accept_payload)])
                self.assertLessEqual(len(buttons[0]["title"]), 20)
                reply = self.consent_source(customer, payload=invitation.accept_payload)
                outcome = consent.handle_consent_reply(reply)
                self.assertEqual(outcome.reply_text, consent.ACK[locale]["accept"])
                self.assertEqual(len(outcome.quick_replies), 1)
                self.assertEqual(outcome.quick_replies[0].title, consent.COPY[locale][3])
                self.assertEqual(outcome.quick_replies[0].payload, invitation.revoke_payload)

    def test_order_permission_never_shares_another_order_or_native_future_basis(self):
        invitation, answer = self.grant_business_consent(self.customer, self.order, self.assignment)
        other = Order.objects.create(order_number="CONSENT-ANOTHER", full_name="Website buyer",
            phone=self.order.phone, total_sum="400.00", payment_status="paid")
        link_order_to_client(other, client=self.customer, actor=self.actor)
        self.assert_no_permission(order=other)
        proof = consent.post_purchase_consent_source(self.customer, self.order.pk)
        self.assertEqual(proof["order_id"], self.order.pk)
        self.assertEqual(proof["purpose"], "post_purchase_marketing")
        self.assertEqual(proof["native_grant"], "unverified")
        from management.services.ig_outgoing_policy import (
            BLOCK, ConsentScope, OutgoingRequest, VERIFIED_CONTRACT_VERSION, decide_outgoing,
        )
        request = OutgoingRequest(platform_contract_version=VERIFIED_CONTRACT_VERSION,
            event_kind="lifecycle.payment_verified", message_purpose="transactional", channel_app_type="instagram_login",
            latest_user_provider_ts=answer.provider_created_at,
            consent_scope=ConsentScope(topic=proof["purpose"], granted_at=answer.provider_created_at,
                expires_at=invitation.expires_at, evidence_message_id=answer.pk), consent_topic_required=proof["purpose"])
        decision = decide_outgoing(request, now=answer.provider_created_at + timedelta(days=2))
        self.assertEqual(decision.decision, BLOCK)
        self.assertNotEqual(decision.policy_basis, "proven_consent")

    def test_ledger_snapshots_and_answers_cannot_be_rewritten(self):
        invitation, _ = self.grant_business_consent(self.customer, self.order, self.assignment)
        with self.assertRaises(ValueError):
            IgMarketingConsentInvitation.objects.filter(pk=invitation.pk).update(purpose="restock")
        invitation.message_snapshot = "Altered customer consent wording"
        with self.assertRaises(ValueError):
            invitation.save(update_fields=["message_snapshot"])
        with self.assertRaises(ValueError):
            invitation.answers.update(choice="decline")
        self.assertTrue(consent.has_post_purchase_consent(self.customer, self.order.pk))

    def test_assignment_producer_queues_paid_consent_before_canonical_lifecycle_early_skip(self):
        from management.models import IgCheckoutProposal, IgDeal, IgOrderAttribution
        from management.services.ig_commercial_episodes import ensure_episode_for_deal
        from management.services.ig_order_fulfillment import ensure_assignment_events
        from orders.models import PaymentAttempt

        customer, order, assignment, _source = self.native_purchase(assignment_source="provider_auto")
        deal = IgDeal.objects.create(client=customer, order=order, status="quoted", amount=order.total_sum)
        proposal = IgCheckoutProposal.objects.create_current(deal=deal,
            commercial_episode=ensure_episode_for_deal(deal), catalog_total=order.total_sum,
            quoted_total=order.total_sum, requested_payment_amount=order.total_sum,
            items_digest=hashlib.sha256(b"actual-canonical-consent-purchase").hexdigest())
        attempt = PaymentAttempt.objects.create(order=order, fingerprint=f"{order.pk:064x}",
            full_name=order.full_name, phone=order.phone, pay_type="online_full", status="converted",
            gross_amount=order.total_sum, payable_amount=order.total_sum, payment_amount=order.total_sum)
        proposal.payment_attempt = attempt
        proposal.save(update_fields=["payment_attempt", "updated_at"])
        IgOrderAttribution.objects.create(order=order, client=customer, deal=deal,
            creation_mode="provider_auto", payment_source="provider_attempt")
        self.assertEqual(ensure_assignment_events(assignment), [])
        invitation = IgMarketingConsentInvitation.objects.get(client=customer, order=order)
        self.assertEqual(invitation.assignment_id, assignment.pk)
        self.assertEqual(invitation.state, "pending")
        self.assertFalse(consent.has_post_purchase_consent(customer, order.pk))
        self.assertEqual(ensure_assignment_events(assignment), [])
        self.assertEqual(IgMarketingConsentInvitation.objects.filter(client=customer, order=order).count(), 1)

    def test_legacy_dispatch_routes_signed_reply_to_one_answer_and_revoke_button(self):
        from management.services.ig_postback_router import dispatch_postback

        invitation = self.queue()
        self.assertEqual(self.deliver(invitation)[0], "sent")
        source = self.reply(invitation)
        first = dispatch_postback(source)
        second = dispatch_postback(source)
        self.assertEqual(first, second)
        self.assertEqual(first.reason, "business_consent_accept")
        self.assertEqual(first.quick_replies[0].payload, invitation.revoke_payload)
        self.assertEqual(IgMarketingConsentAnswer.objects.filter(invitation=invitation).count(), 1)
        self.assertTrue(consent.has_post_purchase_consent(self.customer, self.order.pk))

    def test_full_native_lifecycle_backlog_preserves_bounded_consent_send_slot(self):
        from management.models import IgUgcRewardLifecycleJob
        from management.services.ig_follow_reconcile import reconcile_follow_intelligence_once

        invitation = self.queue()
        for index in range(10):
            IgUgcRewardLifecycleJob.objects.create(client_id=self.customer.pk, order_id=self.order.pk,
                source="order_truth", due_at=self.now - timedelta(minutes=index + 1))
        self.assertEqual(IgUgcRewardLifecycleJob.objects.count(), 10)
        with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                "management.services.instagram_bot._provider_http",
                return_value=(200, '{"message_id":"mid.consent.fair-batch"}')) as http:
            counts = reconcile_follow_intelligence_once(limit=10)
        http.assert_called_once()
        invitation.refresh_from_db()
        self.assertEqual((invitation.state, invitation.provider_message_id), ("sent", "mid.consent.fair-batch"))
        self.assertEqual(counts["consent_selected"], 1)
        self.assertEqual(counts["consent_sent"], 1)
        self.assertLessEqual(counts["ugc_lifecycle_selected"], 8)
        selected = sum(counts.get(key, 0) for key in ("ugc_lifecycle_selected", "consent_selected",
            "payment_selected", "follow_selected", "ugc_selected", "ugc_media_selected"))
        self.assertLessEqual(selected, 10)
        self.assertEqual(IgUgcRewardLifecycleJob.objects.count(), 10 - counts["ugc_lifecycle_selected"])
        with patch("management.services.instagram_bot._provider_http") as again:
            reconcile_follow_intelligence_once(limit=10)
        again.assert_not_called()
        self.assert_no_permission()

    def test_privacy_purge_requires_exact_persisted_fence_and_preserves_foreign_owner(self):
        from management.services.ig_marketing_consent_privacy import purge_marketing_consent_data

        own, own_answer = self.grant_business_consent(self.customer, self.order, self.assignment)
        foreign, order, assignment, _source = self.native_purchase()
        other, other_answer = self.grant_business_consent(foreign, order, assignment)
        with transaction.atomic():
            with self.assertRaises(ValueError):
                purge_marketing_consent_data([self.customer.pk])
        self.assertTrue(IgMarketingConsentInvitation.objects.filter(pk=own.pk).exists())
        self.assertTrue(IgMarketingConsentAnswer.objects.filter(source_message=own_answer).exists())
        self.customer.privacy_erasure_started_at = timezone.now()
        self.customer.save(update_fields=["privacy_erasure_started_at"])
        with transaction.atomic():
            with self.assertRaises(ValueError):
                purge_marketing_consent_data([self.customer.pk, foreign.pk])
            purge_marketing_consent_data([self.customer.pk])
        self.assertFalse(IgMarketingConsentInvitation.objects.filter(pk=own.pk).exists())
        self.assertFalse(IgMarketingConsentAnswer.objects.filter(source_message=own_answer).exists())
        self.assertTrue(IgMarketingConsentInvitation.objects.filter(pk=other.pk).exists())
        self.assertTrue(IgMarketingConsentAnswer.objects.filter(source_message=other_answer).exists())
        self.assertTrue(consent.has_post_purchase_consent(foreign, order.pk))


@override_settings(IG_POST_PURCHASE_BUSINESS_CONSENT_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class MarketingConsentModernRevisionTests(_MarketingConsentFixture, TransactionTestCase):
    """Actual modern owner APIs reject callers inside fixture transactions."""
    reset_sequences = True
    setUp = MarketingConsentAuthorityTests.setUp
    native_purchase = MarketingConsentAuthorityTests.native_purchase
    queue = MarketingConsentAuthorityTests.queue
    deliver = MarketingConsentAuthorityTests.deliver
    reply = MarketingConsentAuthorityTests.reply

    def test_signed_accept_executes_actual_revision_receipt_and_deterministic_revoke_outbox(self):
        from management.models import BotPolicyPublication, IgCustomerTurn, IgSourceActionReceipt, IgTurnMessage
        from management.services.ig_revision_live import _execute_deterministic_input
        from management.services.ig_revision_outbox import PublicationBinding
        from management.services.ig_revision_postbacks import apply_revision_postback
        from management.services.ig_turn_revisions import (
            claim_revision_preparation, claim_sealed_revision, create_collecting_revision, seal_revision,
        )

        invitation = self.queue()
        self.assertEqual(self.deliver(invitation)[0], "sent")
        source = self.reply(invitation, status="pending")
        snapshot = {"schema_version": 1, "instructions": []}
        snapshot_hash = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        publication = BotPolicyPublication.objects.create(version=1, kind="publish", schema_version=1,
            snapshot=snapshot, snapshot_hash=snapshot_hash, compiler_version="instruction-set-v1", instruction_count=0)
        self.settings_row.active_instruction_publication = publication
        self.settings_row.save(update_fields=["active_instruction_publication"])
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role=source.role)
        built = create_collecting_revision(turn, [source], bypass_quiet=True)
        self.assertIsNotNone(built.revision, built.reason)
        preparation = claim_revision_preparation(built.revision.pk)
        sealed = seal_revision(built.revision.pk, preparation.token)
        self.assertIsNotNone(sealed.revision, sealed.reason)
        claimed = claim_sealed_revision(sealed.revision.pk)
        self.assertIsNotNone(claimed.revision, claimed.reason)
        revision = claimed.revision
        kwargs = dict(source_message_id=source.pk, settings_id=self.settings_row.pk,
            settings_permission_epoch=self.settings_row.reply_permission_epoch,
            publication=PublicationBinding(publication.pk, publication.version, publication.snapshot_hash))
        with patch("management.services.call_ai_analysis.gemini_generate_text",
                side_effect=AssertionError("Deterministic consent must not use Gemini")) as gemini:
            result = apply_revision_postback(revision.pk, claimed.token, **kwargs)
            self.assertTrue(result.ready, result.reason)
            self.assertFalse(result.requires_model)
            self.assertEqual(result.receipt["consent_reason"], "business_consent_accept")
            self.assertEqual(result.quick_replies[0].payload, invitation.revoke_payload)
            replay = apply_revision_postback(revision.pk, claimed.token, **kwargs)
            self.assertTrue(replay.ready, replay.reason)
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.receipt, result.receipt)
            revision.refresh_from_db()
            with patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), patch(
                    "management.services.instagram_bot._provider_http",
                    return_value=(200, '{"message_id":"mid.consent.modern-ack"}')) as http:
                outcome = _execute_deterministic_input(revision, claimed.token, self.settings_row,
                    result.receipt, quick_replies=result.quick_replies)
        gemini.assert_not_called()
        self.assertEqual(outcome.sent_parts, 1, outcome)
        http.assert_called_once()
        payload = json.loads(http.call_args.kwargs["data"].decode())
        self.assertEqual(payload["message"]["quick_replies"][0]["payload"], invitation.revoke_payload)
        self.assertEqual(IgMarketingConsentAnswer.objects.filter(invitation=invitation).count(), 1)
        self.assertEqual(IgSourceActionReceipt.objects.filter(source_message_id=source.pk, kind="postback").count(), 1)
        effect = revision.delivery_effects.get()
        self.assertEqual(effect.state, "sent")
        self.assertEqual(effect.provider_message_id, "mid.consent.modern-ack")
        proof = consent.post_purchase_consent_source(self.customer, self.order.pk)
        self.assertEqual(proof["source_message_id"], source.pk)
        self.assertEqual(proof["source_mid"], source.mid)

    def test_privacy_purge_outside_atomic_fence_is_denied_without_deleting_history(self):
        from management.services.ig_marketing_consent_privacy import purge_marketing_consent_data

        invitation, source = self.grant_business_consent(self.customer, self.order, self.assignment)
        self.customer.privacy_erasure_started_at = timezone.now()
        self.customer.save(update_fields=["privacy_erasure_started_at"])
        with self.assertRaises(ValueError):
            purge_marketing_consent_data([self.customer.pk])
        self.assertTrue(IgMarketingConsentInvitation.objects.filter(pk=invitation.pk).exists())
        self.assertTrue(IgMarketingConsentAnswer.objects.filter(source_message_id=source.pk).exists())


@override_settings(IG_POST_PURCHASE_BUSINESS_CONSENT_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class MarketingConsentBoundedProducerTests(_MarketingConsentFixture, TestCase):
    """Canonical zero-legacy-event assignments still have a finite fair queue."""
    setUp = MarketingConsentAuthorityTests.setUp

    def native_purchase(self, *, text="Please reply in English. Thank you for my order.",
                        profile="uk", assignment_source="provider_auto"):
        from management.models import IgCheckoutProposal, IgDeal, IgOrderAttribution
        from management.services.ig_commercial_episodes import ensure_episode_for_deal
        from orders.models import PaymentAttempt

        customer, order, assignment, source = MarketingConsentAuthorityTests.native_purchase(
            self, text=text, profile=profile, assignment_source=assignment_source)
        deal = IgDeal.objects.create(client=customer, order=order, status="quoted", amount=order.total_sum)
        proposal = IgCheckoutProposal.objects.create_current(deal=deal,
            commercial_episode=ensure_episode_for_deal(deal), catalog_total=order.total_sum,
            quoted_total=order.total_sum, requested_payment_amount=order.total_sum,
            items_digest=hashlib.sha256(f"bounded-consent-purchase:{order.pk}".encode()).hexdigest())
        attempt = PaymentAttempt.objects.create(order=order, fingerprint=f"{order.pk:064x}",
            full_name=order.full_name, phone=order.phone, pay_type="online_full", status="converted",
            gross_amount=order.total_sum, payable_amount=order.total_sum, payment_amount=order.total_sum)
        proposal.payment_attempt = attempt
        proposal.save(update_fields=["payment_attempt", "updated_at"])
        IgOrderAttribution.objects.create(order=order, client=customer, deal=deal,
            creation_mode="provider_auto", payment_source="provider_attempt")
        return customer, order, assignment, source

    def test_reconcile_mints_at_most_limit_even_when_every_assignment_is_canonical(self):
        from management.models import IgOrderCustomerEvent
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        purchases = [(self.customer, self.order, self.assignment, self.source)]
        purchases.extend(self.native_purchase() for _ in range(5))
        expected = [row[2].pk for row in purchases]
        for batch in range(3):
            with patch.object(consent, "queue_post_purchase_consent", wraps=consent.queue_post_purchase_consent) as queue, patch(
                    "management.services.instagram_bot._provider_http") as provider:
                result = reconcile_order_customer_events(limit=2, send=False, now=self.now)
            self.assertEqual(queue.call_count, 2)
            provider.assert_not_called()
            self.assertEqual((result["consent_considered"], result["consent_queued"]), (2, 2))
            self.assertEqual(result["created"], 0)
            self.assertEqual(IgOrderCustomerEvent.objects.count(), 0)
            self.assertEqual(list(IgMarketingConsentInvitation.objects.order_by("assignment_id").values_list(
                "assignment_id", flat=True)), expected[:2 * (batch + 1)])
        with patch.object(consent, "queue_post_purchase_consent", wraps=consent.queue_post_purchase_consent) as queue:
            replay = reconcile_order_customer_events(limit=2, send=False, now=self.now)
        queue.assert_not_called()
        self.assertEqual((replay["consent_considered"], replay["consent_queued"]), (0, 0))
        self.assertEqual(IgMarketingConsentInvitation.objects.count(), 6)

    def test_served_first_canonical_order_does_not_starve_next_eligible_assignment(self):
        from management.services.ig_order_fulfillment import ensure_assignment_events, reconcile_order_customer_events

        self.assertEqual(ensure_assignment_events(self.assignment, now=self.now), [])
        first = IgMarketingConsentInvitation.objects.get(assignment=self.assignment)
        customer, order, assignment, _source = self.native_purchase()
        with patch.object(consent, "queue_post_purchase_consent", wraps=consent.queue_post_purchase_consent) as queue:
            result = reconcile_order_customer_events(limit=1, send=False, now=self.now)
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[2].pk, assignment.pk)
        self.assertEqual((result["consent_considered"], result["consent_queued"]), (1, 1))
        self.assertEqual(IgMarketingConsentInvitation.objects.filter(client=customer, order=order).count(), 1)
        first.refresh_from_db()
        self.assertEqual((first.state, first.attempts, first.provider_message_id), ("pending", 0, ""))
        self.assertEqual(IgMarketingConsentInvitation.objects.count(), 2)

    def test_direct_opt_out_of_queue_and_zero_reconcile_budget_create_no_invitation(self):
        from management.services.ig_order_fulfillment import ensure_assignment_events, reconcile_order_customer_events

        self.assertEqual(ensure_assignment_events(self.assignment, now=self.now, queue_consent=False), [])
        with patch.object(consent, "queue_post_purchase_consent", wraps=consent.queue_post_purchase_consent) as queue:
            result = reconcile_order_customer_events(limit=0, send=False, now=self.now)
        queue.assert_not_called()
        self.assertEqual((result["consent_considered"], result["consent_queued"]), (0, 0))
        self.assertFalse(IgMarketingConsentInvitation.objects.exists())
        self.assertEqual(ensure_assignment_events(self.assignment, now=self.now), [])
        self.assertEqual(IgMarketingConsentInvitation.objects.count(), 1)

    def test_existing_invitation_exclusion_is_bound_to_exact_current_reset(self):
        from management.models import IgFunnelResetAudit
        from management.services.ig_order_fulfillment import ensure_assignment_events, reconcile_order_customer_events

        ensure_assignment_events(self.assignment, now=self.now)
        first = IgMarketingConsentInvitation.objects.get(assignment=self.assignment)
        reset = IgFunnelResetAudit.objects.create(client=self.customer,
            reset_after_message_id=self.source.pk, reason="bounded consent reset fixture")
        self.consent_source(self.customer, at=self.now, text="Please send me the new invitation.")
        result = reconcile_order_customer_events(limit=1, send=False, now=self.now)
        self.assertEqual((result["consent_considered"], result["consent_queued"]), (1, 1))
        self.assertEqual(IgMarketingConsentInvitation.objects.filter(assignment=self.assignment).count(), 2)
        self.assertTrue(IgMarketingConsentInvitation.objects.filter(assignment=self.assignment,
            reset_audit_id=reset.pk).exists())
        first.refresh_from_db()
        self.assertEqual(first.reset_audit_id, 0)

    def test_generic_resume_never_resurrects_prior_business_consent_or_pending_card(self):
        from management.models import IgPermissionTransitionJob
        from management.services.ig_order_fulfillment import reconcile_order_customer_events
        from management.services.ig_permission_transitions import (
            attempt_permission_transition, create_permission_transition,
        )

        accepted, _answer = self.grant_business_consent(self.customer, self.order, self.assignment)
        pending_order = Order.objects.create(order_number="CONSENT-PEND-RESUME",
            full_name="Website buyer", phone="380501112233", total_sum="590.00",
            payment_status="paid", source="web", status="new")
        pending_assignment = link_order_to_client(pending_order, client=self.customer, actor=self.actor)
        pending = consent.queue_post_purchase_consent(self.customer, pending_order, pending_assignment)
        self.assertIsNotNone(pending)
        stop = self.consent_source(self.customer, text="Do not send me news or offers.")
        transition = create_permission_transition(kind=IgPermissionTransitionJob.Kind.OPT_OUT,
            dedupe_key=f"bounded-consent-opt-out:{stop.pk}", client=self.customer,
            settings=self.settings_row, source_message=stop)
        self.assertTrue(attempt_permission_transition(transition.pk))
        self.customer.refresh_from_db()
        self.assertIsNotNone(self.customer.opted_out_at)
        # Generic manual bot resume is not a new purpose-specific answer.
        self.customer.opted_in_at = self.customer.opted_out_at + timedelta(seconds=1)
        self.customer.bot_paused = False
        self.customer.paused_reason = ""
        self.customer.save(update_fields=["opted_in_at", "bot_paused", "paused_reason", "updated_at"])
        self.assertFalse(consent.has_post_purchase_consent(self.customer, self.order.pk))
        self.assertIsNone(consent.post_purchase_consent_source(self.customer, self.order.pk))
        self.assertFalse(consent.has_post_purchase_consent(self.customer, pending_order.pk))
        with patch("management.services.instagram_bot._provider_http") as provider:
            self.assertEqual(consent.process_consent_invitation(pending.pk), "failed")
            result = reconcile_order_customer_events(limit=1, send=False)
        provider.assert_not_called()
        self.assertEqual((result["consent_considered"], result["consent_queued"]), (0, 0))
        self.assertEqual(IgMarketingConsentInvitation.objects.count(), 2)
        accepted.refresh_from_db()
        self.assertEqual(accepted.state, "sent")
        self.assertEqual(IgMarketingConsentAnswer.objects.filter(invitation=accepted).count(), 1)

    def test_failed_manual_head_advances_advisory_cursor_to_later_verified_purchase(self):
        from management.services.ig_order_fulfillment import reconcile_order_customer_events

        # Preserve the earliest actual paid order while removing its source
        # qualification: manual 'paid' without an audited action is not truth.
        self.order.source = "manual"
        self.order.payment_payload = {}
        self.order.save(update_fields=["source", "payment_payload"])
        customer, order, assignment, _source = self.native_purchase()
        cache.clear()
        with patch.object(consent, "queue_post_purchase_consent", wraps=consent.queue_post_purchase_consent) as queue, patch(
                "management.services.instagram_bot._provider_http") as provider:
            refused = reconcile_order_customer_events(limit=1, send=False, now=self.now)
            self.assertEqual(queue.call_args.args[2].pk, self.assignment.pk)
            self.assertEqual((refused["consent_considered"], refused["consent_queued"]), (1, 0))
            self.assertFalse(IgMarketingConsentInvitation.objects.exists())
            progressed = reconcile_order_customer_events(limit=1, send=False, now=self.now)
            self.assertEqual(queue.call_count, 2)
            self.assertEqual(queue.call_args.args[2].pk, assignment.pk)
            self.assertEqual((progressed["consent_considered"], progressed["consent_queued"]), (1, 1))
            wrapped = reconcile_order_customer_events(limit=1, send=False, now=self.now)
        provider.assert_not_called()
        self.assertEqual(queue.call_count, 3)
        self.assertEqual((wrapped["consent_considered"], wrapped["consent_queued"]), (1, 0))
        self.assertFalse(IgMarketingConsentInvitation.objects.filter(assignment=self.assignment).exists())
        self.assertEqual(IgMarketingConsentInvitation.objects.filter(client=customer, order=order).count(), 1)
        cache.clear()
