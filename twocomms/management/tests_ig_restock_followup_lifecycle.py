"""Task-origin consent stays independent and requires its own physical ALL review."""
from datetime import timedelta

from django.test import TransactionTestCase, override_settings

from management.services import ig_restock_subscriptions as restock
from management import tests_ig_restock_followup as producer
from management.tests_ig_assisted_commerce import AssistedCommerceFixture
from management.tests_ig_restock_waiting_list import RestockWaitingListFixture


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    SITE_BASE_URL="https://twocomms.test")
class NativeFollowupLifecycleTests(AssistedCommerceFixture, TransactionTestCase):
    reset_sequences = True
    manager_wait = producer.RestockFollowupTests.manager_wait
    dispatch_offer = producer.RestockFollowupTests.dispatch

    def test_accepted_list_survives_next_cart_and_notifies_only_after_own_all_physical_review(self):
        task, original, source = self.manager_wait()
        self.assertEqual(self.dispatch_offer()[0], 1)
        invitation = restock.Invitation.objects.get(request_source=source)
        response, answer, _revision, _token = self.reply(invitation)
        self.assertEqual(response.reason, "accept", response)
        subscription = restock.Subscription.objects.get(accepted_answer__source=answer)
        original_target = invitation.target
        self.canonical("количество 2", at=self.now + timedelta(seconds=1))
        self.assertFalse(restock.AvailabilityEvent.objects.exists())
        # The real reconciliation worker creates a separate ALL review;
        # the original CART WAIT review cannot stand in for that approval.
        initial = self.now + timedelta(seconds=2)
        self.assertEqual(restock.reconcile_subscriptions(now=initial), {"checked": 1, "created": 0})
        # A new cart is not permission to replace or cancel the agreed list.
        case = {"invitation": invitation, "subscription": subscription}
        request, checked = RestockWaitingListFixture.physically_check(self, case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(request.envelope["scope"], "restock_list")
        self.assertNotEqual(request.pk, original.pk)
        self.assertEqual(self.variant.stock, 0)
        later = initial + restock.CHECK_INTERVAL
        self.assertEqual(restock.reconcile_subscriptions(now=later)["created"], 1)
        count, physical = RestockWaitingListFixture.dispatch(self, now=later + timedelta(seconds=1))
        self.assertEqual((count, len(physical)), (1, 1))
        subscription.refresh_from_db()
        invitation.refresh_from_db()
        self.assertEqual(subscription.state, "sent")
        self.assertEqual(invitation.target, original_target)
        self.assertEqual(RestockWaitingListFixture.dispatch(self), (0, []))

    def test_bad_old_receipt_projection_does_not_starve_newer_checkpoint(self):
        from unittest.mock import patch
        from management.models import IgFollowUpTask
        from management.services import ig_fulfillment_delivery as delivery, ig_restock_followup as followup
        first, _request, _source = self.manager_wait()
        with patch.object(followup, "record_task_delivery", return_value=False):
            self.assertEqual(self.dispatch_offer()[0], 1)
        first.refresh_from_db()
        pointer = dict(first.manager_context)
        pointer[followup.TASK_KEY] = {"invitation_id": "invalid-uuid"}
        IgFollowUpTask.objects.filter(pk=first.pk).update(manager_context=pointer)
        self.canonical("количество 2", at=self.now)
        second, _request, _source = self.manager_wait()
        with patch.object(followup, "record_task_delivery", return_value=False):
            self.assertEqual(self.dispatch_offer()[0], 1)
        IgFollowUpTask.objects.filter(pk__in=[first.pk, second.pk]).update(next_attempt_at=None)
        self.assertFalse(restock.Receipt.objects.exists())
        self.assertEqual(delivery.process_due_fulfillment_updates(self.settings, now=self.now, limit=1), 0)
        first.refresh_from_db()
        self.assertGreater(first.next_attempt_at, self.now)
        # Even once the bad row is due again, untouched newer checkpoints
        # must take priority over repeatedly failing historic projections.
        self.assertEqual(delivery.process_due_fulfillment_updates(self.settings,
            now=self.now + delivery.CHECK_INTERVAL, limit=1), 0)
        self.assertTrue(restock.Receipt.objects.filter(followup_task_id=second.pk).exists())
        self.assertFalse(restock.Receipt.objects.filter(followup_task_id=first.pk).exists())

    def test_unrelated_active_interest_does_not_block_a_new_variant(self):
        from copy import deepcopy
        from management.services import ig_restock_followup as followup
        _task, _request, source = self.manager_wait()
        self.assertEqual(self.dispatch_offer()[0], 1)
        invitation = restock.Invitation.objects.get(request_source=source)
        response, _answer, _revision, _token = self.reply(invitation)
        self.assertEqual(response.reason, "accept", response)
        self.assertEqual(followup.subscription_overlap_reason(self.client_row.pk, invitation.target,
            self.settings, now=self.now), "waiting_list_existing_subscription")
        unrelated = deepcopy(invitation.target)
        unrelated["items"][0]["catalog_target"]["color_variant_id"] = self.variants[1].pk
        self.assertEqual(followup.subscription_overlap_reason(self.client_row.pk, unrelated,
            self.settings, now=self.now), "")
