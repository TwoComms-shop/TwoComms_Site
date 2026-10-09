"""Catalog observations are not customer subscriptions or future send grants."""
from copy import deepcopy
from datetime import datetime, timedelta
import os
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.test import TestCase, override_settings

from management.models import (
    IgClient, IgCommercialEpisode, IgDeal, IgFollowUpTask, IgFunnelResetAudit, IgPaymentProjection,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services import bot_followups as followups
from management.services.ig_funnel_journal import remember_stock_gap
from management.services.ig_turn_intent import purpose_blockers
from product_catalog.models import VariantSizeRule
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption, ProductStatus


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class RestockConsentBoundaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = datetime(2026, 10, 9, 11, tzinfo=ZoneInfo("Europe/Kyiv"))
        clock = patch("django.utils.timezone.now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.settings = InstagramBotSettings.load()
        self.settings.is_enabled = True
        self.settings.ig_user_id = "1"
        self.settings.save(update_fields=["is_enabled", "ig_user_id", "updated_at"])
        category = Category.objects.create(name="Restock shirts", slug="restock-boundary")
        self.product = Product.objects.create(title="Exact requested shirt", slug="restock-exact",
            category=category, price=880, status=ProductStatus.PUBLISHED)
        ProductFitOption.objects.create(product=self.product, code="classic", label="Classic", is_active=True)
        color = Color.objects.create(name="Restock black", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(product=self.product, color=color, stock=1, slug="restock-black")
        self.rule = VariantSizeRule.objects.create(variant=self.variant, fit_code="classic", size="M", is_enabled=True, stock=1)
        self.customer = self.customer_row("restock-client")

    def customer_row(self, identity):
        customer = IgClient.objects.create(igsid=identity, stage=IgClient.Stage.QUALIFYING,
            last_user_message_at=self.now, current_product=self.product, current_size="M",
            sales_context={"assisted_checkout_selection": {"product_id": self.product.pk,
                "color_variant_id": self.variant.pk, "fit_option_code": "classic"}})
        self.remember_gap(customer)
        return customer

    def remember_gap(self, customer):
        remember_stock_gap(customer, product_id=self.product.pk, variant_id=self.variant.pk,
            size="M", fit_code="classic", option_values={"fit": "classic"})

    def event(self, **changes):
        values = dict(product_id=self.product.pk, variant_id=self.variant.pk, size="M",
            fit_code="classic", option_values={"fit": "classic"},
            source_revision=f"product_catalog:{followups.variant_inventory_revision(self.variant.pk)}",
            occurred_at=self.now)
        values.update(changes)
        return followups.materialize_restock_inventory_event(**values)

    def review(self):
        return IgFollowUpTask.objects.get(client=self.customer, reason="restock_permission_review")

    def legacy_task(self, **changes):
        values = dict(client=self.customer, kind="qualification", reason="restock_wait",
            due_at=self.now, meta_window_deadline=self.now + timedelta(hours=23),
            message_text="The size is back", level=1, trigger="event",
            event_key=f"legacy-restock-{IgFollowUpTask.objects.count()}",
            event_occurred_at=self.now, policy_started_at=self.now,
            event_payload={"event": "restock_available", "product_id": self.product.pk,
                "variant_id": self.variant.pk, "size": "M", "fit_code": "classic",
                "option_values": {"fit": "classic"}})
        values.update(changes)
        return IgFollowUpTask.objects.create(**values)

    def test_exact_catalog_observation_creates_only_bounded_internal_review_and_keeps_gap(self):
        before = deepcopy(self.customer.sales_context)
        with patch("management.services.instagram_bot._provider_http") as http:
            self.assertEqual(self.event(), 1)
            self.assertEqual(followups.process_due_followups(self.settings, now=self.now), 0)
        http.assert_not_called()
        task = self.review()
        self.assertTrue(followups.is_restock_permission_review(task))
        self.assertEqual(task.kind, "manager_task")
        self.assertEqual(task.manager_context, {"restock_review": task.event_payload})
        self.assertEqual(task.event_payload["permission_status"], "unverified")
        self.assertIs(task.event_payload["subscription_verified"], False)
        self.assertNotIn(self.customer.igsid, str(task.manager_context))
        self.assertEqual(task.discount_percent, 0)
        self.assertIsNone(task.deal_id)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, before)

    def test_callback_time_and_gap_refresh_do_not_create_second_review_or_rewrite_first_source(self):
        self.assertEqual(self.event(), 1)
        first = self.review()
        immutable = (first.event_key, deepcopy(first.event_payload), first.event_occurred_at)
        self.now += timedelta(seconds=10)
        self.remember_gap(self.customer)
        self.assertEqual(self.event(), 0)
        first.refresh_from_db()
        self.assertEqual((first.event_key, first.event_payload, first.event_occurred_at), immutable)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_stock_flapping_and_terminal_review_do_not_replay(self):
        self.assertEqual(self.event(), 1)
        task = self.review()
        source = deepcopy(task.event_payload)
        task.status = "completed"
        task.save(update_fields=["status", "updated_at"])
        self.now += timedelta(minutes=1)
        self.rule.stock = 0
        self.rule.save(update_fields=["stock", "updated_at"])
        self.assertEqual(self.event(), 0)
        self.now += timedelta(minutes=1)
        self.rule.stock = 1
        self.rule.save(update_fields=["stock", "updated_at"])
        self.assertEqual(self.event(), 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.event_payload, source)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_current_unavailable_or_missing_exact_size_rule_is_not_restock_fact(self):
        for changes in ({"stock": 0}, {"stock": 1, "is_enabled": False}):
            with self.subTest(changes=changes):
                for key, value in changes.items():
                    setattr(self.rule, key, value)
                self.rule.save(update_fields=[*changes, "updated_at"])
                self.assertEqual(self.event(), 0)
        self.rule.delete()
        self.assertEqual(self.event(), 0)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_wrong_origin_stale_hash_future_naive_and_malformed_inventory_are_rejected(self):
        valid = f"product_catalog:{followups.variant_inventory_revision(self.variant.pk)}"
        for changes in ({"source_revision": "manual:" + valid.split(":")[1]},
                {"source_revision": "product_catalog:" + "0" * 64},
                {"occurred_at": self.now + timedelta(seconds=1)},
                {"occurred_at": self.now.replace(tzinfo=None)},
                {"product_id": True}, {"variant_id": self.variant.pk + 100000},
                {"option_values": {"fit": "different"}}, {"option_values": {"fit": []}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.event(**changes), 0)
        self.product.status = "draft"
        self.product.save(update_fields=["status", "updated_at"])
        self.assertEqual(self.event(), 0)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_changed_live_selection_rejects_stale_candidate_without_clearing_new_gap(self):
        stale = IgClient.objects.get(pk=self.customer.pk)
        self.customer.current_size = "L"
        self.customer.save(update_fields=["current_size", "updated_at"])
        remember_stock_gap(self.customer, product_id=self.product.pk, variant_id=self.variant.pk,
            size="L", fit_code="classic", option_values={"fit": "classic"})
        new_context = deepcopy(self.customer.sales_context)
        task = followups.materialize_restock(stale, product_id=self.product.pk, variant_id=self.variant.pk,
            size="M", fit_code="classic", option_values={"fit": "classic"},
            source_revision=f"product_catalog:{followups.variant_inventory_revision(self.variant.pk)}",
            now=self.now, occurred_at=self.now)
        self.assertIsNone(task)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, new_context)

    def test_internal_review_does_not_cancel_unrelated_pending_work(self):
        other = IgFollowUpTask.objects.create(client=self.customer, due_at=self.now,
            kind="fulfillment", reason="paid_missing_delivery", message_text="Delivery details")
        self.assertEqual(self.event(), 1)
        other.refresh_from_db()
        self.assertEqual(other.status, "pending")
        self.assertEqual(other.message_text, "Delivery details")

    def test_pause_optout_and_privacy_fences_prevent_materialization(self):
        for field, value in (("bot_paused", True), ("manager_takeover", True),
                ("opted_out_at", self.now), ("hidden_at", self.now),
                ("privacy_erasure_started_at", self.now), ("is_blocked", True)):
            with self.subTest(field=field):
                setattr(self.customer, field, value)
                self.customer.save(update_fields=[field, "updated_at"])
                self.assertEqual(self.event(), 0)
                setattr(self.customer, field, False if isinstance(value, bool) else None)
                self.customer.save(update_fields=[field, "updated_at"])
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_fresh_unanalyzed_service_complaint_prevents_internal_restock_review(self):
        InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", mid="restock-complaint-mid",
            provider_namespace="instagram_login:1", provider_created_at=self.now,
            text="You said shipping was 90 but I paid 120. My first impression is bad.")
        self.assertEqual(self.event(), 0)
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_reset_rejects_old_gap_and_creates_new_identity_for_new_interest(self):
        self.assertEqual(self.event(), 1)
        old_key = self.review().event_key
        self.now += timedelta(minutes=1)
        reset = IgFunnelResetAudit.objects.create(client=self.customer, reason="new source era")
        self.assertEqual(self.event(), 0)
        self.remember_gap(self.customer)
        self.assertEqual(self.event(), 1)
        new = IgFollowUpTask.objects.get(client=self.customer,
            event_payload__reset_id=reset.pk, reason="restock_permission_review")
        self.assertNotEqual(new.event_key, old_key)

    def test_matching_interest_after_non_gap_and_existing_review_rows_is_not_starved(self):
        self.assertEqual(self.event(), 1)
        old = self.review()
        for index in range(101):
            IgClient.objects.create(igsid=f"non-gap-restock-{index}", current_product=self.product,
                current_size="M", last_user_message_at=self.now)
        later = self.customer_row("later-exact-restock-request")
        self.assertEqual(self.event(), 1)
        self.assertTrue(IgFollowUpTask.objects.filter(client=later, reason="restock_permission_review").exists())
        self.assertEqual(self.event(), 0)
        self.assertEqual(IgFollowUpTask.objects.filter(reason="restock_permission_review").count(), 2)
        old.refresh_from_db()
        self.assertTrue(followups.is_restock_permission_review(old))

    def test_legacy_restocks_are_skipped_inside_meta_window_without_http(self):
        rows = [self.legacy_task(), self.legacy_task(trigger="time", event_payload={}),
            self.legacy_task(event_payload={"event": "invoice_expired"}),
            self.legacy_task(reason="other", event_payload={"event": "restock_available"}),
            self.legacy_task(reason="restock_f1", event_payload={})]
        with patch("management.services.instagram_bot._provider_http") as http, patch(
                "management.services.instagram_bot.send_text") as send:
            self.assertEqual(followups.process_due_followups(self.settings, now=self.now), 0)
            self.assertEqual(followups.process_due_followups(self.settings, now=self.now), 0)
        http.assert_not_called()
        send.assert_not_called()
        for task in rows:
            task.refresh_from_db()
            self.assertEqual(task.status, "skipped")
            self.assertEqual(task.skip_reason, "restock_purpose_unverified")
            self.assertEqual(task.provider_message_id, "")

    def test_old_claim_rechecks_purpose_at_physical_boundary_and_keeps_partial_mid(self):
        task = self.legacy_task(status="processing", claim_token="pre-existing-claim",
            claim_until=self.now + timedelta(minutes=4))
        with followups._followup_promotion_provider_boundary(task.pk, task.claim_token,
                checked_at=self.now, provider_message_ids=["restock-first-native-mid"], delivered_chunk_count=1) as permission:
            self.assertFalse(permission)
            self.assertEqual(permission.reason, "restock_purpose_unverified")
        task.refresh_from_db()
        self.assertEqual(task.provider_message_id, "restock-first-native-mid")
        self.assertEqual(task.status, "ambiguous")
        with patch("management.services.instagram_bot._provider_http") as http:
            self.assertEqual(followups.process_due_followups(self.settings, now=self.now), 0)
        http.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.provider_message_id, "restock-first-native-mid")

    def test_restock_ladder_and_legacy_event_entry_cannot_invent_subscription(self):
        self.assertIsNone(followups.schedule_policy_followup(self.customer, "restock_wait", now=self.now))
        self.assertIsNone(followups.schedule_event_followup(self.customer, "restock_wait",
            step_index=1, event_key="invented", now=self.now,
            event_occurred_at=self.now, event_payload={"event": "invoice_expired"}))
        task = followups.schedule_event_followup(self.customer, "restock_wait", step_index=1,
            event_key="caller-key-not-authority", now=self.now, event_occurred_at=self.now,
            event_payload={"event": "restock_available", "product_id": self.product.pk,
                "variant_id": self.variant.pk, "size": "M", "fit_code": "classic",
                "option_values": {"fit": "classic"},
                "source_revision": f"product_catalog:{followups.variant_inventory_revision(self.variant.pk)}"})
        self.assertTrue(followups.is_restock_permission_review(task))
        self.assertFalse(followups._schedule_next_policy_step(task, self.customer, now=self.now))

    def test_direct_restock_scheduling_fails_before_cancelling_another_purpose(self):
        unrelated = IgFollowUpTask.objects.create(client=self.customer, due_at=self.now,
            kind="fulfillment", reason="paid_missing_delivery", message_text="Delivery details")
        for reason, payload in (("restock_wait", {}), ("restock_f2", {}),
                ("other", {"event": "restock_available"}),
                ("other", {"purpose": "restock_notification"})):
            with self.subTest(reason=reason, payload=payload):
                self.assertIsNone(followups.schedule_followup(self.customer, kind="qualification",
                    reason=reason, event_payload=payload, delay=timedelta(0), now=self.now,
                    message_text="The size is back"))
                unrelated.refresh_from_db()
                self.assertEqual(unrelated.status, "pending")
        self.assertEqual(IgFollowUpTask.objects.count(), 1)

    def test_historical_confirmed_purchase_does_not_hide_new_internal_interest_or_authorize_send(self):
        from management.services.bot_payment_truth import client_has_confirmed_purchase, current_payment_confirmation

        paid_at = self.now - timedelta(days=10)
        history = IgDeal.objects.create(client=self.customer, status="paid", payment_status="paid",
            payment_truth="confirmed", paid_at=paid_at, amount=880)
        projection = IgPaymentProjection.objects.create(client=self.customer, deal=history,
            truth="confirmed", gross_amount=880, paid_at=paid_at, provider_modified_at=paid_at)
        old_episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            open_slot=None, materialization_key="restock-previous-purchase", deal=history, state="fulfilled")
        current = IgCommercialEpisode.objects.create(client=self.customer, sequence=2,
            materialization_key="restock-new-interest", repeat_kind="explicit_more")
        self.customer.current_commercial_episode = current
        self.customer.save(update_fields=["current_commercial_episode", "updated_at"])
        before = (IgDeal.objects.filter(pk=history.pk).values().get(),
            IgPaymentProjection.objects.filter(pk=projection.pk).values().get(),
            IgCommercialEpisode.objects.filter(pk=old_episode.pk).values().get())
        self.assertTrue(client_has_confirmed_purchase(self.customer))
        self.assertFalse(current_payment_confirmation(self.customer)["confirmed"])
        self.assertEqual(self.event(), 1)
        self.assertTrue(followups.is_restock_permission_review(self.review()))
        self.assertEqual(followups._restock_customer_send_reason(self.review()), "restock_purpose_unverified")
        with patch("management.services.instagram_bot._provider_http") as http:
            self.assertEqual(followups.process_due_followups(self.settings, now=self.now), 0)
        http.assert_not_called()
        self.assertEqual((IgDeal.objects.filter(pk=history.pk).values().get(),
            IgPaymentProjection.objects.filter(pk=projection.pk).values().get(),
            IgCommercialEpisode.objects.filter(pk=old_episode.pk).values().get()), before)

    def independent_payment_decision(self):
        deal = IgDeal.objects.create(client=self.customer, amount=880, status="awaiting_payment",
            payment_truth="unverified", invoice_id="restock-independent-invoice")
        episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1,
            materialization_key="restock-independent-payment", deal=deal)
        self.customer.current_commercial_episode = episode
        self.customer.save(update_fields=["current_commercial_episode", "updated_at"])
        return {"purpose": "payment", "source_scope": {"commercial_episode_id": episode.pk},
            "source_message_ids": [], "commerce_evidence_refs": []}

    def test_owned_internal_review_does_not_block_independent_payment_or_grant_its_send(self):
        self.assertEqual(self.event(), 1)
        decision = self.independent_payment_decision()
        self.assertEqual(purpose_blockers(self.customer, decision), "")
        payment = IgFollowUpTask.objects.create(client=self.customer, due_at=self.now,
            kind="payment", reason="payment_link_unpaid", event_payload={"event": "invoice_expired"})
        self.assertEqual(followups._restock_customer_send_reason(payment), "")
        self.assertEqual(followups._restock_customer_send_reason(self.review()), "restock_purpose_unverified")

    def test_fake_review_origin_remains_real_manager_blocker(self):
        self.assertEqual(self.event(), 1)
        actual = self.review()
        payload = deepcopy(actual.event_payload)
        payload["origin"] = "different_owner"
        fake = IgFollowUpTask.objects.create(client=self.customer, kind="manager_task",
            due_at=self.now, reason=actual.reason, trigger="event", event_key="fake-restock-review",
            event_occurred_at=self.now, policy_started_at=self.now, policy_version=actual.policy_version,
            event_payload=payload, manager_context={"restock_review": payload}, message_text=actual.message_text)
        self.assertFalse(followups.is_restock_permission_review(fake))
        self.assertEqual(purpose_blockers(self.customer, self.independent_payment_decision()), "pending_manager_case")

    def test_changed_operator_context_is_not_silently_exempted_as_owned_informational_work(self):
        self.assertEqual(self.event(), 1)
        actual = self.review()
        actual.message_text = "Customer asked the manager to arrange a delivery"
        actual.save(update_fields=["message_text", "updated_at"])
        self.assertFalse(followups.is_restock_permission_review(actual))
        self.assertEqual(purpose_blockers(self.customer, self.independent_payment_decision()), "pending_manager_case")
