"""Both delivery owners freeze a safe review request for repeat customers."""
from copy import deepcopy
from datetime import timedelta
import hashlib
from unittest.mock import patch
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import (
    IgCheckoutProposal, IgDeal, IgFollowUpTask, IgOrderAttribution,
    InstagramBotMessage,
)
from management.ig_bot_models import (
    IgLifecycleEvent, IgOrderCustomerEvent, IgPaymentProjection,
    IgPostSaleCase, IgUgcReward, IgUgcRewardLifetime,
)
from management.services.ig_commercial_episodes import ensure_episode_for_deal
from management.services.ig_lifecycle import (
    MESSAGE_SNAPSHOT_KEY, _lifecycle_message_key, dispatch_lifecycle_event,
    ensure_lifecycle_event,
)
from management.services.ig_order_assignments import link_order_to_client, unlink_order_from_client
from management.services.ig_order_fulfillment import deliver_event, ensure_assignment_events
from management.services.ig_post_purchase_invitation import post_purchase_invitation_text
from management.services.ig_response_debt import DEBT_REASON
from management.services.ig_ugc_rewards import _identity_digest_candidates
from orders.models import Order, PaymentAttempt


IDENTITY_KEYS = {
    "active": "active-test-identity-secret-00000000000000",
    "retained": "retained-test-identity-secret-000000000000",
}
MODE_KEY = "post_purchase_invitation"


class ReviewOnlyCopyContractTests(SimpleTestCase):
    def setUp(self):
        self.customer = SimpleNamespace(pk=1)
        self.order = SimpleNamespace(pk=2, order_number="TWC-PLAIN-COPY")

    def test_all_supported_locales_are_plain_exact_review_snapshots(self):
        from management.services.ig_post_purchase_invitation import post_purchase_invitation_block_reason

        for locale, review_word in (("uk", "чесний відгук"), ("ru", "честный отзыв"), ("en", "honest review")):
            with self.subTest(locale=locale):
                text = post_purchase_invitation_text(locale, self.order, mode="review_only")
                self.assertIn(review_word, text)
                self.assertIn(self.order.order_number, text)
                for offer in ("@twocomms", "10%", "90 days", "90 днів", "90 дней", "стор", "story"):
                    self.assertNotIn(offer, text.casefold())
                payload = {MODE_KEY: {"version": 1, "mode": "review_only", "order_number": self.order.order_number}}
                with (
                    patch("management.services.ig_ugc_rewards.ugc_service_case_reason", return_value=""),
                    patch("management.services.ig_response_debt.unresolved_reply_debts") as debts,
                    patch("management.services.ig_ugc_rewards.ugc_identity_already_rewarded",
                        side_effect=AssertionError("No offer entitlement read")) as used,
                    patch("management.services.ig_ugc_rewards.ugc_identity_lifetime_conflicted",
                        side_effect=AssertionError("No offer entitlement read")) as conflicted,
                ):
                    debts.return_value.filter.return_value.exists.return_value = False
                    self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order,
                        payload=payload, text=text, locale=locale, final_text=text), "")
                used.assert_not_called()
                conflicted.assert_not_called()
                for changed in (text + " ", text + " 10%", text.replace(self.order.order_number, "OTHER")):
                    self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order,
                        payload=payload, text=changed, locale=locale), "post_purchase_eligibility_unknown")

    def test_malformed_metadata_and_unknown_locale_fail_before_authority_reads(self):
        from management.services.ig_post_purchase_invitation import post_purchase_invitation_block_reason

        valid = {"version": 1, "mode": "review_only", "order_number": self.order.order_number}
        text = post_purchase_invitation_text("en", self.order, mode="review_only")
        malformed = (
            None, [], "review_only", {},
            {**valid, "version": True}, {**valid, "version": 1.0}, {**valid, "version": 2},
            {**valid, "mode": None}, {**valid, "mode": []}, {**valid, "mode": "unknown"},
            {**valid, "order_number": ""}, {**valid, "order_number": "x" * 21},
            {**valid, "order_number": "TWC\nINJECTION"}, {**valid, "order_number": 123},
        )
        with (
            patch("management.services.ig_ugc_rewards.ugc_service_case_reason",
                side_effect=AssertionError("Malformed snapshot must fail before authority reads")) as service,
            patch("management.services.ig_response_debt.unresolved_reply_debts",
                side_effect=AssertionError("Malformed snapshot must fail before authority reads")) as debt,
            patch("management.services.ig_ugc_rewards.ugc_identity_already_rewarded",
                side_effect=AssertionError("Malformed snapshot must fail before authority reads")) as used,
        ):
            for metadata in malformed:
                with self.subTest(metadata=metadata):
                    self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order,
                        payload={MODE_KEY: metadata}, text=text, locale="en"), "post_purchase_eligibility_unknown")
            for locale in (None, "", "fr", "en-US", True):
                with self.subTest(locale=locale):
                    self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order,
                        payload={MODE_KEY: valid}, text=text, locale=locale), "post_purchase_eligibility_unknown")
        service.assert_not_called()
        debt.assert_not_called()
        used.assert_not_called()


class ReviewOnlyOwnerContract:
    """Use native eligibility and outbox rows; replace transport only."""
    canonical = False

    def setUp(self):
        if self.canonical:
            from management.tests_ig_lifecycle import InstagramLifecycleTests
            InstagramLifecycleTests.setUp(self)
            self.customer = self.client
        else:
            from management.tests_ig_order_fulfillment import IgOrderFulfillmentTests
            IgOrderFulfillmentTests.setUp(self)
            self.customer = self.ig_client
        self.customer.last_user_message_at = timezone.now()
        self.customer.save(update_fields=["last_user_message_at", "updated_at"])
        self.serial = 0
        self._delivered(self.order)

    def _delivered(self, order):
        order.status = "done"
        order.tracking_number = "20400000000009"
        order.tracking_status_code = 9
        order.tracking_terminal_at = timezone.now()
        order.save(update_fields=["status", "tracking_number", "tracking_status_code", "tracking_terminal_at"])

    def _consume(self, *, retained=False, empty_active=False):
        active, old = _identity_digest_candidates(self.customer)
        if empty_active:
            IgUgcRewardLifetime.objects.create(client=self.customer, identity_digest=active)
        return IgUgcRewardLifetime.objects.create(
            client=None if retained else self.customer,
            identity_digest=old if retained else active, consumed_at=timezone.now(),
        )

    def _event(self, *, order=None, insert_defaults=None):
        order = order or self.order
        assignment = link_order_to_client(order, client=self.customer)
        model = IgLifecycleEvent if self.canonical else IgOrderCustomerEvent
        create = model.objects.get_or_create

        def native_insert(**kwargs):
            # Historical/adversarial snapshots enter BEFORE their first INSERT.
            # The genuine manager, model validation and SQL constraints still
            # execute. Existing immutable rows are never rewritten or deleted.
            if insert_defaults is not None:
                defaults = dict(kwargs["defaults"])
                defaults["payload"] = deepcopy(defaults["payload"])
                insert_defaults(defaults)
                kwargs["defaults"] = defaults
            return create(**kwargs)

        with patch.object(model.objects, "get_or_create", side_effect=native_insert):
            if self.canonical:
                event, created = ensure_lifecycle_event(order, "delivered_review_requested", payload={"status_code": "9"})
                self.assertTrue(created)
            else:
                created = ensure_assignment_events(assignment)
                event = IgOrderCustomerEvent.objects.get(assignment=assignment, kind="delivered_review")
                self.assertIn(event, created)
        self.assertIsNotNone(event)
        return event

    def _same_event(self, event):
        if self.canonical:
            same, created = ensure_lifecycle_event(event.order, event.kind, payload={"status_code": "9"})
            self.assertFalse(created)
        else:
            self.assertEqual(ensure_assignment_events(event.assignment), [])
            same = IgOrderCustomerEvent.objects.get(event_key=event.event_key)
        self.assertEqual(same.pk, event.pk)
        return same

    def _text(self, event):
        return event.payload[MESSAGE_SNAPSHOT_KEY] if self.canonical else event.message_snapshot

    def _request_boundary(self, kwargs, *, delivered=0, receipts=(), planned=1):
        if self.canonical:
            return kwargs["provider_request_boundary_factory"](
                delivered_chunk_count=delivered, provider_message_ids=receipts,
                planned_chunk_count=planned,
            )
        return kwargs["permission_boundary_factory"]()

    def _start(self, kwargs):
        if self.canonical:
            self.assertTrue(kwargs["provider_io_started_callback"]())

    def _transport_success(self, _settings, recipient, text, **kwargs):
        self.assertEqual(recipient, self.customer.igsid)
        self.sent_text = text
        self._start(kwargs)
        with self._request_boundary(kwargs) as permitted:
            self.assertTrue(permitted)
            kwargs["provider_message_callback"]("mid.review-only.success")
        # Canonical dispatch consumes the returned normalized receipt as well
        # as the durable callback checkpoint; callback-only success is ambiguous.
        return (True, "", "", "mid.review-only.success") if self.canonical else (True, "", "")

    def _dispatch(self, event, *, transport=None):
        with (
            patch("management.services.instagram_bot.send_text",
                side_effect=transport or AssertionError("Unexpected customer transport")) as send,
            patch("management.services.instagram_bot._provider_http",
                side_effect=AssertionError("Unexpected provider HTTP")),
            patch("management.services.instagram_bot.notify_manager"),
        ):
            state = dispatch_lifecycle_event(event.pk) if self.canonical else deliver_event(event.pk)
        event.refresh_from_db()
        return state, send

    def _service(self):
        self.serial += 1
        source = InstagramBotMessage.objects.create(client=self.customer,
            sender_id=self.customer.igsid, role="user", source="webhook", status="done",
            text="Потрібен обмін", mid=f"review-only-service-{self.serial}")
        return IgPostSaleCase.objects.create(client=self.customer, order=self.order,
            source_message=source, case_type="exchange", status="in_transit")

    def _debt(self, *, cancelled=False):
        self.serial += 1
        return IgFollowUpTask.objects.create(client=self.customer, kind="manager_task",
            reason=DEBT_REASON, event_key=f"review-only-debt:{self.serial}", due_at=timezone.now(),
            status="cancelled" if cancelled else "skipped")

    def _assert_plain(self, text):
        self.assertEqual(text, post_purchase_invitation_text(self.customer.language,
            self.order, mode="review_only"))
        self.assertTrue(any(word in text.casefold() for word in ("review", "відгук", "отзыв")))
        for offer in ("@twocomms", "10%", "90 days", "90 днів", "90 дней", "стор", "story", "stories"):
            self.assertNotIn(offer, text.casefold())

    def test_positive_consumed_lifetime_creates_plain_review_and_real_sent_receipt_once(self):
        self._consume()
        event = self._event()
        self.assertEqual(event.payload[MODE_KEY], {"version": 1, "mode": "review_only",
            "order_number": self.order.order_number})
        self._assert_plain(self._text(event))
        immutable = (deepcopy(event.payload), self._text(event), event.event_key)
        with (
            patch("management.services.ig_ugc_rewards.ugc_identity_already_rewarded",
                side_effect=AssertionError("Review-only dispatch must not read entitlement")),
            patch("management.services.ig_ugc_rewards.ugc_identity_lifetime_conflicted",
                side_effect=AssertionError("Review-only dispatch must not read entitlement")),
        ):
            state, send = self._dispatch(event, transport=self._transport_success)
        self.assertEqual(state, "sent", event.last_error)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sent_text, immutable[1])
        self.assertEqual(event.provider_message_id, "mid.review-only.success")
        self.assertIsNotNone(event.completed_at)
        state, send = self._dispatch(event)
        send.assert_not_called()
        self.assertEqual(event.state, "sent")
        self.assertEqual((event.payload, self._text(event), event.event_key), immutable)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 1)
        self.assertEqual(IgUgcReward.objects.count(), 0)
        if self.canonical:
            message = InstagramBotMessage.objects.get(synthetic_event_key=_lifecycle_message_key(event.event_key))
            self.assertEqual(message.send_state, "sent")
            self.assertEqual(message.text, immutable[1])
            self.assertEqual(message.provider_message_id, event.provider_message_id)

    def test_consumed_retained_key_after_empty_active_slot_selects_plain_review(self):
        self._consume(retained=True, empty_active=True)
        event = self._event()
        self.assertEqual(event.payload[MODE_KEY]["mode"], "review_only")
        self._assert_plain(self._text(event))
        state, send = self._dispatch(event, transport=self._transport_success)
        self.assertEqual(state, "sent", event.last_error)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 2)

    def test_unused_identity_keeps_conditional_reward_and_creation_uses_no_lifetime_dml(self):
        event = self._event()
        self.assertEqual(event.payload[MODE_KEY]["mode"], "review_and_reward")
        for token in ("@twocomms", "10%", "90"):
            self.assertIn(token, self._text(event))
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 0)
        self.assertEqual(IgUgcReward.objects.count(), 0)
        state, send = self._dispatch(event, transport=self._transport_success)
        self.assertEqual(state, "sent", event.last_error)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 0)

    def test_unknown_at_creation_keeps_combined_snapshot_and_can_recover_before_dispatch(self):
        with override_settings(IG_UGC_IDENTITY_HMAC_KEYRING={}):
            event = self._event()
        self.assertEqual(event.payload[MODE_KEY]["mode"], "review_and_reward")
        immutable = deepcopy(event.payload)
        state, send = self._dispatch(event, transport=self._transport_success)
        self.assertEqual(state, "sent", event.last_error)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(event.payload, immutable)

    def test_unknown_combined_dispatch_fails_closed_without_plain_review_downgrade(self):
        event = self._event()
        original = self._text(event)
        with override_settings(IG_UGC_IDENTITY_HMAC_KEYRING={}):
            state, send = self._dispatch(event)
        self.assertEqual(state, "cancelled")
        self.assertEqual(event.last_error, "post_purchase_eligibility_unknown")
        self.assertEqual(self._text(event), original)
        send.assert_not_called()
        self._same_event(event)
        self.assertEqual(type(event).objects.filter(order=self.order, kind=event.kind).count(), 1)

    def test_old_no_mode_combined_snapshot_is_immutable_and_used_identity_cancels(self):
        event = self._event(insert_defaults=lambda defaults: defaults["payload"].pop(MODE_KEY))
        original = (deepcopy(event.payload), self._text(event), event.event_key)
        self._consume()
        same = self._same_event(event)
        self.assertEqual((same.payload, self._text(same), same.event_key), original)
        state, send = self._dispatch(event)
        self.assertEqual(state, "cancelled")
        self.assertEqual(event.last_error, "post_purchase_already_rewarded")
        self.assertEqual((event.payload, self._text(event), event.event_key), original)
        send.assert_not_called()

    def test_plain_mode_flag_cannot_authorize_a_reward_snapshot(self):
        def forged_defaults(defaults):
            defaults["payload"][MODE_KEY]["mode"] = "review_only"

        event = self._event(insert_defaults=forged_defaults)
        state, send = self._dispatch(event)
        self.assertEqual(state, "cancelled")
        self.assertEqual(event.last_error, "post_purchase_eligibility_unknown")
        self.assertIn("10%", self._text(event))
        send.assert_not_called()

    def test_invalid_version_or_mode_or_captured_number_is_not_plain_authority(self):
        self._consume()
        event = self._event()
        original = deepcopy(event.payload)
        invalid = (
            {**original[MODE_KEY], "version": True},
            {**original[MODE_KEY], "version": 2},
            {**original[MODE_KEY], "mode": None},
            {**original[MODE_KEY], "order_number": "OTHER-ORDER"},
        )
        from management.services.ig_post_purchase_invitation import post_purchase_invitation_block_reason
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                payload = {**original, MODE_KEY: metadata}
                self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order,
                    payload=payload, text=self._text(event), locale=event.locale),
                    "post_purchase_eligibility_unknown")

    def test_canonical_final_text_cannot_reintroduce_an_incentive(self):
        self._consume()
        event = self._event()
        from management.services.ig_post_purchase_invitation import post_purchase_invitation_block_reason
        self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order,
            payload=event.payload, text=self._text(event), locale=event.locale,
            final_text=self._text(event) + " Earn a 10% discount!"),
            "post_purchase_eligibility_unknown")

    def test_mode_is_verified_again_at_final_transport_boundary(self):
        self._consume()
        event = self._event()
        original_payload = deepcopy(event.payload)
        from management.services.ig_post_purchase_invitation import post_purchase_invitation_block_reason

        def detached_observation(client, order, payload=None, text=None, locale=None, final_text=""):
            # Adversarial read seam only. No stored row or immutable field is
            # changed, and the genuine validator decides the observed payload.
            observed = deepcopy(payload)
            observed[MODE_KEY]["order_number"] = "FORGED-ORDER"
            return post_purchase_invitation_block_reason(client, order, observed, text, locale, final_text)

        def forged_transport(_settings, _recipient, _text, **kwargs):
            self._start(kwargs)
            owner = "ig_lifecycle" if self.canonical else "ig_order_fulfillment"
            with patch(f"management.services.{owner}.post_purchase_invitation_block_reason",
                    side_effect=detached_observation):
                with self._request_boundary(kwargs) as permitted:
                    self.assertFalse(permitted)
            return False, "cancelled", "permission boundary rejected"

        state, send = self._dispatch(event, transport=forged_transport)
        self.assertEqual(state, "cancelled", event.last_error)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(event.last_error, "post_purchase_eligibility_unknown")
        self.assertFalse(event.provider_message_id)
        self.assertEqual(event.payload, original_payload)

    def test_open_service_still_blocks_plain_review(self):
        self._consume()
        event = self._event()
        case = self._service()
        state, send = self._dispatch(event)
        self.assertEqual(state, "cancelled")
        self.assertEqual(event.last_error, "post_purchase_service_case_open")
        send.assert_not_called()
        case.status = "resolved"
        case.save(update_fields=["status", "updated_at"])
        # A terminal event is never revived to send once service is resolved.
        state, send = self._dispatch(event)
        send.assert_not_called()
        self.assertEqual(event.state, "cancelled")

    def test_real_reply_debt_blocks_plain_review(self):
        self._consume()
        event = self._event()
        self._debt()
        state, send = self._dispatch(event)
        self.assertEqual(state, "cancelled")
        self.assertEqual(event.last_error, "post_purchase_reply_debt_open")
        send.assert_not_called()

    def test_cancelled_reply_debt_does_not_block_plain_review(self):
        self._consume()
        event = self._event()
        self._debt(cancelled=True)
        state, send = self._dispatch(event, transport=self._transport_success)
        self.assertEqual(state, "sent", event.last_error)
        self.assertEqual(send.call_count, 1)

    def test_service_opened_after_claim_rejects_plain_review_at_final_boundary(self):
        self._consume()
        event = self._event()

        def late_service(_settings, _recipient, _text, **kwargs):
            self._start(kwargs)
            self._service()
            with self._request_boundary(kwargs) as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "permission boundary rejected"

        state, send = self._dispatch(event, transport=late_service)
        self.assertEqual(state, "cancelled")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(event.last_error, "post_purchase_service_case_open")
        self.assertFalse(event.provider_message_id)

    def test_reply_debt_after_partial_plain_receipt_is_ambiguous_and_never_replayed(self):
        self._consume()
        event = self._event()
        receipt = "mid.review-only.partial"

        def late_debt(_settings, _recipient, _text, **kwargs):
            self._start(kwargs)
            with self._request_boundary(kwargs, planned=2) as permitted:
                self.assertTrue(permitted)
                kwargs["provider_message_callback"](receipt)
            self._debt()
            with self._request_boundary(kwargs, delivered=1, receipts=(receipt,), planned=2) as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "permission boundary rejected"

        state, send = self._dispatch(event, transport=late_debt)
        self.assertEqual(state, "ambiguous")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(event.provider_message_id, receipt)
        self.assertIn("partial delivery before post_purchase_reply_debt_open", event.last_error)
        state, send = self._dispatch(event)
        send.assert_not_called()
        self.assertEqual(event.state, "ambiguous")
        self.assertEqual(event.provider_message_id, receipt)

    def test_plain_snapshot_keeps_captured_order_number_after_order_edit(self):
        self._consume()
        event = self._event()
        captured = self._text(event)
        self.order.order_number = "TWC-EDITED-NUMBER"
        self.order.save(update_fields=["order_number"])
        state, send = self._dispatch(event, transport=self._transport_success)
        self.assertEqual(state, "sent", event.last_error)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sent_text, captured)
        self.assertNotIn("TWC-EDITED-NUMBER", self.sent_text)

    def test_closed_response_window_still_requires_manager_review(self):
        self._consume()
        event = self._event()
        self.customer.last_user_message_at = timezone.now() - timedelta(days=2)
        self.customer.save(update_fields=["last_user_message_at", "updated_at"])
        state, send = self._dispatch(event)
        self.assertEqual(state, "manager_review")
        self.assertEqual(event.last_error, "standard_response_window_closed" if self.canonical else "standard response window is closed")
        send.assert_not_called()

    def test_pause_still_prevents_plain_review_transport(self):
        self._consume()
        event = self._event()
        self.customer.bot_paused = True
        self.customer.manager_takeover = True
        self.customer.save(update_fields=["bot_paused", "manager_takeover", "updated_at"])
        state, send = self._dispatch(event)
        self.assertNotEqual(event.state, "sent")
        self.assertFalse(event.provider_message_id)
        send.assert_not_called()

    def test_erasure_still_prevents_plain_review_transport(self):
        self._consume()
        event = self._event()
        self.customer.privacy_erasure_started_at = timezone.now()
        self.customer.save(update_fields=["privacy_erasure_started_at", "updated_at"])
        def denied_by_erasure(_settings, _recipient, _text, **kwargs):
            # Legacy send_text owns the final permission seam; invoking that
            # seam is distinct from a provider request being authorized.
            with self._request_boundary(kwargs) as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "privacy erasure boundary rejected"

        state, send = self._dispatch(event, transport=denied_by_erasure)
        self.assertNotEqual(event.state, "sent")
        self.assertFalse(event.provider_message_id)
        self.assertIsNone(event.completed_at)
        self.assertLessEqual(send.call_count, 1)

    def test_unassigned_order_still_prevents_plain_review_transport(self):
        self._consume()
        event = self._event()
        assignment = event.assignment if not self.canonical else self.order.instagram_assignment
        unlink_order_from_client(self.order, client=self.customer,
            expected_version=assignment.version, reason_code="manager_correction", reason="Wrong owner")
        state, send = self._dispatch(event)
        self.assertNotEqual(event.state, "sent")
        self.assertFalse(event.provider_message_id)
        send.assert_not_called()

    def test_retracted_carrier_receipt_still_blocks_plain_review(self):
        self._consume()
        event = self._event()
        self.order.tracking_terminal_at = None
        self.order.tracking_status_code = 7
        self.order.save(update_fields=["tracking_terminal_at", "tracking_status_code"])
        state, send = self._dispatch(event)
        self.assertNotEqual(event.state, "sent")
        self.assertFalse(event.provider_message_id)
        send.assert_not_called()

    def test_second_delivered_order_has_its_own_idempotent_plain_review_event(self):
        self._consume()
        first = self._event()
        first_snapshot = (deepcopy(first.payload), self._text(first), first.event_key)
        second_order = Order.objects.create(order_number="TWC-REPEAT-REVIEW", full_name="Repeat buyer",
            phone="380501112233", city="Kyiv", np_office="Branch 1", total_sum="950.00",
            payment_status="paid", pay_type="online_full")
        self._delivered(second_order)
        if self.canonical:
            deal = IgDeal.objects.create(client=self.customer, status="paid", payment_status="paid",
                amount=second_order.total_sum, paid_at=timezone.now())
            ensure_episode_for_deal(deal)
            attempt = PaymentAttempt.objects.create(fingerprint=hashlib.sha256(b"review-only-second-order").hexdigest(),
                full_name=second_order.full_name, phone=second_order.phone, city=second_order.city,
                np_office=second_order.np_office, pay_type="online_full", status="converted",
                cart_snapshot={"checkout_surface": "instagram_proposal", "items": []},
                gross_amount=second_order.total_sum, payable_amount=second_order.total_sum,
                payment_amount=second_order.total_sum, order=second_order)
            proposal = IgCheckoutProposal.objects.create_current(deal=deal,
                catalog_total=second_order.total_sum, quoted_total=second_order.total_sum,
                requested_payment_amount=second_order.total_sum, items_digest="b" * 64)
            proposal.payment_attempt = attempt
            proposal.save(update_fields=["payment_attempt", "updated_at"])
            IgOrderAttribution.objects.create(order=second_order, client=self.customer, deal=deal,
                creation_mode="provider_auto", payment_source="provider_attempt")
            IgPaymentProjection.objects.create(client=self.customer, deal=deal,
                truth="confirmed", gross_amount=second_order.total_sum, paid_at=deal.paid_at)
        second = self._event(order=second_order)
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.event_key, second.event_key)
        self.assertEqual(second.payload[MODE_KEY]["mode"], "review_only")
        self.assertIn(second_order.order_number, self._text(second))
        self.assertNotIn("10%", self._text(second))
        same = self._same_event(second)
        self.assertEqual(same.pk, second.pk)
        first.refresh_from_db()
        self.assertEqual((first.payload, self._text(first), first.event_key), first_snapshot)
        self.assertEqual(type(first).objects.filter(client=self.customer, kind=first.kind).count(), 2)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 1)


@override_settings(IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="active", IG_UGC_IDENTITY_HMAC_KEYRING=IDENTITY_KEYS)
class LegacyReviewOnlyContractTests(ReviewOnlyOwnerContract, TestCase):
    pass


@override_settings(IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="active", IG_UGC_IDENTITY_HMAC_KEYRING=IDENTITY_KEYS)
class CanonicalReviewOnlyContractTests(ReviewOnlyOwnerContract, TestCase):
    canonical = True

    def test_previously_persisted_final_text_cannot_add_reward_to_plain_snapshot(self):
        self._consume()
        event = self._event()
        original_payload = deepcopy(event.payload)
        self.assertEqual(event.final_text, "")
        event.final_text = self._text(event) + " Earn a 10% discount!"
        # The model permits the first final-text materialization; subsequent
        # changes are immutable. This is a persisted adversarial final copy.
        event.save(update_fields=["final_text", "updated_at"])
        state, send = self._dispatch(event)
        self.assertEqual(state, "cancelled", event.last_error)
        self.assertEqual(event.last_error, "post_purchase_eligibility_unknown")
        self.assertEqual(event.payload, original_payload)
        self.assertIn("10%", event.final_text)
        self.assertFalse(event.provider_message_id)
        send.assert_not_called()
