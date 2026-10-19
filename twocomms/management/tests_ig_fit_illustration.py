"""Native fit-photo truth, labeled illustrations and immutable send evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import json
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_product_focus as focus_fixture
from management.services.ig_fit_consultation import capture_fit_consultation
from management.services.ig_product_inquiry_presentation import (
    fit_illustration_reference, prepare_fit_illustration, validate_fit_illustration_metadata,
)
from management.services.ig_response_control import parse_structured_response
from management.services.ig_revision_catalog_assets import capture_fit_photo_capability, prepare_catalog_asset_control
from management.services.ig_revision_authority import CLAIM_CATALOG_ASSET_REFERENCES, build_revision_authority_bindings, check_fact_bindings
from management.services.ig_revision_outbox import _digest, _normalize_specs
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth


class FitPhotoClaimTests(SimpleTestCase):
    def test_merchant_alt_must_describe_one_positive_cut(self):
        from management.services.ig_revision_catalog_assets import _explicit_photo_fit

        self.assertEqual(_explicit_photo_fit('Біла oversize-футболка TwoComms «Правил немає»'), "oversize")
        self.assertEqual(_explicit_photo_fit("Класична футболка з іншим принтом"), "classic")
        self.assertEqual(_explicit_photo_fit("Regular-fit T-shirt with another print"), "classic")
        for alt in ("", "Не oversize футболка", "classic або oversize", "Чи класична футболка?",
                    'Принт «Oversize» на футболці', 'Футболка з принтом “Classic”', "Футболка з принтом ‘Classic’",
                    "Футболка Classic з великим принтом", "classic"):
            self.assertEqual(_explicit_photo_fit(alt), "", alt)

    def test_photo_existence_never_authorizes_actual_stock(self):
        for text in (
            "Саме для цього принта зараз є фото oversize, а окремого фото класики поки немає.",
            "Для этого принта есть фото oversize, отдельного фото классики пока нет.",
            "We have a photo of the oversize version; no separate classic photo is confirmed.",
        ):
            self.assertTrue(validate_reply_truth(text, context=ReplyTruthContext()).valid, text)
        for text in ("Є фото oversize. Футболка є в наявності.",
                     "Є фото oversize, є розмір M.", "Есть фото oversize, есть размер M.",
                     "Є фото oversize, розмір M є в наявності.",
                     "We have a photo of oversize, and the T-shirt is in stock.", "Oversize is available."):
            self.assertIn("unverified_availability", validate_reply_truth(text, context=ReplyTruthContext()).reasons, text)


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class FitIllustrationTests(TransactionTestCase):
    reset_sequences = True
    _message = focus_fixture.ProductFocusTests._message
    _prepare = focus_fixture.ProductFocusTests._prepare
    _generate = focus_fixture.ProductFocusTests._generate
    followup = focus_fixture.ProductFocusTests.followup

    def setUp(self):
        focus_fixture.ProductFocusTests.setUp(self)
        from productcolors.models import ProductColorImage
        from storefront.models import ProductFitOption

        directory = tempfile.TemporaryDirectory(prefix="fit-illustration-test-")
        self.addCleanup(directory.cleanup)
        override = override_settings(MEDIA_ROOT=directory.name, MEDIA_URL="/media/")
        override.enable()
        self.addCleanup(override.disable)
        for fit in ("classic", "oversize"):
            ProductFitOption.objects.create(product=self.product, code=fit, label=fit,
                is_default=fit == "classic", is_active=True)
        self.variant = self.product.color_variants.get()
        self.image = ProductColorImage.objects.create(variant=self.variant, image="product_colors/85.jpg",
            alt_text='Біла oversize-футболка TwoComms «Правил немає»', order=0)
        self.unknown = ProductColorImage.objects.create(variant=self.variant, image="product_colors/classic_86.jpg", order=1)
        for name in (self.image.image.name, self.unknown.image.name):
            target = Path(directory.name, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"local public image fixture")

    def _prepared(self, *, text="Покажіть приклади classic та oversize крою"):
        self.followup(text)
        context = capture_fit_consultation(self.revision)
        response = parse_structured_response({"reply_text": "Класичний крій прямий, оверсайз вільніший.",
            "controls": [{"kind": "show_products", "value": str(self.product.pk)}]})
        prepared = prepare_fit_illustration(self.revision, response, self.settings, context=context)
        self.assertTrue(prepared.ready, prepared.reasons)
        return prepared, context

    def test_metadata_capture_proves_oversize_alt_and_rejects_classic_filename_without_io(self):
        with patch.object(FileSystemStorage, "open", side_effect=AssertionError("open")), \
                patch.object(FileSystemStorage, "size", side_effect=AssertionError("size")), \
                patch.object(FileSystemStorage, "exists", side_effect=AssertionError("exists")), \
                patch.object(FileSystemStorage, "path", side_effect=AssertionError("path")), transaction.atomic():
            capability = capture_fit_photo_capability(self.product.pk, metadata_only=True)
        self.assertEqual(capability["error"], "")
        self.assertEqual(capability["supported_fits"], ["classic", "oversize"])
        self.assertEqual(capability["missing_fit_photos"], ["classic"])
        self.assertEqual([row["fit_code"] for row in capability["photos"]], ["oversize", ""])

    def test_focused_oversize_photo_discloses_missing_classic_without_new_choice(self):
        prepared, _context = self._prepared()
        self.assertIn("підтвердженого фото", prepared.copy_hint)
        native = prepared.effects[0]["payload"]["message"]["attachment"]["payload"]
        self.assertEqual(len(native["elements"]), 2)
        self.assertEqual(native["elements"][0]["title"], "Класичний крій · Спереду")
        self.assertIn("Чиста основа", native["elements"][0]["subtitle"])
        self.assertIn("без обговорюваного принта", native["elements"][0]["subtitle"])
        self.assertEqual(native["elements"][1]["title"], "Оверсайз")
        manifest = prepared.effects[0]["projection_metadata"]["fit_illustration"]
        blank = manifest["items"][0]
        self.assertEqual(blank["asset_kind"], "blank_constructor_preview")
        self.assertEqual(blank["base_key"], "tshirt:regular")
        self.assertEqual(blank["fit_code"], "classic")
        self.assertFalse(blank["exact_design_photo"])
        self.assertTrue(blank["asset_version"])
        self.assertEqual(manifest["focus"]["product_id"], self.product.pk)
        self.assertFalse(any("buttons" in row or "default_action" in row for row in native["elements"]))
        self.assertTrue(_normalize_specs(prepared.effects, self.customer.igsid))
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)

    def test_other_design_is_labeled_fit_example_without_shown_product_projection(self):
        from productcolors.models import ProductColorImage, ProductColorVariant
        from storefront.models import Product
        from types import SimpleNamespace
        from management.services.ig_revision_reply_projection import _project_part

        other = Product.objects.create(title="Футболка Інший принт", slug="fit-example-other",
            category=self.product.category, status="published", price=500)
        variant = ProductColorVariant.objects.create(product=other, color=self.variant.color, stock=0)
        image = ProductColorImage.objects.create(variant=variant, image="product_colors/example.jpg",
            alt_text="Класична футболка з іншим принтом")
        Path(image.image.path).write_bytes(b"a real local fixture image")
        prepared, context = self._prepared()
        self.assertEqual(context["fit_photo_capability"]["example_product_ids"], [other.pk])
        manifest = prepared.effects[0]["projection_metadata"]["fit_illustration"]
        example = next(row for row in manifest["items"] if row["role"] == "example")
        self.assertEqual(example["product_id"], other.pk)
        self.assertIn("Інший принт", prepared.effects[0]["payload"]["message"]["attachment"]["payload"]["elements"][0]["subtitle"])
        native = prepared.effects[0]
        effect = SimpleNamespace(pk=999, payload=native["payload"], payload_digest=_digest(native["payload"]),
            projection_metadata=native["projection_metadata"], projection_digest=_digest(native["projection_metadata"]),
            group="template", kind="template", purpose="normal_reply", provider_namespace="instagram_login:owner-1",
            provider_message_id="fit-example-sent", terminal_at=timezone.now(), generation_request_id="", generation_model="")
        message, shown = _project_part(self.customer, effect)
        self.assertIsNone(shown)
        self.assertIn("Інший принт", message.text)
        self.assertNotIn("картки товарів", message.text)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)

    def test_rejected_native_fallback_cannot_show_images_before_disclosure_sent(self):
        from productcolors.models import ProductColorImage, ProductColorVariant
        from storefront.models import Product
        from unittest.mock import Mock
        from management.services.ig_revision_outbox import PublicationBinding, plan_revision_effects
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group

        other = Product.objects.create(title="Футболка інший принт", slug="fallback-example-other",
            category=self.product.category, status="published", price=500)
        variant = ProductColorVariant.objects.create(product=other, color=self.variant.color)
        image = ProductColorImage.objects.create(variant=variant, image="product_colors/fallback-example.jpg",
            alt_text="Класична футболка з іншим принтом")
        Path(image.image.path).write_bytes(b"a real local example fixture")
        prepared, _context = self._prepared()
        control, reasons = prepare_catalog_asset_control(self.customer, prepared.effects)
        self.assertFalse(reasons)
        authority = build_revision_authority_bindings(self.customer,
            claims=(CLAIM_CATALOG_ASSET_REFERENCES,), control=control, settings_obj=self.settings)
        self.assertTrue(authority.ready, authority.reasons)
        planned = plan_revision_effects(self.revision.pk, self.token,
            source_message_id=self.revision.sources.get().message_id, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash),
            authority_context_digest=authority.authority_digest, effects=prepared.effects,
            fact_bindings=authority.fact_bindings, fact_checker=check_fact_bindings)
        self.assertTrue(planned.effects, planned.reasons)
        drain_group(self.revision.pk, self.token, "template", lambda _payload: ProviderPartResult(
            "instagram_login:owner-1", http_status=400, outcome="explicit_rejected"), fact_checker=check_fact_bindings)
        payloads = []
        def unknown_preface(payload):
            payloads.append(payload)
            return ProviderPartResult("instagram_login:owner-1", outcome="timeout")
        result = drain_group(self.revision.pk, self.token, "template_fallback", unknown_preface,
            fact_checker=check_fact_bindings)
        self.assertEqual(result.state, "unknown")
        self.assertEqual(len(payloads), 1)
        self.assertIn("Інший принт", payloads[0]["message"]["text"])
        self.assertNotIn("attachment", payloads[0]["message"])
        self.assertFalse(self.revision.delivery_effects.filter(kind="image", state__in=("sent", "provider_started")).exists())
        replay = Mock()
        drain_group(self.revision.pk, self.token, "template_fallback", replay, fact_checker=check_fact_bindings)
        replay.assert_not_called()

    def test_explicit_size_chart_keeps_its_existing_image_owner(self):
        from types import SimpleNamespace
        from management.services.ig_revision_live import _prepare_visible_content

        self.followup("Покажіть таблицю розмірів classic футболки")
        response = parse_structured_response({"reply_text": "Таблиця розмірів.", "controls": []})
        self.assertFalse(prepare_fit_illustration(self.revision, response, self.settings).handled)
        chart = SimpleNamespace(handled=True, effects=("exact-chart",), reasons=())
        with patch("management.services.ig_revision_live._catalog_plain_chart_requested", return_value=True), \
                patch("management.services.ig_catalog_choice_presentations.prepare_catalog_chart_effect", return_value=chart), \
                patch("management.services.ig_product_inquiry_presentation.prepare_fit_illustration", side_effect=AssertionError("chart replaced")):
            effects, _reply, reasons = _prepare_visible_content(self.revision, response, self.settings)
        self.assertEqual(effects, ("exact-chart",))
        self.assertFalse(reasons)

    def test_asset_authority_rechecks_alt_and_native_owner_without_file_io(self):
        from productcolors.models import ProductColorImage
        prepared, _context = self._prepared()
        control, reasons = prepare_catalog_asset_control(self.customer, prepared.effects)
        self.assertFalse(reasons, reasons)
        with patch.object(FileSystemStorage, "open", side_effect=AssertionError("open")), \
                patch.object(FileSystemStorage, "size", side_effect=AssertionError("size")), \
                patch.object(FileSystemStorage, "path", side_effect=AssertionError("path")), transaction.atomic():
            authority = build_revision_authority_bindings(self.customer,
                claims=(CLAIM_CATALOG_ASSET_REFERENCES,), control=control, settings_obj=self.settings)
            self.assertTrue(authority.ready, authority.reasons)
            self.assertTrue(check_fact_bindings(authority.fact_bindings, revision=self.revision,
                client=self.customer, settings_obj=self.settings))
        ProductColorImage.objects.filter(pk=self.image.pk).update(alt_text="Класична футболка")
        self.assertFalse(check_fact_bindings(authority.fact_bindings, revision=self.revision,
            client=self.customer, settings_obj=self.settings))

    def test_detached_payload_or_fit_label_cannot_enter_outbox(self):
        prepared, _context = self._prepared()
        row = deepcopy(prepared.effects[0])
        row["payload"]["message"]["attachment"]["payload"]["elements"][0]["title"] = "Класичний крій"
        self.assertFalse(validate_fit_illustration_metadata(row["projection_metadata"], payload=row["payload"], group="template", kind="template"))
        with self.assertRaisesRegex(ValueError, "projection"):
            _normalize_specs([row], self.customer.igsid)

    def _execute_current(self, *, unknown=False):
        from management.services.ig_revision_live import execute_claimed_revision

        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("unexpected Gemini I/O")) as generate, \
                patch("management.services.instagram_bot.get_page_token", return_value="fit-fixture-token"), \
                patch("management.services.instagram_bot._provider_http", side_effect=TimeoutError() if unknown else
                    lambda *_args, **_kwargs: (200, json.dumps({"message_id": "fit-facts-sent"}))) as http:
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        return result, generate, http

    def test_pure_proved_audience_is_answered_without_provider_graph_or_manager_debt(self):
        from management.models import GeminiRequest, IgFollowUpTask

        self.followup("А жіночі які?")
        result, generate, http = self._execute_current()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        http.assert_called_once()
        self.assertFalse(GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{self.revision.pk}").exists())
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["source_preference_fallback"]
        self.assertEqual(receipt["reason"], "approved_fit_facts")
        self.assertEqual(receipt["failed_request_id"], "")
        self.assertIn("унісекс", receipt["reply_text"])
        self.assertEqual(receipt["authority"]["allowed_actions"], [])
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").exists())
        _, replay_generate, replay_http = self._execute_current()
        replay_generate.assert_not_called()
        replay_http.assert_not_called()

    def test_mixed_price_request_cannot_take_fast_fact_admission(self):
        from management.services.ig_revision_input import record_source_preference_fallback

        self.followup("А жіночі які? Скільки коштує ця футболка?")
        result = record_source_preference_fallback(self.revision.pk, self.token,
            settings_id=self.settings.pk, approved_fit_facts=True)
        self.assertFalse(result.ready)
        self.assertEqual(result.reason, "fallback_not_pure_approved_fit_facts")
        self.revision.refresh_from_db()
        self.assertNotIn("source_preference_fallback", self.revision.action_receipts)

    def test_unknown_fast_fact_delivery_never_retries_or_creates_provider_graph(self):
        from management.models import GeminiRequest

        self.followup("А жіночі які?")
        first, generate, http = self._execute_current(unknown=True)
        self.assertEqual(first.state, "delivery_pending", first.reasons)
        generate.assert_not_called()
        http.assert_called_once()
        second, generate, http = self._execute_current()
        self.assertEqual(second.state, "delivery_pending")
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{self.revision.pk}").exists())

    def test_failed_graph_gets_fit_facts_without_a_fake_winner(self):
        from management.models import GeminiRequest
        from management.services.ig_revision_input import record_source_preference_fallback

        self.followup("А жіночі які?")
        self._reduce_current()
        source = self.revision.sources.get().message
        graph = GeminiRequest.objects.create(request_id="fit-failed-graph", client_id=self.customer.pk,
            source_message_id=source.pk, logical_turn_id=f"ig-revision:{self.revision.pk}", terminal_resolution="failed")
        admitted = record_source_preference_fallback(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertTrue(admitted.ready, admitted.reason)
        self.assertEqual(admitted.receipt["proof"]["kind"], "fit_consultation_fallback")
        result, generate, http = self._execute_current()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        http.assert_called_once()
        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertIsNone(graph.winner_attempt_id)

    def test_failed_graph_after_zvychna_acknowledges_classic_and_asks_size_for_known_print(self):
        from management.models import GeminiRequest
        from management.services.ig_revision_input import record_source_preference_fallback
        from storefront.models import Catalog, SizeGrid

        catalog = Catalog.objects.create(name="Fallback shirts", slug="fallback-shirts")
        grid = SizeGrid.objects.create(catalog=catalog, name="Fallback sizes", guide_data={
            "columns": [{"key": "size", "label": "Size"}], "rows": [{"size": "M"}, {"size": "L"}]})
        self.product.catalog, self.product.size_grid = catalog, grid
        self.product.save(update_fields=["catalog", "size_grid"])
        focus_fixture.ProductFocusTests.deliver_human_fit_question(self)
        self.followup("Звична")
        self._reduce_current()
        self.customer.refresh_from_db()
        source = self.revision.sources.get().message
        graph = GeminiRequest.objects.create(request_id="zvychna-failed-graph", client_id=self.customer.pk,
            source_message_id=source.pk, logical_turn_id=f"ig-revision:{self.revision.pk}", terminal_resolution="failed")
        admitted = record_source_preference_fallback(self.revision.pk, self.token, settings_id=self.settings.pk)
        self.assertTrue(admitted.ready, admitted.reason)
        self.assertIn("класич", admitted.receipt["reply_text"].casefold())
        self.assertIn("розмір", admitted.receipt["reply_text"].casefold())
        self.assertNotIn("обрати принт", admitted.receipt["reply_text"].casefold())
        result, generate, http = self._execute_current()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        http.assert_called_once()
        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertIsNone(graph.winner_attempt_id)
        _, generate, http = self._execute_current()
        generate.assert_not_called()
        http.assert_not_called()

    def _reduce_current(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_revision_outbox import PublicationBinding

        reduced = reduce_revision_commerce(self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.revision.refresh_from_db()

    def test_failed_graph_fit_photo_fallback_uses_native_illustration_outbox(self):
        from management.models import GeminiRequest

        self.followup("Покажіть приклади classic та oversize крою")
        self._reduce_current()
        source = self.revision.sources.get().message
        graph = GeminiRequest.objects.create(request_id="fit-photo-failed-graph", client_id=self.customer.pk,
            source_message_id=source.pk, logical_turn_id=f"ig-revision:{self.revision.pk}", terminal_resolution="failed")
        counter = iter(("fit-photo-native", "fit-photo-text"))
        from management.services.ig_revision_live import execute_claimed_revision
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("unexpected Gemini")) as generate, \
                patch("management.services.instagram_bot.get_page_token", return_value="fit-fixture-token"), \
                patch("management.services.instagram_bot._provider_http", side_effect=lambda *_args, **_kwargs:
                    (200, json.dumps({"message_id": next(counter)}))) as http:
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 2)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["source_preference_fallback"]["proof"]["kind"], "fit_consultation_fallback")
        self.assertTrue(self.revision.delivery_effects.filter(group="template", state="sent").exists())
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.assertNotIn("shown_products", self.customer.sales_context or {})
        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "failed")
