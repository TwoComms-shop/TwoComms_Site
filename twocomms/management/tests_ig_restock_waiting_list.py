"""Real source -> RS1 receipt -> exact-list consent -> physical ALL gate."""
from copy import deepcopy
from datetime import timedelta
import json
import uuid
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.models import IgCustomerTurnRevision, InstagramBotMessage
from management.services import ig_assisted_commerce as commerce
from management.services import ig_fulfillment_reviews as reviews
from management.services import ig_restock_subscriptions as restock
from management.services import ig_restock_waiting_list as waiting
from management.tests_ig_assisted_commerce import AssistedCommerceFixture
from management.tests_ig_assisted_partial_sources import PartialCommerceFixture
from orders.models import Order, PaymentAttempt


class RestockWaitingListFixture(AssistedCommerceFixture):
    def catalogue_accepted_list(self, *, accept=True):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        variant = self.variants[1]
        variant.stock = 0
        variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical("добавьте Storm, количество 1, размер L, цвет pink, крій classic")
        result, source, revision, token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertTrue(result.invitation_id, result)
        inv = restock.Invitation.objects.get(pk=result.invitation_id)
        self.assertEqual(len(inv.target["items"]), 2)
        self.sent_card(revision, token, source, result)
        restock.record_delivery(revision.pk, now=self.now)
        if not accept:
            return {"invitation": inv}
        response, source, _revision, _token = self.reply(inv)
        self.assertEqual(response.reason, "accept", response)
        return {"invitation": inv, "subscription": restock.Subscription.objects.get(accepted_answer__source=source)}

    def edit_native_variant_color(self):
        from django.urls import reverse
        with override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False):
            self.client.force_login(self.manager)
            response = self.client.post(reverse("product_catalog_api_variant_save"), json.dumps({
                "product_id": self.product.pk, "id": self.variant.pk, "color": {"id": self.variants[1].color_id}}),
                content_type="application/json", HTTP_HOST="twocomms.shop")
        self.assertEqual(response.status_code, 200, response.content)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.color_id, self.variants[1].color_id)

    def cart_update_for_list(self, case):
        from management.models import IgFollowUpTask
        source = self.source("Встигнете відправити до 20.10.2026? Це подарунок", at=self.now + timedelta(seconds=2))
        revision, _token = self.revision(source)
        result = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now + timedelta(seconds=2))
        self.assertTrue(result["ready"], result)
        request = reviews.Request.objects.get(pk=result["request_id"])
        self.assertEqual(request.envelope["scope"], "cart")
        self.assertEqual(len(request.envelope["lines"]), 2)
        rows = [{"line_id": row["line_id"], "availability": "available", "checked_quantity": row["target"]["quantity"],
            "supply_basis": "accessible_unrecorded"} for row in request.envelope["lines"]]
        decision = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), decisions_by_line=rows,
            now=self.now + timedelta(seconds=2))
        self.assertTrue(decision["ready"], decision)
        return IgFollowUpTask.objects.get(event_key=f"ig-fulfillment-update:{request.pk}:{decision['version']}")

    def dispatch_cart(self, *, unknown=False, during_http=None):
        from management.services import instagram_bot as bot, ig_fulfillment_delivery as delivery
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(state="processed", active_slot=None, claim_token="", lease_until=None)
        physical = []
        def transport(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if during_http:
                during_http()
            if unknown:
                raise TimeoutError("Actual original CART request reached the provider, outcome unknown")
            return 200, json.dumps({"message_id": "native-original-cart-list-availability"})
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=3)), \
            patch.object(bot, "get_page_token", return_value="synthetic-native-cart-token"), \
            patch.object(bot, "_provider_http", side_effect=transport):
            count = delivery.process_due_fulfillment_updates(self.settings, now=self.now + timedelta(seconds=3))
        return count, physical

    def waiting_source_case(self, *, mixed=False):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.canonical("цвет purple, размер L")
        source = self.canonical("добавьте Restock Aurora, количество 1, размер M, цвет black, крій classic" if mixed else
            "добавьте Storm, количество 1, размер L, цвет olive, крій classic")
        revision, _claim = self.revision(source)
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now)
        request, _created = commerce.create_assisted_request(revision.pk, settings_id=self.settings.pk,
            operation_id=uuid.uuid4(), expected_revision_digest=revision.snapshot_digest,
            expected_full_capture_digest=cart["capture_digest"], now=self.now)
        items = commerce._editable_items(request.envelope["snapshot"])
        for row in items:
            row.update(availability="wait", checked_quantity=None, supply_basis="incoming_confirmed", source_location="Очікуємо фізичну перевірку")
            if row["item_kind"] == "custom_apparel":
                row.update(title="Худі", unit_price="1500.00")
            row["question_answers"] = [{"question_id": question["question_id"], "state": "confirmed", "answer": "Параметри підтверджено; готовність ще не підтверджена."}
                for question in request.envelope["snapshot"]["questions"] if question["line_id"] == row["line_id"]]
        draft, _created = self.record(request, items=items)
        decision, _created = self.record(request, status="approved", latest=draft)
        return request, decision

    def list_invitation(self, *, mixed=False, text="Повідомте, коли всі позиції будуть в наявності", sent=True, outcome="response"):
        request, decision = self.waiting_source_case(mixed=mixed)
        result, source, revision, token = self.request(text)
        self.assertIsNotNone(result.invitation_id, result)
        invitation = restock.Invitation.objects.get(pk=result.invitation_id)
        self.assertTrue(waiting.is_list(invitation.target))
        if sent:
            self.sent_card(revision, token, source, result, outcome=outcome)
            restock.record_delivery(revision.pk, now=self.now)
        return {"request": request, "decision": decision, "result": result, "source": source,
            "revision": revision, "token": token, "invitation": invitation}

    def accepted_list(self, **kwargs):
        case = self.list_invitation(**kwargs)
        response, source, _revision, _token = self.reply(case["invitation"])
        self.assertEqual(response.reason, "accept", response)
        subscription = restock.Subscription.objects.get(accepted_answer__source=source)
        return {**case, "subscription": subscription}

    def physically_check(self, case, *, unavailable_index=None, checked_quantity=None, basis="accessible_unrecorded", preparation=None, now=None):
        from management.services.ig_restock_waiting_list_review import _rows
        request = _rows(case["invitation"], case.get("subscription"))[0]
        rows = [{"line_id": item["line_id"], "availability": "wait" if index == unavailable_index else "available",
            "checked_quantity": item["quantity"] if checked_quantity is None else checked_quantity,
            "supply_basis": "incoming_confirmed" if index == unavailable_index else basis,
            "source_location": "Менеджер фізично перевірив точні позиції", "preparation": preparation}
            for index, item in enumerate(case["invitation"].target["items"])]
        result = reviews.apply_fulfillment_decision(request.pk, actor=self.manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(), decisions_by_line=rows,
            now=now or self.now + timedelta(seconds=2))
        return request, result

    def dispatch(self, *, now=None, before_http=None, unknown=False):
        from management.services import instagram_bot as bot
        now = now or self.now + timedelta(seconds=3)
        InstagramBotMessage.objects.filter(client=self.client_row, role="user").update(status="done")
        IgCustomerTurnRevision.objects.filter(client=self.client_row).update(state="processed", active_slot=None, claim_token="", lease_until=None)
        physical = []
        def lookup(_settings, **kwargs):
            if before_http:
                before_http()
            return "synthetic-waiting-list-token"
        def transport(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            if unknown:
                raise TimeoutError("Actual physical boundary reached, outcome unknown")
            return 200, json.dumps({"message_id": "native-all-waiting-list-notice"})
        with patch("django.utils.timezone.now", return_value=now), patch.object(bot, "get_page_token", side_effect=lookup), \
                patch.object(bot, "_provider_http", side_effect=transport):
            count = restock.process_due_restock_notifications(self.settings, now=now)
        return count, physical


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeRestockWaitingListTests(RestockWaitingListFixture, TransactionTestCase):
    def test_real_catalogue_color_edit_between_target_capture_and_initial_baseline_cannot_mint_card(self):
        from management.services import ig_restock_waiting_list_review as list_reviews
        self.waiting_source_case(mixed=True)
        original = list_reviews.create_invitation_review
        calls = []
        def edit_then_delegate(*args, **kwargs):
            calls.append(True)
            self.edit_native_variant_color()
            return original(*args, **kwargs)
        # Only interpose the real editor; every source, configuration and
        # review producer still runs its actual native implementation.
        with patch.object(list_reviews, "create_invitation_review", side_effect=edit_then_delegate):
            result, _source, _revision, _token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertEqual(calls, [True])
        self.assertFalse(result.invitation_id, result)
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_old_assisted_wait_catalogue_row_cannot_start_new_card_after_same_pk_color_edit(self):
        self.waiting_source_case(mixed=True)
        self.edit_native_variant_color()
        result, _source, _revision, _token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertFalse(result.invitation_id, result)
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_same_variant_pk_color_edit_after_sent_card_cannot_retarget_acceptance(self):
        case = self.catalogue_accepted_list(accept=False)
        original = deepcopy(case["invitation"].target)
        self.edit_native_variant_color()
        response, _source, _revision, _token = self.reply(case["invitation"])
        self.assertNotEqual(response.reason, "accept", response)
        self.assertFalse(restock.Subscription.objects.exists())
        case["invitation"].refresh_from_db()
        self.assertEqual(case["invitation"].target, original)

    def test_same_variant_pk_color_edit_before_physical_approval_rejects_manager_available(self):
        case = self.catalogue_accepted_list()
        self.edit_native_variant_color()
        _request, result = self.physically_check(case)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "configuration_changed")
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 0)
        self.assertEqual(self.dispatch(), (0, []))

    def test_same_variant_pk_color_edit_after_all_approval_cannot_queue_notification(self):
        case = self.catalogue_accepted_list()
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.edit_native_variant_color()
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 0)
        self.assertFalse(restock.AvailabilityEvent.objects.exists())
        self.assertEqual(self.dispatch(), (0, []))

    def test_same_variant_pk_color_edit_at_socket_rejects_already_queued_all_notification(self):
        case = self.catalogue_accepted_list()
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        self.assertEqual(self.dispatch(before_http=self.edit_native_variant_color), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")

    def test_expired_decision_renewal_retains_original_accepted_configuration_after_real_color_edit(self):
        from management.services.ig_restock_waiting_list_review import _rows
        case = self.catalogue_accepted_list()
        original, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        baseline = {row["line_id"]: row["configuration_digest"] for row in original.envelope["lines"]}
        self.edit_native_variant_color()
        later = self.now + timedelta(hours=25)
        with patch("django.utils.timezone.now", return_value=later):
            self.assertEqual(restock.reconcile_subscriptions(now=later)["created"], 0)
            renewal = _rows(case["invitation"], case["subscription"])[0]
            self.assertNotEqual(renewal.pk, original.pk)
            self.assertEqual(renewal.envelope["scope"], "restock_list")
            self.assertEqual({row["line_id"]: row["configuration_digest"] for row in renewal.envelope["lines"]}, baseline)
            self.assertTrue(any(row["observation"]["configuration_digest"] != row["configuration_digest"] for row in renewal.envelope["lines"]))
            _request, result = self.physically_check(case, now=later)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "configuration_changed")
        self.assertFalse(restock.AvailabilityEvent.objects.exists())

    def test_numeric_stock_changes_keep_the_original_configuration_and_physical_all_admission(self):
        case = self.catalogue_accepted_list()
        original, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        baseline = deepcopy(original.envelope["lines"])
        self.variant.stock = 3
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 3
        self.rule.save(update_fields=["stock"])
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        count, physical = self.dispatch()
        self.assertEqual((count, len(physical)), (1, 1))
        original.refresh_from_db()
        self.assertEqual(original.envelope["lines"], baseline)

    def test_default_review_preserves_older_cart_while_exact_new_list_request_stays_reviewable(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        variant = self.variants[1]
        variant.stock = 0
        variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical("добавьте Storm, количество 1, размер L, цвет pink, крій classic")
        source = self.source("Повідомте, коли всі позиції будуть в наявності")
        revision, _token = self.revision(source)
        created = reviews.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(created["ready"], created)
        cart_request = reviews.Request.objects.get(pk=created["request_id"])
        self.assertEqual(cart_request.envelope["scope"], "cart")
        later = self.now + timedelta(seconds=1)
        with patch("django.utils.timezone.now", return_value=later):
            result = restock.prepare_request(revision, source, settings_obj=self.settings, now=later)
        self.assertTrue(result.invitation_id, result)
        inv = restock.Invitation.objects.get(pk=result.invitation_id)
        list_request = reviews.Request.objects.get(pk=inv.envelope["manager_review"]["request_id"])
        self.assertGreater(list_request.created_at, cart_request.created_at)
        cart = capture_current_selection_lines(self.client_row.pk, now=later)
        default = reviews.fulfillment_review_projection(self.client_row.pk, now=later, current_source_cart=cart)
        self.assertEqual(default["status"], "pending", default)
        self.assertEqual(default["current_request"]["id"], str(cart_request.pk), default)
        exact = reviews.fulfillment_review_projection(self.client_row.pk, now=later, request_id=list_request.pk,
            current_source_cart=cart)
        self.assertEqual(exact["status"], "pending", exact)
        self.assertEqual(exact["current_request"]["id"], str(list_request.pk), exact)
        self.assertEqual(exact["current_request"]["scope"], "restock_list_invitation")

    def test_original_pending_cart_availability_has_priority_over_overlapping_all_list(self):
        case = self.catalogue_accepted_list()
        self.cart_update_for_list(case)
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        self.assertEqual(self.dispatch(), (0, []))
        event = restock.AvailabilityEvent.objects.get(subscription=case["subscription"])
        event.task.refresh_from_db()
        self.assertEqual(event.task.skip_reason, "other_availability_send_in_progress")

    def test_native_sent_two_line_cart_does_not_mint_an_all_list_receipt_or_duplicate_send(self):
        case = self.catalogue_accepted_list()
        cart = self.cart_update_for_list(case)
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        count, physical = self.dispatch_cart()
        self.assertEqual((count, len(physical)), (1, 1))
        cart.refresh_from_db()
        self.assertEqual(cart.provider_message_id, "native-original-cart-list-availability")
        self.assertEqual(self.dispatch(now=self.now + timedelta(seconds=4)), (0, []))
        event = restock.AvailabilityEvent.objects.get(subscription=case["subscription"])
        event.task.refresh_from_db()
        self.assertEqual(event.task.skip_reason, "other_availability_receipt_requires_review")
        self.assertFalse(event.task.provider_message_id)
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")

    def test_native_unknown_two_line_cart_holds_all_list_without_replay(self):
        case = self.catalogue_accepted_list()
        self.cart_update_for_list(case)
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        _count, physical = self.dispatch_cart(unknown=True)
        self.assertEqual(len(physical), 1)
        self.assertEqual(self.dispatch(now=self.now + timedelta(seconds=4)), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")

    def test_real_cart_processing_boundary_holds_overlapping_list_without_a_cloned_mid(self):
        from management.services.ig_restock_waiting_list_coordination import restock_update_coordination
        case = self.catalogue_accepted_list()
        cart = self.cart_update_for_list(case)
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        event = restock.AvailabilityEvent.objects.select_related("subscription__accepted_answer__invitation").get(subscription=case["subscription"])
        seen = []
        def inspect_native_processing():
            cart.refresh_from_db()
            self.assertEqual(cart.status, "processing")
            self.assertEqual(cart.attempt_count, 1)
            seen.append(restock_update_coordination(event, self.settings, now=self.now + timedelta(seconds=3)))
        count, physical = self.dispatch_cart(during_http=inspect_native_processing)
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertEqual(seen, [{"reason": "other_availability_send_in_progress"}])
        event.task.refresh_from_db()
        self.assertFalse(event.task.provider_message_id)

    def _actor_revocation_case(self, mode, *, at_socket):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group, Permission
        from management.bot_access import (META_REVIEWER_GROUP_NAME, OPERATE_IG_BOT_PERMISSION,
            VIEW_IG_CONVERSATION_PII_PERMISSION)
        from management.tests_ig_clients_ui import _operator
        self.manager = _operator("waiting-list-physical-approver", OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION)
        case = self.accepted_list()
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        def revoke():
            principal = get_user_model().objects.get(pk=self.manager.pk)
            if mode == "inactive":
                principal.is_active = False
                principal.save(update_fields=["is_active"])
            elif mode == "reviewer":
                principal.groups.add(Group.objects.get_or_create(name=META_REVIEWER_GROUP_NAME)[0])
            else:
                principal.user_permissions.remove(Permission.objects.get(content_type__app_label="management",
                    codename=VIEW_IG_CONVERSATION_PII_PERMISSION.split(".", 1)[1]))
        if at_socket:
            self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
            count, physical = self.dispatch(before_http=revoke)
            self.assertEqual((count, physical), (0, []))
        else:
            revoke()
            self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 0)
            self.assertFalse(restock.AvailabilityEvent.objects.exists())
            self.assertEqual(self.dispatch(), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")

    def test_deactivated_physical_approver_cannot_create_notification(self):
        self._actor_revocation_case("inactive", at_socket=False)

    def test_reviewer_denied_physical_approver_cannot_create_notification(self):
        self._actor_revocation_case("reviewer", at_socket=False)

    def test_revoked_pii_capability_cannot_create_notification(self):
        self._actor_revocation_case("capability", at_socket=False)

    def test_deactivated_physical_approver_is_rechecked_before_socket(self):
        self._actor_revocation_case("inactive", at_socket=True)

    def test_reviewer_denied_physical_approver_is_rechecked_before_socket(self):
        self._actor_revocation_case("reviewer", at_socket=True)

    def test_revoked_pii_capability_is_rechecked_before_socket(self):
        self._actor_revocation_case("capability", at_socket=True)

    def test_source_owned_multiple_catalogue_waiting_items_use_real_targets_and_all_gate(self):
        from product_catalog.models import ProductInventoryPolicy, VariantSizeRule
        variant = self.variants[1]
        variant.stock = 0
        variant.save(update_fields=["stock"])
        ProductInventoryPolicy.objects.create(product=self.products[1], source="catalog_variant")
        VariantSizeRule.objects.create(variant=variant, fit_code="classic", size="L", stock=0, is_enabled=True)
        self.canonical("добавьте Storm, количество 2, размер L, цвет pink, крій classic")
        result, source, revision, token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertTrue(result.invitation_id, result)
        inv = restock.Invitation.objects.get(pk=result.invitation_id)
        self.assertEqual({item["catalog_target"]["product_id"] for item in inv.target["items"]}, {row.pk for row in self.products})
        self.assertEqual(sorted(item["quantity"] for item in inv.target["items"]), [1, 2])
        self.assertTrue(all(item["source"]["kind"] == "catalog_selection" for item in inv.target["items"]))
        self.sent_card(revision, token, source, result)
        restock.record_delivery(revision.pk, now=self.now)
        response, source, _revision, _token = self.reply(inv)
        self.assertEqual(response.reason, "accept", response)
        case = {"invitation": inv, "subscription": restock.Subscription.objects.get(accepted_answer__source=source)}
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        count, physical = self.dispatch()
        self.assertEqual((count, len(physical)), (1, 1))

    def test_exact_custom_list_has_one_all_items_button_and_no_subscription_before_actual_receipt_answer(self):
        case = self.list_invitation(sent=False)
        inv = case["invitation"]
        self.assertEqual(len(case["result"].quick_replies), 1)
        self.assertIn("УСІХ", inv.body)
        self.assertEqual(len(inv.target["items"]), 2)
        self.assertEqual({row["size"] for row in inv.target["items"]}, {"L"})
        self.assertTrue(all(row["catalog_target"] is None for row in inv.target["items"]))
        response, _source, _revision, _token = self.reply(inv)
        self.assertNotEqual(response.reason, "accept")
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())

    def test_mixed_catalogue_and_custom_list_retains_both_native_kinds_and_exact_quantities(self):
        case = self.accepted_list(mixed=True)
        self.assertEqual({item["item_kind"] for item in case["invitation"].target["items"]}, {"catalog_apparel", "custom_apparel"})
        self.assertEqual(sum(item["quantity"] for item in case["invitation"].target["items"]), 2)
        self.assertEqual(restock.Subscription.objects.count(), 1)

    def test_all_physical_rows_required_wait_or_estimated_date_never_creates_notification(self):
        case = self.accepted_list()
        _request, result = self.physically_check(case, unavailable_index=1,
            preparation={"min_days": 7, "max_days": 10, "unit": "calendar"})
        self.assertTrue(result["ready"], result)
        reconciled = restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        self.assertEqual(reconciled["created"], 0)
        self.assertFalse(restock.AvailabilityEvent.objects.exists())
        count, sent = self.dispatch()
        self.assertEqual((count, sent), (0, []))

    def test_actual_all_checked_rows_send_once_without_creating_stock_reservation_or_order(self):
        case = self.accepted_list()
        _request, result = self.physically_check(case)
        self.assertTrue(result["ready"], result)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 0)
        count, physical = self.dispatch()
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertIn("УСІХ", physical[0]["message"]["text"])
        again, repeated = self.dispatch()
        self.assertEqual((again, repeated), (0, []))
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PaymentAttempt.objects.exists())
        dto = waiting.waiting_list_projection(self.client_row.pk, now=self.now + timedelta(seconds=3))
        self.assertTrue(dto["lists"][0]["notification"]["native_receipt_confirmed"], dto)

    def test_changing_cart_before_acceptance_invalidates_card_but_after_acceptance_preserves_original_interest(self):
        case = self.list_invitation()
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.canonical("измените позицию 2, количество 2")
        current = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(current["lines"][1]["fields"]["quantity"]["value"], 2, current)
        response, _source, _revision, _token = self.reply(case["invitation"])
        self.assertNotEqual(response.reason, "accept")
        self.assertFalse(restock.Subscription.objects.exists())

    def test_accepted_list_is_independent_of_a_later_active_cart(self):
        case = self.accepted_list()
        frozen = deepcopy(case["invitation"].target)
        self.canonical("измените позицию 2, количество 2")
        _request, result = self.physically_check(case)
        self.assertTrue(result["ready"], result)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        count, physical = self.dispatch()
        self.assertEqual((count, len(physical)), (1, 1))
        case["invitation"].refresh_from_db()
        self.assertEqual(case["invitation"].target, frozen)

    def test_bare_yes_and_partial_wait_choice_never_become_list_consent(self):
        case = self.list_invitation()
        response, _source, _revision, _token = self.reply(case["invitation"], native=False)
        self.assertFalse(response.handled)
        self.assertFalse(restock.Subscription.objects.exists())

    def test_unknown_native_invitation_receipt_cannot_accept_or_send(self):
        case = self.list_invitation(outcome="unknown")
        response, _source, _revision, _token = self.reply(case["invitation"])
        self.assertNotEqual(response.reason, "accept")
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertFalse(restock.Receipt.objects.exists())

    def test_manager_checked_quantity_below_accepted_quantity_is_rejected(self):
        case = self.accepted_list()
        _request, result = self.physically_check(case, checked_quantity=0)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "physical_availability_unverified")
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 0)

    def test_elapsed_standard_window_holds_even_when_all_physically_ready(self):
        case = self.accepted_list()
        _request, result = self.physically_check(case)
        self.assertTrue(result["ready"], result)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        count, physical = self.dispatch(now=self.now + timedelta(hours=23, seconds=2))
        self.assertEqual((count, physical), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")

    def test_new_inbound_before_socket_prevents_send_and_does_not_consume_subscription(self):
        case = self.accepted_list()
        _request, result = self.physically_check(case)
        self.assertTrue(result["ready"], result)
        restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        count, physical = self.dispatch(before_http=lambda: self.source("Уточнення", at=self.now + timedelta(seconds=3)))
        self.assertEqual((count, physical), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")

    def test_unknown_send_never_replays_or_mints_a_native_receipt(self):
        case = self.accepted_list()
        _request, result = self.physically_check(case)
        self.assertTrue(result["ready"], result)
        restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        _count, physical = self.dispatch(unknown=True)
        self.assertEqual(len(physical), 1)
        self.assertEqual(self.dispatch(), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "unknown")

    def test_revocation_requires_actual_ack_card_and_stops_scheduled_send(self):
        case = self.list_invitation()
        result, source, revision, token = self.reply(case["invitation"])
        self.assertEqual(result.reason, "accept", result)
        self.sent_card(revision, token, source, result, mid="native-all-list-ack", now=source.provider_created_at)
        response, _source, _revision, _token = self.reply(case["invitation"], action="revoke", reply_to="native-all-list-ack", at=self.now + timedelta(seconds=2))
        self.assertEqual(response.reason, "revoke", response)
        self.assertEqual(restock.Subscription.objects.get().state, "revoked")
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=3))["created"], 0)

    def test_source_edit_invalidates_accepted_list_without_a_provider_call(self):
        case = self.accepted_list()
        first = case["request"].source
        first.text = "Змінені початкові параметри"
        first.save(update_fields=["text"])
        with patch("management.services.instagram_bot._provider_http") as physical:
            self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 0)
        physical.assert_not_called()

    def test_current_custom_wish_quantity_change_requires_fresh_manager_source_before_invitation(self):
        self.waiting_source_case()
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.canonical("измените позицию 2, количество 3")
        current = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(current["lines"][1]["fields"]["quantity"]["value"], 3, current)
        result, _source, _revision, _token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertIsNone(result.invitation_id)
        self.assertFalse(restock.Subscription.objects.exists())

    def _assert_new_catalogue_row_requires_fresh_full_manager_scope(self, *, stock):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.waiting_source_case()
        self.variant.stock = stock
        self.variant.save(update_fields=["stock"])
        self.rule.stock = stock
        self.rule.save(update_fields=["stock"])
        self.canonical("добавьте Restock Aurora, количество 1, размер M, цвет black, крій classic")
        current = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(len(current["lines"]), 3, current)
        result, _source, _revision, _token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertIsNone(result.invitation_id, result)
        self.assertEqual(result.reason, "waiting_list_capture_unverified")
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_added_unavailable_catalogue_row_cannot_disappear_from_an_older_assisted_all_list(self):
        self._assert_new_catalogue_row_requires_fresh_full_manager_scope(stock=0)

    def test_added_ready_catalogue_row_still_requires_reviewed_full_mixed_wish_scope(self):
        self._assert_new_catalogue_row_requires_fresh_full_manager_scope(stock=3)

    def test_removed_waiting_row_cannot_be_reintroduced_from_old_manager_approval(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.waiting_source_case()
        self.canonical("удалите позицию 2")
        current = capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertEqual(len(current["lines"]), 1, current)
        result, _source, _revision, _token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertIsNone(result.invitation_id, result)
        self.assertFalse(restock.Subscription.objects.exists())

    def test_reviewed_ready_row_is_excluded_only_with_complete_current_manager_scope(self):
        request, approved = self.waiting_source_case()
        # _editable_items supplies only the supported manager input fields.
        from management.services.ig_assisted_commerce import _editable_items
        editable = _editable_items(request.envelope["snapshot"])
        approved_rows = {row["line_id"]: row for row in approved.envelope["snapshot"]["items"]}
        for row in editable:
            row.update({key: deepcopy(approved_rows[row["line_id"]][key]) for key in row})
        editable[0].update(availability="ready", checked_quantity=editable[0]["quantity"], supply_basis="accessible_unrecorded")
        draft, _created = self.record(request, items=editable, latest=approved)
        current, _created = self.record(request, status="approved", latest=draft)
        result, _source, _revision, _token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertTrue(result.invitation_id, result)
        inv = restock.Invitation.objects.get(pk=result.invitation_id)
        self.assertEqual([row["line_id"] for row in inv.target["items"]], [row["line_id"]
            for row in current.envelope["snapshot"]["items"] if row["availability"] == "wait"])

    def test_expired_physical_manager_check_is_rechecked_at_actual_socket_boundary(self):
        case = self.accepted_list()
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))
        decision = reviews.Decision.objects.get(request_id=case["invitation"].envelope["manager_review"]["request_id"])
        with patch.object(reviews, "FRESHNESS", timedelta(seconds=2)):
            count, physical = self.dispatch(now=decision.created_at + timedelta(seconds=3))
        self.assertEqual((count, physical), (0, []))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "active")

    def test_list_projection_is_read_only_and_does_not_expose_native_payloads(self):
        self.accepted_list()
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as physical:
            dto = waiting.waiting_list_projection(self.client_row.pk, now=self.now + timedelta(seconds=1))
        physical.assert_not_called()
        self.assertEqual(len(dto["lists"]), 1, dto)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")) for row in queries))
        encoded = json.dumps(dto)
        self.assertNotIn("RS1:", encoded)
        self.assertNotIn("envelope_hmac", encoded)
        self.assertNotIn("provider_message_id", encoded)
        self.assertEqual(dto["native_future_capability"], "unverified")

    def test_shared_agreement_descriptor_retains_actual_acceptance_inside_24_selects(self):
        self.accepted_list()
        with CaptureQueriesContext(connection) as queries, patch("management.services.instagram_bot._provider_http") as provider:
            dto = waiting.waiting_list_projection(self.client_row.pk, now=self.now + timedelta(seconds=1),
                limit=1, include_manager_review=False, max_read_queries=24)
        provider.assert_not_called()
        self.assertEqual(len(dto["lists"]), 1, dto)
        self.assertTrue(dto["coverage_complete"], dto)
        self.assertEqual(dto["lists"][0]["agreement"]["state"], "accepted", dto)
        self.assertTrue(dto["lists"][0]["agreement"]["invitation_receipt_confirmed"])
        self.assertEqual(dto["lists"][0]["manager_review"]["state"], "unverified")
        self.assertFalse(dto["lists"][0]["notification"]["native_receipt_confirmed"])
        self.assertLessEqual(dto["diagnostics"]["read_queries"], 24)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")) for row in queries))

    def test_source_owned_language_is_frozen_for_all_three_list_languages(self):
        self.waiting_source_case()
        for text, locale, all_word in (
            ("Повідомте, коли всі позиції будуть в наявності", "uk", "УСІХ"),
            ("Сообщите, когда все позиции будут в наличии", "ru", "ВСЕХ"),
            ("Notify me when all items are available", "en", "ALL"),
            ("Пожалуйста, сообщите, когда все позиции будут в наличии", "ru", "ВСЕХ"),
            ("Please notify me when all items are available", "en", "ALL"),
        ):
            with self.subTest(locale=locale):
                result, _source, _revision, _token = self.request(text)
                self.assertTrue(result.invitation_id, result)
                inv = restock.Invitation.objects.get(pk=result.invitation_id)
                self.assertEqual(inv.locale, locale)
                self.assertIn(all_word, inv.body)
                self.assertIn(all_word, restock._copy_for(inv, "accepted"))
                self.assertIn(all_word, restock._copy_for(inv, "notification"))

    def test_compound_customer_questions_remain_with_normal_response_plan_without_list_consent(self):
        self.waiting_source_case()
        result, _source, _revision, _token = self.request(
            "Повідомте, коли всі позиції будуть в наявності; скільки коштує доставка і чи є знижка?")
        self.assertFalse(result.handled, result)
        self.assertEqual(result.reason, "mixed_request")
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_long_exact_list_is_never_truncated_or_changed_to_single_active_item(self):
        self.waiting_source_case()
        # A real manager revision expands the exact descriptions; no synthetic
        # invitation, source or receipt is accepted by this test.
        request = commerce.latest_assisted_request(self.client_row.pk)
        latest = commerce._models()[1].objects.filter(request=request).order_by("-version").first()
        items = commerce._editable_items(request.envelope["snapshot"])
        for row in items:
            row.update(title="Фізично очікувана особлива модель " * 5, availability="wait", unit_price="1500.00",
                checked_quantity=None, supply_basis="incoming_confirmed", source_location="Майбутня перевірка")
            row["question_answers"] = [{"question_id": q["question_id"], "state": "confirmed", "answer": "Параметр підтверджено"}
                for q in request.envelope["snapshot"]["questions"] if q["line_id"] == row["line_id"]]
        draft, _created = self.record(request, items=items, latest=latest)
        self.record(request, status="approved", latest=draft)
        result, _source, _revision, _token = self.request("Повідомте, коли всі позиції будуть в наявності")
        self.assertEqual(result.reason, "waiting_list_single_card_bound", result)
        self.assertIn("підписку ще не створено", result.reply_text)
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False)
class NativeDeferredRestockWaitingListTests(PartialCommerceFixture, TransactionTestCase):
    physically_check = RestockWaitingListFixture.physically_check
    dispatch = RestockWaitingListFixture.dispatch

    def test_real_paid_partial_remainder_needs_its_own_positive_consent_then_all_manager_ready(self):
        original = self.selected_case(line_count=3)
        self.assertFalse(restock.Subscription.objects.exists())
        paid = self.pay_prepared(self.prepare_selection(original["selection"]))
        self.assertEqual(Order.objects.count(), 1)
        result, source, revision, token = self.request("Повідомте, коли всі решта позицій будуть в наявності")
        self.assertTrue(result.invitation_id, result)
        inv = restock.Invitation.objects.get(pk=result.invitation_id)
        self.assertEqual(len(inv.target["items"]), 2)
        waiting_ids = {row["line_id"] for row in original["decision"].envelope["snapshot"]["items"] if row["availability"] == "wait"}
        self.assertEqual({row["line_id"] for row in inv.target["items"]}, waiting_ids)
        self.assertFalse(restock.Subscription.objects.exists())
        self.sent_card(revision, token, source, result, mid="native-paid-remainder-invitation")
        restock.record_delivery(revision.pk, now=self.now)
        response, source, _revision, _token = self.reply(inv)
        self.assertEqual(response.reason, "accept", response)
        case = {"invitation": inv, "subscription": restock.Subscription.objects.get(accepted_answer__source=source)}
        _request, checked = self.physically_check(case, unavailable_index=1)
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 0)
        # Each existing fulfilment request has one immutable decision. A later
        # fresh physical check uses its real renewal request, never overwrites
        # the prior WAIT or treats that choice as consent.
        self.now += timedelta(hours=25)
        self.source("Перевірте, будь ласка, погоджений список", at=self.now)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now)["created"], 0)
        _request, checked = self.physically_check(case)
        self.assertTrue(checked["ready"], checked)
        self.now += restock.CHECK_INTERVAL
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        count, physical = self.dispatch(now=self.now + timedelta(seconds=3))
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertEqual(Order.objects.get().pk, paid.pk)
        self.assertEqual(PaymentAttempt.objects.count(), 1)
