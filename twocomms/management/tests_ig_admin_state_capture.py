"""Current admin observations preserve source ownership without creating work."""
from copy import deepcopy
from datetime import timedelta
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from django.utils import timezone

from management.bot_access import VIEW_IG_CONVERSATION_PII_PERMISSION
from management.bot_state_views import bot_client_state_api
from management.models import (IgClient, IgCommerceSelectionSession, IgCommercialEpisode,
    IgCustomerTurn, IgCustomerTurnRevision, IgDeal, IgFunnelResetAudit,
    IgPaymentConfirmationReview, InstagramBotMessage, InstagramBotSettings)
from management.services import ig_admin_state_capture as capture
from management.services.ig_commerce_projection import captured_selection_for
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn

urlpatterns = [path("bot/api/clients/<int:client_id>/state/", bot_client_state_api, name="admin-state-test")]


@override_settings(GOOGLE_INDEXING_ENABLED=False, ROOT_URLCONF=__name__, SECURE_SSL_REDIRECT=False)
class CurrentAdminStateTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = timezone.now()
        self.row = IgClient.objects.create(igsid="admin-state-client", language="uk")
        self.settings_row = InstagramBotSettings.objects.create(pk=1, ig_user_id="admin-state-owner")
        self.namespace = "instagram_login:admin-state-owner"
        self.source_count = 0

    def source(self, text, **kwargs):
        self.source_count += 1
        return InstagramBotMessage.objects.create(client=self.row, sender_id=self.row.igsid,
            role="user", source="webhook", status="pending", provider_namespace=self.namespace,
            provider_created_at=self.now-timedelta(minutes=1), text=text,
            mid=f"admin-state-{self.source_count}", **kwargs)

    def partial(self):
        source = self.source("Хочу футболку розмір л")
        decision = apply_turn(self.row, source, parse_turn(source.text), reply_payload={})
        self.row.refresh_from_db()
        self.session = decision.session
        self.source_row = source
        return source

    def result(self, **kwargs):
        return capture.current_admin_state(self.row.pk, now=self.now, **kwargs)

    def slots(self, result):
        return result.state.as_dict()["slots"]

    def assertSelectOnly(self, queries):
        verbs = [item["sql"].lstrip().split(None, 1)[0].upper() for item in queries]
        self.assertFalse(set(verbs) & {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP"}, verbs)
        self.assertTrue(set(verbs) <= {"SELECT", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "ROLLBACK"}, verbs)
        return verbs.count("SELECT")

    def test_client352_partial_choice_without_sku_retains_original_proof(self):
        source = self.partial()
        with CaptureQueriesContext(connection) as queries:
            result = self.result()
        selects = self.assertSelectOnly(queries)
        self.assertEqual(result.status, "captured", result.as_dict())
        self.assertLessEqual(selects, capture.MAX_READ_QUERIES)
        self.assertEqual(result.read_queries, selects)
        size = self.slots(result)["choice.size"]
        self.assertEqual((size["value"], size["status"], size["applicability"], size["availability"]),
            ("L", "confirmed", "unknown", "unknown"))
        self.assertEqual(size["source_refs"][0]["id"], source.pk)
        self.assertEqual(self.slots(result)["choice.garment_type"]["value"], "tshirt")
        self.assertEqual(self.slots(result)["choice.product_id"]["status"], "unknown")
        self.assertEqual(self.slots(result)["configuration.readiness"]["status"], "unknown")
        self.assertEqual(result.as_dict()["view_mode"], "current_admin")
        self.assertEqual(result.as_dict()["as_of"], self.now.isoformat())
        self.assertEqual(result.state.as_dict()["boundary"]["source_watermark_kind"], "current_observation_vector")

    def test_legacy_selection_without_source_is_unknown_and_never_bootstrapped(self):
        self.row.current_size = "XL"
        self.row.sales_context = {"assisted_checkout_selection": {"product_id": 37, "size": "XL"}}
        self.row.save(update_fields=["current_size", "sales_context"])
        with patch("management.services.ig_commerce_projection.bootstrap_session_from_legacy") as bootstrap, patch(
                "management.services.call_ai_analysis.gemini_generate_text") as provider, patch(
                "management.services.ig_memory_producer.enqueue_memory_source") as enqueue, patch(
                "management.services.bot_conversation_analysis.schedule_analysis") as analysis:
            with CaptureQueriesContext(connection) as queries:
                result = self.result()
        self.assertSelectOnly(queries)
        for mocked in (bootstrap, provider, enqueue, analysis):
            mocked.assert_not_called()
        self.assertEqual(self.slots(result)["choice.size"]["status"], "unknown")
        self.assertFalse(IgCommerceSelectionSession.objects.filter(client=self.row).exists())

    def test_missing_settings_is_read_only_and_does_not_seed(self):
        self.partial()
        InstagramBotSettings.objects.all().delete()
        with CaptureQueriesContext(connection) as queries:
            result = self.result()
        self.assertSelectOnly(queries)
        self.assertEqual(self.slots(result)["choice.size"]["status"], "unknown")
        self.assertFalse(InstagramBotSettings.objects.exists())

    def test_old_paid_episode_and_review_do_not_enter_new_episode(self):
        self.partial()
        old = IgCommercialEpisode.objects.get(pk=self.row.current_commercial_episode_id)
        deal = IgDeal.objects.create(client=self.row, amount="790.00", payment_truth="confirmed")
        review = IgPaymentConfirmationReview.objects.create(client=self.row, deal=deal,
            dedupe_key="admin-old-paid", status="confirmed", evidence={"order_draft": {"quoted_total": "790.00"}})
        old.deal, old.primary_payment_review, old.open_slot = deal, review, None
        old.save(update_fields=["deal", "primary_payment_review", "open_slot"])
        new = IgCommercialEpisode.objects.create(client=self.row, sequence=old.sequence+1,
            materialization_key="admin-new-episode")
        self.row.current_commercial_episode = new
        self.row.save(update_fields=["current_commercial_episode"])
        result = self.result()
        self.assertEqual(result.state.as_dict()["scope"]["episode_id"], new.pk)
        self.assertEqual(self.slots(result)["payment.current"]["status"], "unknown")
        self.assertEqual(self.slots(result)["choice.size"]["status"], "unknown")
        self.assertNotIn("790.00", str(result.as_dict()))

    def test_exact_episode_payment_capture_disables_latest_review_fallback(self):
        self.partial()
        episode = IgCommercialEpisode.objects.get(pk=self.row.current_commercial_episode_id)
        deal = IgDeal.objects.create(client=self.row, amount="790.00")
        episode.deal = deal
        episode.save(update_fields=["deal"])
        IgPaymentConfirmationReview.objects.create(client=self.row, deal=deal,
            dedupe_key="admin-unbound-review", status="confirmed")
        from management.services.ig_commercial_episodes import payment_truth_snapshot
        with patch("management.services.ig_commercial_episodes.payment_truth_snapshot", wraps=payment_truth_snapshot) as truth:
            result = self.result()
        self.assertEqual(truth.call_count, 2)
        self.assertTrue(all(call.kwargs["allow_deal_review_fallback"] is False for call in truth.call_args_list))
        payment = self.slots(result)["payment.current"]
        self.assertEqual(payment["status"], "confirmed")
        self.assertEqual(payment["value"]["deal_id"], deal.pk)
        self.assertIsNone(payment["value"]["review_id"])
        # Measure the interactive path with an actual catalog product/variant
        # and the exact episode's review, rather than only a partial choice.
        from storefront.models import Category, Product, ProductStatus
        from productcolors.models import Color, ProductColorVariant
        from management.services.ig_commerce_source_identity import resolve_source_product_request
        category = Category.objects.create(name="Tshirts", slug="admin-state-tshirts")
        product = Product.objects.create(category=category, title="Admin state tshirt", slug="admin-state-tshirt",
            price=790, status=ProductStatus.PUBLISHED)
        color = Color.objects.create(name="Black", primary_hex="#000000")
        ProductColorVariant.objects.create(product=product, color=color)
        source = self.source("https://twocomms.shop/product/admin-state-tshirt/ розмір L")
        apply_turn(self.row, source, resolve_source_product_request(self.row, source, parse_turn(source.text)), reply_payload={})
        self.row.refresh_from_db()
        episode.refresh_from_db()
        episode.primary_payment_review = IgPaymentConfirmationReview.objects.get(dedupe_key="admin-unbound-review")
        episode.save(update_fields=["primary_payment_review"])
        with CaptureQueriesContext(connection) as queries:
            catalog_result = self.result()
        selects = self.assertSelectOnly(queries)
        self.assertEqual(catalog_result.status, "captured", catalog_result.as_dict())
        self.assertEqual(catalog_result.read_queries, selects)
        self.assertLessEqual(selects, capture.MAX_READ_QUERIES)
        self.assertEqual(self.slots(catalog_result)["choice.product_id"]["value"], product.pk)
        from management.services.ig_selection_corrections import save_size_correction
        import uuid
        operator = get_user_model().objects.create_superuser(username="admin-catalog-correction", password="test")
        context = catalog_result.state.as_dict()["boundary"]["size_correction_context"]
        save_size_correction(self.row.pk, actor=operator, operation_id=uuid.uuid4(),
            expected_selection_revision=context["context"]["selection_revision"],
            expected_context_digest=context["context_digest"], operation="set", value="XL", now=self.now)
        with CaptureQueriesContext(connection) as queries:
            corrected_catalog = self.result()
        corrected_selects = self.assertSelectOnly(queries)
        self.assertEqual(corrected_catalog.status, "captured", corrected_catalog.as_dict())
        self.assertEqual(corrected_catalog.read_queries, corrected_selects)
        self.assertLessEqual(corrected_selects, capture.MAX_READ_QUERIES)
        self.assertEqual(self.slots(corrected_catalog)["choice.size"]["authority"], "audited_correction")

    def test_foreign_client_payment_is_omitted(self):
        self.partial()
        foreign = IgClient.objects.create(igsid="admin-foreign")
        deal = IgDeal.objects.create(client=foreign, amount="12345.00")
        episode = IgCommercialEpisode.objects.get(pk=self.row.current_commercial_episode_id)
        episode.deal = deal
        episode.save(update_fields=["deal"])
        result = self.result()
        self.assertEqual(result.reason, "payment_source_scope_mismatch")
        self.assertEqual(self.slots(result)["payment.current"]["status"], "unknown")
        self.assertNotIn("12345.00", str(result.as_dict()))

    def test_payment_link_to_unbound_order_cannot_fill_current_order(self):
        from orders.models import Order
        self.partial()
        order = Order.objects.create(full_name="Test", phone="380501112233", city="Kyiv",
            np_office="1", total_sum="12345.00", source="manual")
        deal = IgDeal.objects.create(client=self.row, order=order, amount="12345.00")
        episode = IgCommercialEpisode.objects.get(pk=self.row.current_commercial_episode_id)
        episode.deal = deal
        episode.save(update_fields=["deal"])
        result = self.result()
        self.assertEqual(result.reason, "payment_order_scope_mismatch")
        self.assertIsNone(result.state.as_dict()["scope"]["order_id"])
        self.assertEqual(self.slots(result)["payment.current"]["status"], "unknown")
        self.assertNotIn("12345.00", str(result.as_dict()))

    def test_reset_boundary_invalidates_previous_source(self):
        source = self.partial()
        IgFunnelResetAudit.objects.create(client=self.row, reset_after_message_id=source.pk, reason="test")
        result = self.result()
        self.assertEqual(result.state.as_dict()["scope"]["reset_floor"], source.pk+1)
        self.assertEqual(self.slots(result)["choice.size"]["status"], "unknown")
        self.assertFalse(result.state.source_selection)

    def test_erasure_returns_no_personal_state(self):
        self.partial()
        self.row.privacy_erasure_started_at = self.now
        self.row.save(update_fields=["privacy_erasure_started_at"])
        with CaptureQueriesContext(connection) as queries:
            result = self.result()
        self.assertEqual((result.status, result.reason), ("unavailable", "client_erasing"))
        self.assertEqual(self.assertSelectOnly(queries), 1)
        self.assertFalse(result.state.as_dict()["slots"])
        self.assertNotIn("L", str(result.as_dict()))

    def test_generic_bot_optin_never_grants_any_purpose(self):
        self.partial()
        self.row.opted_in_at = self.now
        self.row.save(update_fields=["opted_in_at"])
        result = self.result()
        for purpose in ("marketing", "payment_reminder", "restock"):
            slot = self.slots(result)["consent."+purpose]
            self.assertEqual((slot["status"], slot["authority"], slot["omission_reason"]),
                ("unknown", "none", "purpose_grant_unknown"))

    def test_current_selection_cas_conflict_returns_no_old_slots(self):
        self.partial()
        result = self.result(expected_selection_revision=self.session.revision+1)
        self.assertEqual((result.status, result.reason), ("conflict", "selection_revision_conflict"))
        self.assertFalse(result.state.as_dict()["slots"])
        for invalid in (0, -1, True, capture.MAX_IDENTIFIER+1):
            with self.subTest(invalid=invalid), CaptureQueriesContext(connection) as queries:
                invalid_client = capture.current_admin_state(invalid, now=self.now)
                invalid_expected = self.result(expected_selection_revision=invalid)
                invalid_history = capture.historical_admin_state(self.row.pk, invalid, now=self.now)
            self.assertEqual(len(queries), 0)
            self.assertEqual(invalid_client.reason, "client_id_invalid")
            self.assertEqual(invalid_expected.reason, "selection_revision_invalid")
            self.assertEqual(invalid_history.reason, "revision_id_invalid")

    def test_duplicate_line_and_foreign_parent_scope_are_unavailable(self):
        self.partial()
        original = deepcopy(self.session.lines)
        for lines in ([*original, deepcopy(original[0])], [{**original[0], "client_id": self.row.pk+99}]):
            IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(lines=lines)
            result = self.result()
            self.assertEqual(result.reason, "selection_line_scope_unknown")
            self.assertFalse(result.state.as_dict()["slots"])

    def test_line_and_recipient_cannot_borrow_source_choice(self):
        self.partial()
        original = captured_selection_for(self.row)
        for key, changed in (("recipient_id", "friend"), ("line_id", "second-line")):
            forged = deepcopy(original)
            forged["scope"][key] = changed
            with patch("management.services.ig_commerce_projection.captured_selection_for", return_value=forged):
                result = self.result()
            self.assertEqual((result.status, result.reason), ("conflict", "current_selection_scope_changed"))
            self.assertFalse(result.state.as_dict()["slots"])

    def test_source_namespace_and_event_time_are_revalidated(self):
        source = self.partial()
        for field, changed in (("provider_namespace", "instagram_login:foreign"),
                ("provider_created_at", self.now+timedelta(days=1)), ("status", "failed")):
            with self.subTest(field=field):
                original = getattr(source, field)
                InstagramBotMessage.objects.filter(pk=source.pk).update(**{field: changed})
                result = self.result()
                self.assertEqual(self.slots(result)["choice.size"]["status"], "unknown")
                InstagramBotMessage.objects.filter(pk=source.pk).update(**{field: original})

    def test_session_race_is_conflict_without_dml_in_reader(self):
        self.partial()
        original = capture._owner_fence
        calls = 0
        def changed(client_id):
            nonlocal calls
            value = original(client_id)
            calls += 1
            if calls == 2:
                value["session"]["revision"] += 1
            return value
        with patch.object(capture, "_owner_fence", side_effect=changed), CaptureQueriesContext(connection) as queries:
            result = self.result()
        self.assertSelectOnly(queries)
        self.assertEqual((result.status, result.reason), ("conflict", "current_state_changed"))
        self.assertFalse(result.state.as_dict()["slots"])

    def test_source_watermark_race_is_conflict(self):
        self.partial()
        original = capture._source_watermark
        calls = 0
        def changed(*args):
            nonlocal calls
            value = original(*args)
            calls += 1
            if calls == 2:
                value["message_id"] += 1
            return value
        with patch.object(capture, "_source_watermark", side_effect=changed):
            result = self.result()
        self.assertEqual((result.status, result.reason), ("conflict", "current_source_changed"))
        self.assertFalse(result.state.as_dict()["slots"])

    def test_original_source_edit_race_is_conflict(self):
        self.partial()
        original = capture._source_rows
        calls = 0
        def changed(ids):
            nonlocal calls
            rows = original(ids)
            calls += 1
            if calls == 2:
                rows[0]["text"] = "Розмір XL"
            return rows
        with patch.object(capture, "_source_rows", side_effect=changed), CaptureQueriesContext(connection) as queries:
            result = self.result()
        self.assertSelectOnly(queries)
        self.assertEqual((result.status, result.reason), ("conflict", "current_source_changed"))
        self.assertFalse(result.state.as_dict()["slots"])

    def test_payment_truth_change_race_is_conflict(self):
        self.partial()
        original = capture._payment_capture
        calls = 0
        def changed(*args):
            nonlocal calls
            value, reason = original(*args)
            calls += 1
            return (value, "payment_changed_during_read") if calls == 2 else (value, reason)
        with patch.object(capture, "_payment_capture", side_effect=changed):
            result = self.result()
        self.assertEqual((result.status, result.reason), ("conflict", "current_payment_changed"))
        self.assertFalse(result.state.as_dict()["slots"])

    def test_read_budget_and_accidental_writer_fail_closed(self):
        self.partial()
        original_size = self.row.current_size
        with patch.object(capture, "MAX_READ_QUERIES", 2):
            budget = self.result()
        self.assertEqual(budget.reason, "state_read_budget_exceeded")
        def writer(*args):
            IgClient.objects.filter(pk=self.row.pk).update(current_size="XL")
        with patch.object(capture, "_current_capture", side_effect=writer):
            denied = self.result()
        self.assertEqual(denied.reason, "state_read_side_effect_rejected")
        self.assertFalse(connection.needs_rollback)
        self.row.refresh_from_db()
        self.assertEqual(self.row.current_size, original_size)
        # An already broken caller belongs to that caller. The factory must
        # neither clear its rollback flag nor create an unisolated writer.
        with transaction.atomic():
            transaction.set_rollback(True)
            blocked = self.result()
            self.assertEqual(blocked.reason, "state_read_unavailable")
            self.assertTrue(connection.needs_rollback)
        self.assertFalse(connection.needs_rollback)
        self.row.refresh_from_db()
        self.assertEqual(self.row.current_size, original_size)
        # A downstream helper may catch the denied ORM call; the factory's
        # latched violation still forces its own savepoint rollback.
        previous = self.result()
        def swallowing_writer(*args):
            try:
                writer()
            except capture.AdminStateReadError:
                return previous
        with patch.object(capture, "_current_capture", side_effect=swallowing_writer):
            swallowed = self.result()
        self.assertEqual(swallowed.reason, "state_read_side_effect_rejected")
        self.assertFalse(connection.needs_rollback)
        with patch.object(capture, "_current_capture", side_effect=DatabaseError("reader failed")):
            failed = self.result()
        self.assertEqual(failed.reason, "state_read_unavailable")
        self.assertFalse(connection.needs_rollback)
        self.row.refresh_from_db()
        self.assertEqual(self.row.current_size, original_size)

    def test_historical_revision_is_not_reconstructed_from_current_facts(self):
        source = self.partial()
        turn, _ = IgCustomerTurn.objects.get_or_create(primary_source_message=source,
            defaults={"client": self.row, "window_started_at": self.now, "window_deadline": self.now})
        revision = IgCustomerTurnRevision.objects.create(client=self.row, turn=turn, revision=1,
            quiet_started_at=self.now, quiet_deadline=self.now, quiet_cap_at=self.now, overall_deadline=self.now,
            bundle_snapshot={"sources": [{"message_id": source.pk, "text": source.text}]})
        with CaptureQueriesContext(connection) as queries:
            result = capture.historical_admin_state(self.row.pk, revision.pk, now=self.now)
        self.assertSelectOnly(queries)
        self.assertEqual((result.status, result.reason, result.view_mode),
            ("not_reconstructable", "historical_capture_unavailable", "historical"))
        self.assertFalse(result.state.as_dict()["slots"])
        foreign = IgClient.objects.create(igsid="history-foreign")
        self.assertEqual(capture.historical_admin_state(foreign.pk, revision.pk).reason, "revision_missing")

    def test_endpoint_permission_get_only_no_cache_and_parameter_validation(self):
        actor = get_user_model().objects.create_user(username="admin-state-reader")
        request = RequestFactory().get("/bot/api/clients/1/state/")
        request.user = actor
        self.assertEqual(bot_client_state_api(request, self.row.pk).status_code, 403)
        actor.user_permissions.add(Permission.objects.get(content_type__app_label="management",
            codename=VIEW_IG_CONVERSATION_PII_PERMISSION.split(".")[1]))
        actor = get_user_model().objects.get(pk=actor.pk)
        for query in ({"expected_selection_revision": "bad"}, {"revision_id": "0"},
                {"revision_id": "1", "expected_selection_revision": "0"},
                {"expected_selection_revision": str(capture.MAX_IDENTIFIER+1)},
                {"revision_id": str(capture.MAX_IDENTIFIER+1)}):
            request = RequestFactory().get("/bot/api/clients/1/state/", query)
            request.user = actor
            self.assertEqual(bot_client_state_api(request, self.row.pk).status_code, 400)
        request = RequestFactory().get("/bot/api/clients/999999999999999999999/state/")
        request.user = actor
        self.assertEqual(bot_client_state_api(request, capture.MAX_IDENTIFIER+1).status_code, 400)
        request = RequestFactory().post("/bot/api/clients/1/state/")
        request.user = actor
        self.assertEqual(bot_client_state_api(request, self.row.pk).status_code, 405)

    def test_direct_and_fetch_endpoint_get_have_zero_dml_provider_or_work(self):
        self.partial()
        actor = get_user_model().objects.create_superuser(username="admin-state-ui", password="test")
        self.client.force_login(actor)
        with patch("management.services.call_ai_analysis.gemini_generate_text") as provider, patch(
                "management.services.ig_memory_producer.enqueue_memory_source") as enqueue, patch(
                "management.services.ig_commerce_projection.bootstrap_session_from_legacy") as bootstrap:
            for headers in ({}, {"HTTP_SEC_FETCH_MODE": "cors", "HTTP_SEC_FETCH_DEST": "empty"}):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(f"/bot/api/clients/{self.row.pk}/state/", **headers)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertSelectOnly(queries)
                self.assertIn("no-store", response.headers["Cache-Control"])
                self.assertEqual(response.json()["status"], "captured")
        for mocked in (provider, enqueue, bootstrap):
            mocked.assert_not_called()
