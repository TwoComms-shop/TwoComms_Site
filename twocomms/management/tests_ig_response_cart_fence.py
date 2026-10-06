"""Real source/cart admission and bounded final read predicates; no provider I/O."""
from copy import deepcopy
from datetime import timedelta
import json
import threading
from unittest.mock import patch

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_commerce_line_operations as fixtures
from management import tests_ig_turn_capture as turn_fixtures
from management.models import (IgClient, IgCommercialEpisode, IgCommerceSelectionSession,
    IgFunnelResetAudit, InstagramBotMessage, InstagramBotSettings)
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_response_cart_fence import (
    SourceCartAuthority, capture_source_cart_authority, check_source_cart_authority,
)
from management.services.ig_turn_intelligence import capture_digest


class ResponseCartFenceValueTests(SimpleTestCase):
    def test_binding_property_is_defensive_and_unavailable_has_no_authority(self):
        result = SourceCartAuthority(True, "", json.dumps({"source_ids": [1]}))
        result.binding["source_ids"].append(2)
        self.assertEqual(result.binding, {"source_ids": [1]})
        self.assertFalse(SourceCartAuthority(False, "source_cart_unavailable"))
        self.assertEqual(SourceCartAuthority(False).binding, {})

    def test_invalid_or_foreign_binding_does_not_read_database(self):
        for fence in ({}, {"schema": "source-cart-authority.v1"}, None):
            with self.subTest(fence=fence):
                result = check_source_cart_authority(1, fence)
                self.assertFalse(result.ready)
                self.assertEqual(result.reason, "source_cart_binding_invalid")


class CartFenceFixture:
    source = fixtures.CommerceLineOperationsTests.source
    reduce = fixtures.CommerceLineOperationsTests.reduce
    capture = fixtures.CommerceLineOperationsTests.capture
    attach_order = fixtures.CommerceLineOperationsTests.attach_order

    def setUp(self):
        super().setUp()
        fixtures.CommerceLineOperationsTests.setUp(self)

    def two_lines(self):
        first, initial = self.reduce("добавьте чёрную футболку размер L")
        second, decision = self.reduce("добавьте розовое худи размер XL для друга")
        self.assertEqual(len(decision.session.lines), 2)
        capture = self.capture()
        self.assertEqual(capture["status"], "captured", capture)
        self.assertTrue(capture["coverage_complete"], capture)
        return first, second, decision.session, capture

    def authority(self, capture):
        result = capture_source_cart_authority(self.customer, capture)
        self.assertTrue(result.ready, result.reason)
        return result.binding

    def assertDenied(self, fence, reason=None):
        result = check_source_cart_authority(self.customer, fence)
        self.assertFalse(result.ready)
        if reason:
            self.assertEqual(result.reason, reason)
        return result


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class ResponseCartFenceTests(CartFenceFixture, TestCase):
    def test_actual_two_line_capture_is_admitted_read_only_and_final_checks_do_not_recapture(self):
        _, _, _, capture = self.two_lines()
        with CaptureQueriesContext(connection) as preparation:
            fence = self.authority(capture)
        self.assertLessEqual(len(preparation), 80)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in preparation))
        self.assertEqual(fence["capture_digest"], capture["capture_digest"])
        self.assertEqual(fence["source_ids"], capture["fence"]["source_ids"])
        with patch("management.services.ig_commerce_projection.capture_current_selection_lines",
                side_effect=AssertionError("final fence must not recapture choices")), CaptureQueriesContext(connection) as reads:
            result = check_source_cart_authority(self.customer, fence)
        self.assertTrue(result.ready, result.reason)
        self.assertEqual(len(reads), 8)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in reads))

    def test_changed_nonactive_configuration_with_same_active_line_is_rejected(self):
        _, _, session, capture = self.two_lines()
        fence = self.authority(capture)
        lines = deepcopy(session.lines)
        lines[0]["size"] = "M"
        IgCommerceSelectionSession.objects.filter(pk=session.pk).update(lines=lines)
        self.assertDenied(fence, "source_cart_head_changed")

    def test_changed_nonactive_recipient_is_rejected_without_revision_increment(self):
        _, _, session, capture = self.two_lines()
        fence = self.authority(capture)
        lines = deepcopy(session.lines)
        lines[0]["recipient_id"] = "friend"
        IgCommerceSelectionSession.objects.filter(pk=session.pk).update(lines=lines)
        self.assertDenied(fence, "source_cart_head_changed")

    def test_nonactive_same_price_product_replacement_is_not_equal_authority(self):
        from storefront.models import Category, Product, ProductStatus
        category = Category.objects.create(name="Fence exact IDs", slug="fence-exact-ids")
        products = [Product.objects.create(title=title, slug=slug, category=category,
            price=790, status=ProductStatus.PUBLISHED) for title, slug in
            (("Alpha", "fence-alpha"), ("Beta", "fence-beta"), ("Gamma", "fence-gamma"))]
        _, first = self.reduce(f"add https://twocomms.shop/product/{products[0].slug}/ size L")
        _, second = self.reduce(f"add https://twocomms.shop/product/{products[1].slug}/ size XL")
        capture = self.capture()
        self.assertEqual(capture["status"], "captured", capture)
        fence = self.authority(capture)
        lines = deepcopy(second.session.lines)
        self.assertEqual(lines[0]["product_id"], products[0].pk)
        lines[0]["product_id"] = products[2].pk
        IgCommerceSelectionSession.objects.filter(pk=second.session.pk).update(lines=lines)
        self.assertDenied(fence, "source_cart_head_changed")

    def test_original_source_text_and_supporting_noop_are_both_fenced(self):
        first, _, _, _ = self.two_lines()
        observed, _ = self.reduce("Гаразд")
        capture = self.capture()
        fence = self.authority(capture)
        self.assertIn(observed.pk, fence["source_ids"])
        self.assertFalse(any(proof["source_message_id"] == observed.pk
            for row in capture["lines"] for proof in row["evidence"].values()))
        for source in (first, observed):
            original = source.text
            InstagramBotMessage.objects.filter(pk=source.pk).update(text="changed source frame")
            self.assertDenied(fence, "source_cart_sources_changed")
            InstagramBotMessage.objects.filter(pk=source.pk).update(text=original)
        self.assertTrue(check_source_cart_authority(self.customer, fence).ready)

    def test_source_client_sender_namespace_event_time_and_mid_fail_closed(self):
        first, _, _, capture = self.two_lines()
        fence = self.authority(capture)
        other = IgClient.objects.create(igsid="foreign-cart-owner")
        for field, value in (("client_id", other.pk), ("sender_id", other.igsid),
                ("provider_namespace", "instagram_login:foreign"),
                ("provider_created_at", self.now + timedelta(days=1)), ("status", "failed")):
            original = getattr(first, field)
            InstagramBotMessage.objects.filter(pk=first.pk).update(**{field: value})
            self.assertDenied(fence, "source_cart_sources_changed")
            InstagramBotMessage.objects.filter(pk=first.pk).update(**{field: original})
        InstagramBotMessage.objects.filter(pk=first.pk).update(mid="another-mid")
        self.assertDenied(fence, "source_cart_sources_changed")

    def test_copy_origin_sources_remain_in_final_authority_after_paid_repeat(self):
        original, _ = self.reduce("добавьте чёрную футболку размер L")
        self.attach_order(paid=True)
        copied, _ = self.reduce("ещё одну такую же")
        capture = self.capture()
        self.assertEqual(capture["status"], "captured", capture)
        fence = self.authority(capture)
        self.assertIn(original.pk, fence["source_ids"])
        self.assertIn(copied.pk, fence["source_ids"])
        InstagramBotMessage.objects.filter(pk=original.pk).update(text="unverified old copy origin")
        self.assertDenied(fence, "source_cart_sources_changed")

    def test_reset_erasure_permission_and_namespace_switch_are_strict(self):
        first, _, _, capture = self.two_lines()
        fence = self.authority(capture)
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
        self.assertDenied(fence, "source_cart_client_erasing")
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=None, reply_permission_epoch=1)
        self.assertDenied(fence, "source_cart_head_changed")
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=0)
        InstagramBotSettings.objects.filter(pk=1).update(ig_user_id="new-cart-owner")
        self.assertDenied(fence, "source_cart_namespace_changed")
        InstagramBotSettings.objects.filter(pk=1).update(ig_user_id="cart-source-owner")
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=first.pk)
        self.assertDenied(fence, "source_cart_head_changed")

    def test_episode_order_scope_changes_are_strict_but_episode_updated_at_is_not_source_identity(self):
        _, _, _, capture = self.two_lines()
        fence = self.authority(capture)
        episode = IgCommercialEpisode.objects.get(pk=self.customer.current_commercial_episode_id)
        IgCommercialEpisode.objects.filter(pk=episode.pk).update(updated_at=timezone.now() + timedelta(seconds=10))
        self.assertTrue(check_source_cart_authority(self.customer, fence).ready)
        fresh = self.capture()
        self.assertNotEqual(capture["fence"]["owner_digest"], fresh["fence"]["owner_digest"])
        recreated = capture_source_cart_authority(self.customer, capture)
        self.assertTrue(recreated.ready, recreated.reason)
        self.assertEqual(recreated.binding["semantic_capture_digest"], fence["semantic_capture_digest"])
        self.assertEqual(recreated.binding["capture_digest"], capture["capture_digest"])
        from orders.models import Order
        order = Order.objects.create(full_name="Owner", phone="380501234567", total_sum=790)
        IgCommercialEpisode.objects.filter(pk=episode.pk).update(intended_order_id=order.pk)
        self.assertDenied(fence, "source_cart_head_changed")

    def test_rehashed_forged_line_or_proof_history_is_not_admitted(self):
        _, _, _, capture = self.two_lines()
        for change in ("recipient", "history", "source_ids"):
            forged = deepcopy(capture)
            if change == "recipient":
                forged["lines"][0]["recipient_id"] = "foreign"
                forged["lines"][0]["source_selection"]["scope"]["recipient_id"] = "foreign"
            elif change == "history":
                forged["lines"][0]["history"] = [{"kind": "forged_history"}]
            else:
                forged["fence"]["source_ids"] = []
            forged["capture_digest"] = capture_digest({key: value for key, value in forged.items() if key != "capture_digest"})
            result = capture_source_cart_authority(self.customer, forged)
            self.assertFalse(result.ready, change)
        self.assertFalse(capture_source_cart_authority(self.customer,
            {"status": "unavailable", "schema": "source-selections.v1"}).ready)

    def test_binding_mutation_and_foreign_client_are_rejected_without_reads(self):
        _, _, _, capture = self.two_lines()
        fence = self.authority(capture)
        changed = deepcopy(fence)
        changed["source_ids"] = changed["source_ids"][:1]
        with self.assertNumQueries(0):
            self.assertDenied(changed, "source_cart_binding_invalid")
            self.assertFalse(check_source_cart_authority(self.customer.pk + 100, fence).ready)

    def test_database_error_is_finite_and_never_accepts_partial_head(self):
        _, _, _, capture = self.two_lines()
        fence = self.authority(capture)
        with patch("management.services.ig_response_cart_fence._read_head", side_effect=DatabaseError("unavailable")):
            self.assertDenied(fence, "source_cart_authority_unavailable")

    def test_source_changed_between_two_final_reads_is_not_a_torn_success(self):
        first, _, _, capture = self.two_lines()
        fence = self.authority(capture)
        from management.services.ig_turn_capture import validate_current_source_cart_sources as validate
        rows = list(InstagramBotMessage.objects.filter(pk__in=fence["source_ids"]))
        calls = 0
        def after_first(*args, **kwargs):
            nonlocal calls
            result = validate(*args, source_rows=rows, **kwargs)
            calls += 1
            if calls == 1:
                # Actual digest validation uses an independently supplied read
                # result, as the admin consumer does. No synthetic rejection.
                next(row for row in rows if row.pk == first.pk).text = "changed after first source read"
            return result
        with patch("management.services.ig_turn_capture.validate_current_source_cart_sources", side_effect=after_first):
            self.assertDenied(fence, "source_cart_sources_changed")
        self.assertEqual(calls, 1)

    def test_real_sealed_revision_accepts_equal_watermark_and_rejects_later_cart_source(self):
        first, _ = self.reduce("добавьте чёрную футболку размер L")
        self.client_row = self.customer
        revision, _, _ = turn_fixtures.RevisionTurnCaptureTests.seal(self, first)
        capture = self.capture()
        admitted = capture_source_cart_authority(self.customer, capture, revision=revision)
        self.assertTrue(admitted.ready, admitted.reason)
        self.assertTrue(check_source_cart_authority(self.customer, admitted.binding, revision=revision).ready)
        self.reduce("добавьте худи размер XL для друга")
        later = capture_source_cart_authority(self.customer, self.capture(), revision=revision)
        self.assertFalse(later.ready)
        self.assertEqual(later.reason, "source_cart_after_seal")


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class NativeResponseCartFenceRaceTests(CartFenceFixture, TransactionTestCase):
    def test_committed_nonactive_change_between_owner_reads_is_rejected(self):
        if connection.vendor != "mysql":
            self.skipTest("actual independent MariaDB connection required")
        _, _, session, capture = self.two_lines()
        fence = self.authority(capture)
        from management.services.ig_response_cart_fence import _read_head
        changed = threading.Event()
        proceed = threading.Event()
        errors = []
        lines = deepcopy(session.lines)
        lines[0]["size"] = "M"
        def writer():
            close_old_connections()
            try:
                if not proceed.wait(5):
                    raise AssertionError("reader did not reach actual first owner read")
                with transaction.atomic():
                    row = IgCommerceSelectionSession.objects.select_for_update().get(pk=session.pk)
                    row.lines = lines
                    row.save(update_fields=["lines"])
                changed.set()
            except BaseException as exc:
                errors.append(exc)
                changed.set()
            finally:
                close_old_connections()
        thread = threading.Thread(target=writer)
        thread.start()
        reads = 0
        def interleaved(client_id):
            nonlocal reads
            head = _read_head(client_id)
            reads += 1
            if reads == 1:
                proceed.set()
                self.assertTrue(changed.wait(5), "writer did not commit")
                self.assertEqual(errors, [])
            return head
        try:
            with patch("management.services.ig_response_cart_fence._read_head", side_effect=interleaved):
                self.assertDenied(fence, "source_cart_head_changed")
        finally:
            proceed.set()
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
