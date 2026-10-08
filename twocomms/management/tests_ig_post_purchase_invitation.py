from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.services.ig_post_purchase_invitation import (
    INVITATION_BLOCK_REASONS,
    post_purchase_invitation_block_reason,
    post_purchase_invitation_text,
)


class PostPurchaseInvitationPolicyTests(SimpleTestCase):
    def setUp(self):
        self.customer = SimpleNamespace(pk=351, igsid="customer")
        self.order = SimpleNamespace(pk=334, order_number="TWC-INVITATION")

    def test_both_owners_share_conditional_generic_copy_in_all_locales(self):
        from management.services.ig_lifecycle import _message_for
        from management.services.ig_order_fulfillment import _message

        for locale in ("uk", "ru", "en"):
            with self.subTest(locale=locale):
                text = post_purchase_invitation_text(locale, self.order)
                self.assertEqual(text, _message("delivered_review", locale, self.order, ""))
                self.assertEqual(text, _message_for("delivered_review_requested", locale, self.order, {}))
                for required in ("TWC-INVITATION", "@twocomms", "10%", "90"):
                    self.assertIn(required, text)
                for unsupported in ("T-shirt", "футбол", "your fit", "вам підійш", "подар"):
                    self.assertNotIn(unsupported, text)
        self.assertIn("After we verify", post_purchase_invitation_text("en", self.order))
        self.assertIn("from issuance", post_purchase_invitation_text("en", self.order))
        self.assertIn("if they have been tried on", post_purchase_invitation_text("en", self.order))

    def test_existing_authorities_block_without_issuing_anything(self):
        checks = (
            ("service_case_open", False, False, "post_purchase_service_case_open"),
            ("", True, False, "post_purchase_reply_debt_open"),
            ("", False, True, "post_purchase_already_rewarded"),
            ("", False, False, ""),
        )
        for service, debt, used, expected in checks:
            with (
                self.subTest(expected=expected),
                patch("management.services.ig_ugc_rewards.ugc_service_case_reason", return_value=service),
                patch("management.services.ig_response_debt.unresolved_reply_debts") as debts,
                patch("management.services.ig_ugc_rewards.ugc_identity_already_rewarded", return_value=used),
                patch("management.services.ig_ugc_rewards.ugc_identity_lifetime_conflicted", return_value=False),
            ):
                debts.return_value.filter.return_value.exists.return_value = debt
                self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order), expected)

    def test_unreadable_lifetime_authority_fails_closed(self):
        with (
            patch("management.services.ig_ugc_rewards.ugc_service_case_reason", return_value=""),
            patch("management.services.ig_response_debt.unresolved_reply_debts") as debts,
            patch("management.services.ig_ugc_rewards.ugc_identity_already_rewarded", side_effect=ValueError("keyring unavailable")),
        ):
            debts.return_value.filter.return_value.exists.return_value = False
            self.assertEqual(post_purchase_invitation_block_reason(self.customer, self.order),
                             "post_purchase_eligibility_unknown")

    def test_all_block_reasons_have_existing_finite_disposition(self):
        from management.services.ig_lifecycle_reasons import FIXED_REASON_CODES, Reason

        for reason in INVITATION_BLOCK_REASONS:
            self.assertEqual(FIXED_REASON_CODES[reason], Reason.PERMISSION_DENIED)


class LegacyPostPurchaseInvitationTests(TestCase):
    def setUp(self):
        from management import tests_ig_order_fulfillment as fixtures

        fixtures.IgOrderFulfillmentTests.setUp(self)

    def _review_event(self):
        from management.ig_bot_models import IgOrderCustomerEvent
        from management.services.ig_order_assignments import link_order_to_client
        from management.services.ig_order_fulfillment import ensure_assignment_events

        self.order.status = "done"
        self.order.tracking_status_code = 9
        self.order.tracking_terminal_at = timezone.now()
        self.order.save(update_fields=["status", "tracking_status_code", "tracking_terminal_at"])
        assignment = link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        ensure_assignment_events(assignment)
        return IgOrderCustomerEvent.objects.get(assignment=assignment, kind="delivered_review")

    def test_invitation_blocks_before_transport_and_remains_terminal(self):
        from management.services.ig_order_fulfillment import deliver_event

        event = self._review_event()
        # Separate owner rows would hide accidental resurrection. Test the
        # first reason, then prove this same terminal event cannot be resent.
        with (
            patch("management.services.ig_order_fulfillment.post_purchase_invitation_block_reason",
                  return_value="post_purchase_service_case_open"),
            patch("management.services.instagram_bot.send_text") as send,
        ):
            self.assertEqual(deliver_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_service_case_open")
        send.assert_not_called()
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(deliver_event(event.pk), "skipped")
        send.assert_not_called()

    def test_invitation_rechecks_reward_authority_at_final_send_boundary(self):
        from management.services.ig_order_fulfillment import deliver_event

        event = self._review_event()
        blocked = False

        def reason(*_args):
            return "post_purchase_already_rewarded" if blocked else ""

        def send_text(_settings, _igsid, _text, **kwargs):
            nonlocal blocked
            blocked = True
            with kwargs["permission_boundary_factory"]() as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "permission boundary rejected"

        with (
            patch("management.services.ig_order_fulfillment.post_purchase_invitation_block_reason", side_effect=reason),
            patch("management.services.instagram_bot.send_text", side_effect=send_text),
        ):
            self.assertEqual(deliver_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_already_rewarded")
        self.assertFalse(event.provider_message_id)

    def test_real_open_service_case_suppresses_invitation(self):
        from management.ig_bot_models import IgPostSaleCase
        from management.models import InstagramBotMessage
        from management.services.ig_order_fulfillment import deliver_event

        event = self._review_event()
        source = InstagramBotMessage.objects.create(
            client=self.ig_client, sender_id=self.ig_client.igsid,
            role="user", text="Потрібен обмін", status="done",
        )
        IgPostSaleCase.objects.create(
            client=self.ig_client, order=self.order, source_message=source,
            case_type="exchange", status="in_transit",
        )
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(deliver_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_service_case_open")
        send.assert_not_called()

    def test_consumed_identity_without_reward_payload_still_blocks(self):
        from management.ig_bot_models import IgUgcRewardLifetime
        from management.services.ig_order_fulfillment import deliver_event
        from management.services.ig_ugc_rewards import _identity_digest

        event = self._review_event()
        IgUgcRewardLifetime.objects.create(
            client=self.ig_client, identity_digest=_identity_digest(self.ig_client),
            consumed_at=timezone.now(),
        )
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(deliver_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_already_rewarded")
        send.assert_not_called()

    @override_settings(
        IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="active",
        IG_UGC_IDENTITY_HMAC_KEYRING={
            "active": "active-test-identity-secret-00000000000000",
            "retained": "retained-test-identity-secret-000000000000",
        },
    )
    def test_retained_consumed_slot_cannot_hide_behind_first_empty_active_slot(self):
        from management.ig_bot_models import IgUgcRewardLifetime
        from management.services.ig_order_fulfillment import deliver_event
        from management.services.ig_ugc_rewards import (
            _identity_digest_candidates, ugc_identity_already_rewarded,
        )

        event = self._review_event()
        active, retained = _identity_digest_candidates(self.ig_client)
        IgUgcRewardLifetime.objects.create(client=self.ig_client, identity_digest=active)
        IgUgcRewardLifetime.objects.create(identity_digest=retained, consumed_at=timezone.now())
        self.assertTrue(ugc_identity_already_rewarded(self.ig_client))
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(deliver_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_already_rewarded")
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 2)
        send.assert_not_called()

    @override_settings(
        IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="active",
        IG_UGC_IDENTITY_HMAC_KEYRING={
            "active": "active-test-identity-secret-00000000000000",
            "retained": "retained-test-identity-secret-000000000000",
        },
    )
    def test_two_empty_matching_slots_are_unknown_not_consumed_or_eligible(self):
        from management.ig_bot_models import IgUgcRewardLifetime
        from management.services.ig_order_fulfillment import deliver_event
        from management.services.ig_ugc_rewards import (
            _identity_digest_candidates, ugc_identity_already_rewarded,
            ugc_identity_lifetime_conflicted,
        )

        event = self._review_event()
        active, retained = _identity_digest_candidates(self.ig_client)
        IgUgcRewardLifetime.objects.create(client=self.ig_client, identity_digest=active)
        IgUgcRewardLifetime.objects.create(identity_digest=retained)
        self.assertFalse(ugc_identity_already_rewarded(self.ig_client))
        self.assertTrue(ugc_identity_lifetime_conflicted(self.ig_client))
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(deliver_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_eligibility_unknown")
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 2)
        send.assert_not_called()

    def test_late_invitation_block_after_partial_receipt_is_ambiguous_without_replay(self):
        from management.services.ig_order_fulfillment import deliver_event

        event = self._review_event()
        blocked = False

        def reason(*_args):
            return "post_purchase_already_rewarded" if blocked else ""

        def send_text(_settings, _igsid, _text, **kwargs):
            nonlocal blocked
            with kwargs["permission_boundary_factory"]() as permitted:
                self.assertTrue(permitted)
            kwargs["provider_message_callback"]("mid.invitation.partial")
            blocked = True
            with kwargs["permission_boundary_factory"]() as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "permission boundary rejected"

        with (
            patch("management.services.ig_order_fulfillment.post_purchase_invitation_block_reason", side_effect=reason),
            patch("management.services.instagram_bot.send_text", side_effect=send_text),
        ):
            self.assertEqual(deliver_event(event.pk), "ambiguous")
        event.refresh_from_db()
        self.assertEqual(event.provider_message_id, "mid.invitation.partial")
        self.assertEqual(event.delivery_provider_message_ids, ["mid.invitation.partial"])
        self.assertIn("partial delivery before post_purchase_already_rewarded", event.last_error)
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(deliver_event(event.pk), "skipped")
        send.assert_not_called()

    @override_settings(IG_UGC_IDENTITY_HMAC_KEYRING={})
    def test_missing_identity_keyring_cannot_be_treated_as_unused_reward(self):
        from management.services.ig_order_fulfillment import deliver_event

        event = self._review_event()
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(deliver_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_eligibility_unknown")
        send.assert_not_called()

    def test_transactional_ttn_does_not_require_invitation_eligibility(self):
        from management.services.ig_order_assignments import link_order_to_client
        from management.services.ig_order_fulfillment import ensure_assignment_events, deliver_event

        assignment = link_order_to_client(self.order, client=self.ig_client, actor=self.manager)
        event = ensure_assignment_events(assignment)[0]
        with patch("management.services.ig_order_fulfillment.post_purchase_invitation_block_reason") as guard:
            self.assertEqual(deliver_event(event.pk, send=False), "planned")
        guard.assert_not_called()


class CanonicalPostPurchaseInvitationTests(TestCase):
    def setUp(self):
        from management import tests_ig_lifecycle as fixtures

        fixtures.InstagramLifecycleTests.setUp(self)

    def _review_event(self):
        from management import tests_ig_lifecycle as fixtures

        self.order.status = "done"
        self.order.tracking_number = "20450000000009"
        self.order.tracking_status_code = 9
        self.order.tracking_terminal_at = timezone.now()
        self.order.save(update_fields=["status", "tracking_number", "tracking_status_code", "tracking_terminal_at"])
        return fixtures.InstagramLifecycleTests._event(self, "delivered_review_requested", {"status_code": "9"})

    def test_unknown_entitlement_cancels_before_provider_io_and_stays_terminal(self):
        from management.services.ig_lifecycle import dispatch_lifecycle_event

        event = self._review_event()
        with (
            patch("management.services.ig_lifecycle.post_purchase_invitation_block_reason",
                  return_value="post_purchase_eligibility_unknown"),
            patch("management.services.instagram_bot.send_text") as send,
        ):
            self.assertEqual(dispatch_lifecycle_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_eligibility_unknown")
        send.assert_not_called()
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(dispatch_lifecycle_event(event.pk), "cancelled")
        send.assert_not_called()

    def test_service_opened_after_claim_blocks_the_final_provider_request(self):
        from management.services.ig_lifecycle import dispatch_lifecycle_event

        event = self._review_event()
        blocked = False

        def reason(*_args):
            return "post_purchase_service_case_open" if blocked else ""

        def send_text(_settings, _igsid, _text, **kwargs):
            nonlocal blocked
            self.assertTrue(kwargs["provider_io_started_callback"]())
            blocked = True
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=1) as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "provider request rejected"

        with (
            patch("management.services.ig_lifecycle.post_purchase_invitation_block_reason", side_effect=reason),
            patch("management.services.instagram_bot.send_text", side_effect=send_text),
        ):
            self.assertEqual(dispatch_lifecycle_event(event.pk), "cancelled")
        event.refresh_from_db()
        self.assertEqual(event.last_error, "post_purchase_service_case_open")
        self.assertFalse(event.provider_message_id)

    def test_late_invitation_block_after_partial_receipt_is_ambiguous_without_replay(self):
        from management.services.ig_lifecycle import dispatch_lifecycle_event

        event = self._review_event()
        blocked = False

        def reason(*_args):
            return "post_purchase_service_case_open" if blocked else ""

        def send_text(_settings, _igsid, _text, **kwargs):
            nonlocal blocked
            self.assertTrue(kwargs["provider_io_started_callback"]())
            with kwargs["provider_request_boundary_factory"](planned_chunk_count=2) as permitted:
                self.assertTrue(permitted)
            kwargs["provider_message_callback"]("mid.canonical.partial")
            blocked = True
            with kwargs["provider_request_boundary_factory"](
                delivered_chunk_count=1,
                provider_message_ids=("mid.canonical.partial",),
                planned_chunk_count=2,
            ) as permitted:
                self.assertFalse(permitted)
            return False, "cancelled", "provider request rejected"

        with (
            patch("management.services.ig_lifecycle.post_purchase_invitation_block_reason", side_effect=reason),
            patch("management.services.instagram_bot.send_text", side_effect=send_text),
        ):
            self.assertEqual(dispatch_lifecycle_event(event.pk), "ambiguous")
        event.refresh_from_db()
        self.assertEqual(event.provider_message_id, "mid.canonical.partial")
        self.assertIn("partial delivery before post_purchase_service_case_open", event.last_error)
        with patch("management.services.instagram_bot.send_text") as send:
            self.assertEqual(dispatch_lifecycle_event(event.pk), "ambiguous")
        send.assert_not_called()


@override_settings(
    IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID="active",
    IG_UGC_IDENTITY_HMAC_KEYRING={
        "active": "active-test-identity-secret-00000000000000",
        "retained": "retained-test-identity-secret-000000000000",
    },
)
class ManagerUgcLifetimeEligibilityTests(TestCase):
    def setUp(self):
        from management.ig_bot_models import IgClient, IgUgcRewardLifetime
        from management.services.ig_ugc_rewards import _identity_digest_candidates
        from orders.models import Order

        self.customer = IgClient.objects.create(igsid="manager-retained-lifetime")
        self.order = Order.objects.create(
            order_number="TWC-ELIGIBILITY", full_name="Buyer", phone="380501112233",
            city="Kyiv", np_office="Branch 1", total_sum="790.00", status="done",
            tracking_number="20400000000009", tracking_status_code=9,
            tracking_terminal_at=timezone.now(),
        )
        active, retained = _identity_digest_candidates(self.customer)
        self.active_slot = IgUgcRewardLifetime.objects.create(
            client=self.customer, identity_digest=active,
        )
        self.retained_slot = IgUgcRewardLifetime.objects.create(identity_digest=retained)

    def test_manager_eligibility_detects_consumed_retained_slot_after_empty_active(self):
        from management.services.ig_ugc_rewards import ugc_reward_eligibility

        self.retained_slot.consumed_at = timezone.now()
        self.retained_slot.save(update_fields=["consumed_at", "updated_at"])
        self.assertEqual(
            ugc_reward_eligibility(self.customer, assignments=[SimpleNamespace(order=self.order)]),
            (False, "already_rewarded"),
        )

    def test_manager_eligibility_reports_two_empty_matching_slots_as_conflict(self):
        from management.services.ig_ugc_rewards import ugc_reward_eligibility

        self.assertEqual(
            ugc_reward_eligibility(self.customer, assignments=[SimpleNamespace(order=self.order)]),
            (False, "lifetime_conflict"),
        )

    def test_empty_unmatched_legacy_slot_is_unknown_without_claiming_issuance(self):
        from management.ig_bot_models import IgUgcRewardLifetime
        from management.services.ig_ugc_rewards import (
            ugc_identity_already_rewarded, ugc_identity_lifetime_conflicted,
            ugc_reward_eligibility,
        )

        self.active_slot.delete()
        self.retained_slot.delete()
        legacy = IgUgcRewardLifetime.objects.create(
            client=self.customer, identity_digest="legacy-empty-unverified-identity",
        )
        self.assertFalse(ugc_identity_already_rewarded(self.customer))
        self.assertTrue(ugc_identity_lifetime_conflicted(self.customer))
        self.assertEqual(
            ugc_reward_eligibility(self.customer, assignments=[SimpleNamespace(order=self.order)]),
            (False, "lifetime_identity_unverified"),
        )
        self.assertEqual(
            post_purchase_invitation_block_reason(self.customer, self.order),
            "post_purchase_eligibility_unknown",
        )
        legacy.refresh_from_db()
        self.assertEqual(legacy.identity_digest, "legacy-empty-unverified-identity")
        self.assertIsNone(legacy.reward_id)
        self.assertIsNone(legacy.consumed_at)
        self.assertEqual(IgUgcRewardLifetime.objects.count(), 1)
