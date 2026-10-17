"""An exact chart is presentation evidence, never an implicit product purchase."""
from pathlib import Path
import json
import tempfile
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.test import TransactionTestCase, override_settings

from management import tests_ig_product_focus as focus_fixture
from management.models import GeminiRequest, IgFollowUpTask, InstagramBotMessage
from management.services.ig_revision_outbox import PublicationBinding


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class DiscussedChartTests(TransactionTestCase):
    reset_sequences = True
    _message = focus_fixture.ProductFocusTests._message
    _prepare = focus_fixture.ProductFocusTests._prepare
    _generate = focus_fixture.ProductFocusTests._generate
    followup = focus_fixture.ProductFocusTests.followup
    configure_fit_catalog = focus_fixture.ProductFocusTests.configure_fit_catalog
    deliver_human_fit_question = focus_fixture.ProductFocusTests.deliver_human_fit_question

    def setUp(self):
        focus_fixture.ProductFocusTests.setUp(self)
        self.configure_fit_catalog()
        directory = tempfile.TemporaryDirectory(prefix="discussed-chart-")
        self.addCleanup(directory.cleanup)
        override = override_settings(MEDIA_ROOT=directory.name, MEDIA_URL="/media/")
        override.enable()
        self.addCleanup(override.disable)
        self.grid = self.product.size_grid
        self.grid.image = "size_grids/classic-tshirt.webp"
        self.grid.save(update_fields=["image"])
        target = Path(directory.name, self.grid.image.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"native chart fixture")
        self.deliver_human_fit_question()
        old_answer = self.followup("Звична")
        self.old_source = old_answer.sources.get().message
        with patch("management.services.ig_fit_consultation.contextual_fit_choice", return_value={}):
            self._reduce_current()
        self.deliver_human_fit_question(context_message_id=self.old_source.pk,
            text="Домовились: «Правил немає», класичний крій (regular). Який розмір вам потрібен?")
        self.followup("Скиньте розмірну сітку")
        self._reduce_current()
        from management.services.ig_product_focus import capture_discussed_configuration
        from management.services.ig_catalog_configured_choices import capture_configured_choices
        certificate = capture_discussed_configuration(self.revision)
        self.assertTrue(certificate)
        configured = capture_configured_choices(certificate["product_id"], fit_code=certificate["fit_code"])
        self.assertTrue((configured.get("grid") or {}).get("photo_ref"), configured)

    def _reduce_current(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        reduced = reduce_revision_commerce(self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        self.revision.refresh_from_db()

    def _execute(self, *, unknown=False, callback=None):
        from management.services.ig_revision_live import execute_claimed_revision
        counter = iter(range(10))
        def sent(*args, **kwargs):
            if callback:
                callback(*args, **kwargs)
            return 200, json.dumps({"message_id": f"native-chart-{next(counter)}"})
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("unexpected model")) as model, \
                patch("management.services.instagram_bot.get_page_token", return_value="chart-fixture-token"), \
                patch("management.services.instagram_bot._provider_http", side_effect=TimeoutError() if unknown else sent) as http:
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        return result, model, http

    def test_chart_request_sends_native_exact_fit_image_without_graph_or_product_selection(self):
        result, model, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        model.assert_not_called()
        self.assertEqual(http.call_count, 2)
        self.revision.refresh_from_db()
        effects = list(self.revision.delivery_effects.order_by("order_index"))
        chart = effects[0].projection_metadata["catalog_chart"]
        self.assertEqual(chart["configuration_selector"]["fit_code"], "classic")
        self.assertEqual(chart["presentation_context"]["fit_source"]["source_message_id"], self.old_source.pk)
        self.assertEqual(chart["grid_id"], self.grid.pk)
        self.assertEqual(effects[0].payload["message"]["attachment"]["type"], "image")
        self.assertNotIn("buttons", str(effects[0].payload))
        self.assertFalse(GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{self.revision.pk}").exists())
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.assertNotIn("shown_products", self.customer.sales_context or {})
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").exists())
        _, model, http = self._execute()
        model.assert_not_called()
        http.assert_not_called()

    def test_failed_chart_graph_remains_failed_after_native_reply(self):
        source = self.revision.sources.get().message
        graph = GeminiRequest.objects.create(request_id="chart-failed-235", client_id=self.customer.pk,
            source_message_id=source.pk, logical_turn_id=f"ig-revision:{self.revision.pk}", terminal_resolution="failed")
        result, model, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 2)
        model.assert_not_called()
        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertIsNone(graph.winner_attempt_id)

    def test_chart_certificate_is_db_only_and_rejects_changed_original_fit_answer(self):
        from management.services.ig_catalog_choice_presentations import (
            prepare_catalog_chart_effect, catalog_chart_reference, validate_catalog_chart_reference)
        from management.services.ig_response_control import ValidatedResponse
        prepared = prepare_catalog_chart_effect(self.revision, ValidatedResponse("Ось розмірна сітка."), self.settings)
        self.assertTrue(prepared.ready, prepared.reasons)
        reference = catalog_chart_reference(prepared.manifest)
        with transaction.atomic(), patch.object(FileSystemStorage, "open", side_effect=AssertionError("open")), \
                patch.object(FileSystemStorage, "path", side_effect=AssertionError("path")), \
                patch.object(FileSystemStorage, "exists", side_effect=AssertionError("exists")), \
                patch.object(FileSystemStorage, "size", side_effect=AssertionError("size")):
            self.assertEqual(validate_catalog_chart_reference(reference, self.customer, self.settings), "")
        InstagramBotMessage.objects.filter(pk=self.old_source.pk).update(text="Вільніша")
        self.assertEqual(validate_catalog_chart_reference(reference, self.customer, self.settings), "catalog_choice_chart_context_changed")

    def test_unknown_native_chart_never_retries(self):
        first, model, http = self._execute(unknown=True)
        self.assertEqual(first.state, "delivery_pending", first.reasons)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(self.revision.delivery_effects.get(group="template").state, "unknown")
        second, model, http = self._execute()
        self.assertEqual(second.state, "delivery_pending", second.reasons)
        model.assert_not_called()
        http.assert_not_called()

    def test_mixed_chart_price_request_cannot_use_local_chart_admission(self):
        from management.services.ig_revision_input import record_source_preference_fallback
        self.followup("Скиньте розмірну сітку. Скільки коштує футболка?")
        self._reduce_current()
        admitted = record_source_preference_fallback(self.revision.pk, self.token,
            settings_id=self.settings.pk, approved_chart=True)
        self.assertFalse(admitted.ready)
        self.assertEqual(admitted.reason, "fallback_not_pure_native_chart")

    def test_chart_edit_after_image_receipt_stops_remaining_text_without_duplicate_image(self):
        def edit_chart(*_args, **_kwargs):
            self.grid.guide_data = {"columns": [{"key": "size", "label": "Size"}], "rows": [{"size": "M"}]}
            self.grid.save(update_fields=["guide_data"])
        result, model, http = self._execute(callback=edit_chart)
        model.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get(group="template").state, "sent")
        self.assertEqual(self.revision.delivery_effects.get(group="substantive_text").state, "cancelled")
        self.assertNotEqual(result.state, "completed")
        _, model, http = self._execute()
        model.assert_not_called()
        http.assert_not_called()

    def test_new_discussed_print_never_inherits_old_canonical_products_chart(self):
        from storefront.models import Product, ProductFitOption
        from management.models import IgClient
        from management.services.ig_catalog_choice_presentations import prepare_catalog_chart_effect
        from management.services.ig_response_control import ValidatedResponse
        old = Product.objects.create(title="Old purchase", slug="old-chart-purchase", category=self.product.category,
            catalog=self.product.catalog, size_grid=self.grid, price=800, status="published")
        ProductFitOption.objects.create(product=old, code="classic", label="Classic", is_active=True, is_default=True)
        IgClient.objects.filter(pk=self.customer.pk).update(current_product=old)
        self.customer.refresh_from_db()
        self.revision.refresh_from_db()
        prepared = prepare_catalog_chart_effect(self.revision, ValidatedResponse("Розмірна сітка."), self.settings)
        self.assertTrue(prepared.ready, prepared.reasons)
        self.assertEqual(prepared.manifest["product_id"], self.product.pk)
        InstagramBotMessage.objects.filter(pk=self.old_source.pk).update(text="Дякую")
        unavailable = prepare_catalog_chart_effect(self.revision, ValidatedResponse("Який крій вам потрібен?"), self.settings)
        self.assertFalse(unavailable.ready)
        self.assertFalse(unavailable.effects)

    def test_chart_for_real_selected_product_and_fit_completes_exact_line_obligation(self):
        from management import tests_ig_revision_live as live_fixture
        self.followup("Класична, M")
        self._reduce_current()
        self.actual_inline_count = 0
        self.parsed = {"reply_text": "Ви обрали класичну посадку й розмір M для футболки «Правил немає».",
            "controls": [{"kind": "product", "value": str(self.product.pk)},
                {"kind": "fit", "value": "classic"}, {"kind": "size", "value": "M"}]}
        selected, model, http = live_fixture.RevisionLiveTests._execute(self)
        self.assertEqual(selected.state, "completed", selected.reasons)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, self.product.pk)
        self.followup("Скиньте розмірну сітку")
        self._reduce_current()
        from management.services.ig_response_plan import capture_response_plan
        plan = capture_response_plan(self.customer, revision=self.revision)
        chart_obligations = [row for row in plan.obligations if row["kind"] == "info:size_chart"]
        self.assertEqual(len(chart_obligations), 1)
        result, model, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        model.assert_not_called()
        self.assertEqual(http.call_count, 2)
        self.revision.refresh_from_db()
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertFalse(coverage["remaining"], coverage)
        self.assertIn(chart_obligations[0]["id"], coverage["covered"])
        scope = self.revision.delivery_effects.get(group="template").projection_metadata["catalog_chart"]["scope"]
        if chart_obligations[0].get("line_id"):
            self.assertEqual(scope["line_id"], chart_obligations[0]["line_id"])
