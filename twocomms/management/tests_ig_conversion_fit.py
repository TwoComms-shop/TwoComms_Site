"""Conversion fit regressions: actual category, cuts, guides and media history."""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.services.ig_product_focus import information_subject, capture_product_focus
from management.services.ig_fit_consultation import build_fit_consultation, deterministic_fit_reply
from management.services.ig_catalog_configured_choices import size_measurements
from management.services.ig_fit_illustration_assets import blank_fit_illustrations
from management.tests_ig_fit_consultation import FOCUS, PHOTO, source


class FitCategoryAndMeasurementTests(SimpleTestCase):
    def test_different_information_subject_suspends_old_tshirt_facts(self):
        for text, garment in (("А жіночі худі у вас є?", "hoodie"),
                ("А які жіночі лонгсліви є?", "longsleeve"), ("А жіночі zip худі є?", "zip_hoodie")):
            with self.subTest(text=text):
                rows = [source(text)]
                context = build_fit_consultation(rows, focus=deepcopy(FOCUS), photo_capability=PHOTO,
                    approved_fact_keys=("current_tshirt_bases", "current_tshirt_audience", "current_hoodie_audience"))
                self.assertEqual(information_subject(rows)["garment_type"], garment)
                self.assertEqual(context["focus"], {})
                self.assertEqual(context["approved_fact_keys"], [])
                self.assertEqual(deterministic_fit_reply(context), "")
                self.assertEqual(FOCUS["product_id"], 112)

    def test_negated_quoted_and_several_categories_do_not_select_a_topic(self):
        for text in ("Худі не треба", "Не хочу худі", "Він сказав «хочу худі»"):
            self.assertEqual(information_subject([source(text)]), {}, text)
        self.assertEqual(information_subject([source("Які футболки та худі є?")])["status"], "ambiguous")
        self.assertEqual(information_subject([source("Худі не треба, які жіночі футболки?")])["garment_type"], "tshirt")

    def test_category_context_flags_allow_generic_current_bases_and_reject_specifics(self):
        for text in ("А жіночі худі у вас є?", "Які ваші актуальні худі?", "Можна фото фасонів худі?"):
            topic = information_subject([source(text)])
            self.assertFalse(topic["future_base_requested"], text)
            self.assertFalse(topic["unsupported_detail_requested"], text)
            self.assertFalse(topic["specific_product_requested"], text)
        for text in ("Це худі жіноче?", "Який матеріал цього худі?", "Худі з фото жіноче?",
                "Худі артикул 123?", "Худі https://twocomms.shop/product/example/ жіноче?"):
            self.assertTrue(information_subject([source(text)])["specific_product_requested"], text)
        for text in ("Які люверси у ваших худі?", "Які манжети у худі?", "Є худі з віскози?", "Худі без флісу?"):
            self.assertTrue(information_subject([source(text)])["unsupported_detail_requested"], text)
        for text in ("Які худі в наступній партії?", "Худі з нової колекції?", "Next batch hoodies?"):
            self.assertTrue(information_subject([source(text)])["future_base_requested"], text)
        fragmented = information_subject([source("А жіночі худі у вас є?"), source("А які люверси?", 21)])
        self.assertTrue(fragmented["unsupported_detail_requested"])
        self.assertFalse(information_subject([source('Які худі? Він написав «які люверси на цьому фото?»')])["unsupported_detail_requested"])

    def test_nounless_own_burst_questions_inherit_latest_category_proof_ids(self):
        for fragment in ("Який склад?", "А яка щільність?", "Яка посадка?", "А які люверси?"):
            topic = information_subject([source("А худі?", 20), source(fragment, 21)])
            self.assertEqual(topic["garment_type"], "hoodie")
            self.assertEqual(topic["source_message_ids"], [20, 21], fragment)
        for fragment in ('Він написав «який склад?»', "Склад не цікавить?", "Не цікавить склад?", "Раніше який склад був?"):
            topic = information_subject([source("А худі?", 20), source(fragment, 21)])
            self.assertEqual(topic["source_message_ids"], [20], fragment)
        topic = information_subject([source("А худі?", 20), source("Який склад?", 21), source("А футболки?", 22), source("Яка щільність?", 23)])
        self.assertEqual(topic["source_message_ids"], [22, 23])
        self.assertFalse(information_subject([source("Худі з бавовни?")])["unsupported_detail_requested"])
        self.assertTrue(information_subject([source("Худі з вовни?")])["unsupported_detail_requested"])

    def test_both_chart_request_uses_actual_supported_fits_and_respects_negation(self):
        from management.services.ig_fit_consultation import requested_chart_fits
        self.assertEqual(requested_chart_fits([source("Покажіть обидві сітки")], ["classic", "oversize"]), ["classic", "oversize"])
        self.assertEqual(requested_chart_fits([source("Show both charts")], ["oversize"]), ["oversize"])
        self.assertEqual(requested_chart_fits([source("Покажіть фото крою та розмірну сітку")], ["oversize"]), ["oversize"])
        self.assertEqual(requested_chart_fits([source("Покажіть обидві сітки")]), [])
        self.assertEqual(requested_chart_fits([source("Не показуйте обидві сітки")], ["classic", "oversize"]), [])
        self.assertEqual(requested_chart_fits([source("Покажіть таблиці розмірів classic та oversize")]), ["classic", "oversize"])

    def test_single_configured_cut_never_asks_regular_or_oversize(self):
        photo = {**PHOTO, "supported_fits": ["oversize"]}
        context = build_fit_consultation([source("Які фасони футболки є?")], focus=FOCUS,
            photo_capability=photo, approved_fact_keys=("current_tshirt_audience",))
        context["audience_requested"] = True
        context["next_consultation_selector"] = "fit"
        reply = deterministic_fit_reply(context)
        self.assertNotIn("звична чи вільніша", reply)
        self.assertIn("розмір", reply)

    def test_numeric_grid_is_exact_bounded_and_does_not_inherit_tshirt_tolerance(self):
        guide = NS(pk=6, guide_data={"columns": [{"key": "size", "label": "Size"},
            {"key": "width", "label": "Півобхват грудей"}], "rows": [
                {"size": "M", "width": "58"}, {"size": "L", "width": "60"}],
            "intro": "Заміри виробу у розкладеному вигляді, см", "notes": ["Допуск ±1 см"]})
        dto = size_measurements(guide, product_id=7, option_key="fit=oversize", allowed_sizes={"M", "L"})
        self.assertEqual(dto["rows"], [{"size": "M", "width": "58"}, {"size": "L", "width": "60"}])
        self.assertEqual(dto["unit"], "cm")
        self.assertEqual(dto["measurement_semantics"], "flat_garment")
        self.assertEqual(dto["notes"], ["Допуск ±1 см"])
        self.assertEqual(dto["tolerance_source_text"], ["Допуск ±1 см"])
        before = dto["guide_version"]
        guide.guide_data["rows"][0]["width"] = "59"
        self.assertNotEqual(size_measurements(guide, product_id=7)["guide_version"], before)
        guide.guide_data.pop("intro")
        guide.guide_data.pop("notes")
        unknown = size_measurements(guide, product_id=8)
        self.assertEqual(unknown["unit"], "unspecified")
        self.assertEqual(unknown["measurement_semantics"], "unspecified")
        self.assertEqual(unknown["notes"], [])
        self.assertEqual(unknown["tolerance_source_text"], [])
        self.assertEqual(size_measurements(None, product_id=8), {})
        guide.guide_data["rows"] *= 17
        self.assertEqual(size_measurements(guide, product_id=8), {})

    def test_plain_measurement_hook_keeps_exact_fit_guide_separate_from_fit_intent(self):
        from management.services.ig_fit_consultation import capture_consultation_measurements, fit_consultation_request
        revision = NS(bundle_snapshot={"sources": [source("У моєї зручної речі півобхват 58 см, який розмір?")]})
        self.assertEqual(fit_consultation_request(revision.bundle_snapshot["sources"]), {})
        cart = {"active_line_id": "one", "lines": [{"line_id": "one", "fields": {
            "product_id": {"status": "confirmed", "value": 112}, "fit_option_code": {"status": "confirmed", "value": "oversize"}}}]}
        guide = {"grid_id": 6, "guide_version": "exact", "option_key": "fit=oversize", "variant_id": None,
            "rows": [{"size": "M", "width": "58"}]}
        with patch("management.services.ig_catalog_configured_choices.capture_configured_choices", return_value={
            "configurations": [{"grid": {"measurements": guide}}], "configuration_digest": "bound"}) as capture:
            dto = capture_consultation_measurements(revision, product_focus=FOCUS, source_cart_capture=cart)
        capture.assert_called_once_with(112, metadata_only=True, fit_code="oversize")
        self.assertEqual(dto["guides"], [guide])
        self.assertTrue(dto["complete"])
        self.assertFalse(dto["selection_authority"])

    def test_blank_previews_have_explicit_base_view_version_and_no_fake_product(self):
        for category, fits, count in (("tshirts", ["classic", "oversize"], 4), ("hoodies", ["classic", "oversize"], 2)):
            rows = blank_fit_illustrations(NS(pk=112, category=NS(slug=category)), fits)
            self.assertEqual(len(rows), count)
            self.assertTrue(all(row["asset_kind"] == "blank_constructor_preview" for row in rows))
            self.assertTrue(all(row["product_id"] == 112 and not row["exact_design_photo"] for row in rows))
            self.assertTrue(all(row["asset_version"] and row["base_key"] for row in rows))
            self.assertEqual({row["view"] for row in rows}, {"front", "back"})
        self.assertEqual(blank_fit_illustrations(NS(pk=112, category=NS(slug="longsleeves")), ["classic"]), [])


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class LatestPhotoFocusTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        from management.tests_ig_product_focus import ProductFocusTests
        for name in ("_message", "_prepare", "_generate", "_execute", "followup"):
            setattr(self, name, getattr(ProductFocusTests, name).__get__(self))
        if self._testMethodName == "test_latest_proved_photo_survives_thirteen_older_owner_rows":
            real_message = self._message
            def with_history(text, mid):
                if mid == "focus-owned-photo":
                    from management.models import IgCustomerTurnRevision, IgTurnRevisionSource
                    for index in range(13):
                        previous = real_message("Попереднє фото", f"historical-photo-{index}")
                        # Historical auto_select rows without valid proof were
                        # sufficient to trip the old pre-proof lifetime cap.
                        older = IgCustomerTurnRevision.objects.create(client=self.customer, turn=self.turn,
                            revision=100 + index, active_slot=None, quiet_started_at=timezone.now(),
                            quiet_deadline=timezone.now(), quiet_cap_at=timezone.now(), overall_deadline=timezone.now(),
                            generation_proposal={"turn_intelligence": {"catalog_resolution": "auto_select"}})
                        IgTurnRevisionSource.objects.create(revision=older, message=previous, ordinal=1, role="user",
                            source_namespace=previous.provider_namespace, source_digest="invalid-historical-proof")
                return real_message(text, mid)
            self._message = with_history
        ProductFocusTests.setUp(self)

    def test_latest_proved_photo_survives_thirteen_older_owner_rows(self):
        revision = self.followup("А які є фасони?")
        focus = capture_product_focus(revision)
        self.assertEqual(focus["status"], "focused", focus)
        self.assertEqual(focus["product_id"], self.product.pk)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)

    def test_different_category_suspends_focus_but_return_to_original_recovers_it(self):
        revision = self.followup("А жіночі худі у вас є?")
        focus = capture_product_focus(revision)
        self.assertEqual(focus["reason"], "different_information_subject")
        self.assertEqual(focus["retained_discussed_product_id"], self.product.pk)
        self.assertEqual(capture_product_focus(self.followup("А жіночі футболки які?"))["product_id"], self.product.pk)


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class NativeBlankIllustrationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        from management.tests_ig_fit_illustration import FitIllustrationTests
        for name in ("_message", "_prepare", "_generate", "followup", "_prepared"):
            setattr(self, name, getattr(FitIllustrationTests, name).__get__(self))
        FitIllustrationTests.setUp(self)

    def test_combined_chart_and_fit_request_prepares_signed_honest_illustrations(self):
        from management.services.ig_product_inquiry_presentation import (
            validate_fit_illustration_metadata, fit_illustration_reference, validate_fit_illustration_reference)
        prepared, context = self._prepared(text="Покажіть таблицю розмірів та приклади classic крою")
        self.assertTrue(context["size_chart_requested"])
        self.assertTrue(context["photos_requested"])
        first = prepared.effects[0]
        self.assertTrue(validate_fit_illustration_metadata(first["projection_metadata"], payload=first["payload"],
            group=first["group"], kind=first["kind"]))
        reference = fit_illustration_reference(first["projection_metadata"]["fit_illustration"])
        self.assertTrue(any(row["role"] == "blank" for row in reference["items"]))
        from django.core.files.storage import FileSystemStorage
        with patch.object(FileSystemStorage, "path", side_effect=AssertionError("metadata CAS stat")):
            self.assertEqual(validate_fit_illustration_reference(reference, self.customer, self.settings), "")
        reference["items"][0]["asset_version"] = "unapproved"
        self.assertEqual(validate_fit_illustration_reference(reference, self.customer, self.settings), "fit_illustration_asset_changed")

    def test_hoodie_single_cut_uses_both_blank_views_without_fabricated_measurements(self):
        from storefront.models import Category
        from management.services.ig_fit_consultation import capture_fit_consultation
        from management.services.ig_product_inquiry_presentation import prepare_fit_illustration
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_revision_catalog_assets import capture_fit_photo_capability
        from productcolors.models import ProductColorImage
        category = Category.objects.create(name="Худі", slug="hoodies")
        self.product.category, self.product.title = category, "Худі Правил немає"
        self.product.save(update_fields=["category", "title"])
        # Force the approved blank fallback, not a filename-based real photo.
        ProductColorImage.objects.filter(variant=self.variant).delete()
        self.followup("Покажіть фото крою худі, жіночі є?")
        capability = capture_fit_photo_capability(self.product.pk, metadata_only=True)
        self.assertEqual(capability["supported_fits"], ["oversize"])
        self.assertEqual({row["fit_code"] for row in capability["illustrations"]}, {"oversize"})
        context = capture_fit_consultation(self.revision)
        self.assertEqual(context["size_measurements"], [])
        prepared = prepare_fit_illustration(self.revision, parse_structured_response({"reply_text": "Покажу приклад крою.", "controls": []}),
            self.settings, context=context)
        self.assertTrue(prepared.ready, prepared.reasons)
        items = prepared.effects[0]["projection_metadata"]["fit_illustration"]["items"]
        self.assertEqual({row["view"] for row in items}, {"front", "back"})
        self.assertTrue(all(row["role"] == "blank" and row["fit_code"] == "oversize" for row in items))
        self.assertNotIn("classic", str(items))
        self.assertIn("preview", prepared.copy_hint)

    def test_hoodie_photo_and_missing_chart_complete_with_single_fit_absence_proof(self):
        import json
        from storefront.models import Category
        from productcolors.models import ProductColorImage
        from management.tests_ig_revision_live import RevisionLiveTests
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_revision_outbox import PublicationBinding
        category = Category.objects.create(name="Худі", slug="hoodies")
        self.product.category, self.product.title = category, "Худі Правил немає"
        self.product.save(update_fields=["category", "title"])
        ProductColorImage.objects.filter(variant=self.variant).delete()
        self.followup("Можна фото крою та розмірну сітку худі?")
        reduced = reduce_revision_commerce(self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.revision.refresh_from_db()
        self.actual_inline_count = 0
        self.parsed = {"reply_text": "У поточного худі крій злегка oversize, помірно вільний. Можу показати чисту основу.",
            "controls": [{"kind": "show_products", "value": str(self.product.pk)}]}
        result, _, http = RevisionLiveTests._execute(self, [(200, json.dumps({"message_id": f"hoodie-absence-{index}"})) for index in range(8)])
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts.get("response_coverage")))
        fit = self.revision.delivery_effects.get(projection_metadata__delivery="native")
        self.assertEqual(fit.state, "sent")
        self.assertEqual(fit.projection_metadata["fit_illustration"]["unavailable_chart_fits"], ["oversize"])
        self.assertTrue(all(row["fit_code"] == "oversize" for row in fit.projection_metadata["fit_illustration"]["items"]))
        self.assertFalse(self.revision.action_receipts["response_coverage"]["remaining"])


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class CombinedNativeChartFitTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        from management.tests_ig_discussed_chart import DiscussedChartTests
        from management.tests_ig_revision_live import RevisionLiveTests
        for name in ("_message", "_prepare", "_generate", "followup", "configure_fit_catalog", "deliver_human_fit_question", "_reduce_current"):
            setattr(self, name, getattr(DiscussedChartTests, name).__get__(self))
        self._execute_generated = RevisionLiveTests._execute.__get__(self)
        DiscussedChartTests.setUp(self)
        self.followup("Покажіть таблицю розмірів та приклади classic крою")
        self._reduce_current()
        self.parsed = {"reply_text": "Classic — звична пряма посадка. Покажу приклад крою та розмірну сітку цього принта.",
            "controls": [{"kind": "show_products", "value": str(self.product.pk)}]}

    def test_real_mixed_request_sends_both_signed_parts_and_retry_sends_nothing(self):
        import json
        from management.models import IgFollowUpTask
        result, generate, http = self._execute_generated([(200, json.dumps({"message_id": f"combined-{index}"})) for index in range(8)])
        self.revision.refresh_from_db()
        from management.services.ig_response_debt import response_coverage
        self.assertEqual(result.state, "completed", (result.reasons, response_coverage(self.revision)))
        self.assertEqual(http.call_count, 3)
        self.revision.refresh_from_db()
        parts = list(self.revision.delivery_effects.order_by("order_index"))
        chart = next(row for row in parts if "catalog_chart" in row.projection_metadata)
        fit = next(row for row in parts if "fit_illustration" in row.projection_metadata and row.projection_metadata["delivery"] == "native")
        self.assertEqual(chart.state, "sent")
        self.assertEqual(fit.state, "sent")
        self.assertEqual(fit.projection_metadata["part_index"], 1)
        self.assertEqual(fit.projection_metadata["fit_illustration"]["native_part_index"], 1)
        self.assertEqual(fit.projection_metadata["fit_illustration"]["focus"]["product_id"], self.product.pk)
        binding = next(row for row in fit.fact_bindings if row["claim"] == "catalog_asset_references")
        self.assertEqual(set(binding["selector"]), {"combined_asset_reference"})
        self.assertEqual(set(binding["selector"]["combined_asset_reference"]), {"schema", "scope", "fit", "charts"})
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").exists())
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        _, retry_model, retry_http = self._execute_generated()
        retry_model.assert_not_called()
        retry_http.assert_not_called()

    def test_combined_refs_recheck_both_signed_assets_without_filesystem_io(self):
        from django.core.files.storage import FileSystemStorage
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_revision_live import _prepare_effects
        from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control, build_catalog_asset_reference_binding
        from management.services.ig_revision_outbox import _normalize_specs
        plan = capture_response_plan(self.customer, revision=self.revision)
        effects, reasons = _prepare_effects(self.revision, parse_structured_response(self.parsed), self.settings, response_plan=plan)
        self.assertFalse(reasons, reasons)
        self.assertTrue(_normalize_specs(effects, self.customer.igsid))
        control, reasons = prepare_catalog_asset_control(self.customer, effects)
        self.assertFalse(reasons)
        self.assertEqual(set(control), {"catalog_chart_reference", "fit_illustration_reference"})
        with patch.object(FileSystemStorage, "path", side_effect=AssertionError("CAS filesystem I/O")):
            binding, reason = build_catalog_asset_reference_binding(self.customer, control)
        self.assertEqual(reason, "")
        self.assertEqual(binding["subjects"]["product_ids"], [self.product.pk])
        import json
        self.assertLessEqual(len(json.dumps(binding, ensure_ascii=False, separators=(",", ":")).encode()), 4096)
        with patch.object(FileSystemStorage, "path", side_effect=AssertionError("compact CAS filesystem I/O")):
            self.assertEqual(build_catalog_asset_reference_binding(self.customer, binding["selector"]), (binding, ""))
        changed = deepcopy(control)
        changed["catalog_chart_reference"]["hmac"] = "0" * 64
        self.assertTrue(build_catalog_asset_reference_binding(self.customer, changed)[1])
        changed = deepcopy(control)
        changed["fit_illustration_reference"]["hmac"] = "0" * 64
        self.assertTrue(build_catalog_asset_reference_binding(self.customer, changed)[1])

    def test_fit_rejection_activates_its_own_fallback_after_one_chart(self):
        import json
        outcomes = [(200, json.dumps({"message_id": "combined-chart-ok"})),
            (400, json.dumps({"error": {"code": 100, "message": "unsupported template"}}))]
        outcomes.extend((200, json.dumps({"message_id": f"combined-fallback-{index}"})) for index in range(8))
        result, _, http = self._execute_generated(outcomes)
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        parts = list(self.revision.delivery_effects.order_by("order_index"))
        chart = [row for row in parts if "catalog_chart" in row.projection_metadata]
        self.assertEqual(len(chart), 1)
        self.assertEqual(chart[0].state, "sent")
        fallback = [row for row in parts if row.group == "template_fallback"]
        self.assertTrue(fallback)
        self.assertTrue(all(row.activation_part_index == 1 for row in fallback))
        self.assertTrue(all(row.state == "sent" for row in fallback))
        _, _, retry = self._execute_generated()
        retry.assert_not_called()

    def test_unknown_fit_after_sent_chart_is_never_retried(self):
        import json
        result, _, http = self._execute_generated([(200, json.dumps({"message_id": "combined-chart-before-unknown"})), TimeoutError(), (200, json.dumps({"message_id": "independent-text"}))])
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.assertIn("delivery_unknown", result.reasons)
        self.assertEqual(http.call_count, 3)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.filter(projection_metadata__delivery="native").get().state, "unknown")
        self.assertFalse(self.revision.delivery_effects.filter(group="template_fallback", state="sent").exists())
        _, model, retry = self._execute_generated()
        model.assert_not_called()
        retry.assert_not_called()

    def _both_requested_charts(self):
        from pathlib import Path
        from storefront.models import SizeGrid
        from product_catalog.models import ProductOptionSizeGrid
        ProductOptionSizeGrid.objects.create(product=self.product, option_key="fit=classic", size_grid=self.grid)
        self.oversize_grid = SizeGrid.objects.create(catalog=self.product.catalog, name="Fixture exact oversize tee",
            image="size_grids/fixture-oversize.webp", guide_data={"columns": [{"key": "size", "label": "Size"},
                {"key": "width", "label": "Width"}], "rows": [{"size": "M", "width": "58"}, {"size": "L", "width": "60"}],
                "intro": "Fixture T-shirt measurements laid flat, cm. Tolerance ±1 cm."})
        target = Path(self.oversize_grid.image.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture exact oversize T-shirt chart")
        ProductOptionSizeGrid.objects.create(product=self.product, option_key="fit=oversize", size_grid=self.oversize_grid)
        self.followup("Покажіть фасони й обидві сітки")
        self._reduce_current()
        self.parsed = {"reply_text": "Classic — звична пряма посадка, oversize — вільніша. Покажу приклади крою й обидві сітки.",
            "controls": [{"kind": "show_products", "value": str(self.product.pk)}]}

    def test_both_requested_fit_charts_and_examples_are_delivered_without_new_selection(self):
        import json
        from management.services.ig_response_plan import capture_response_plan
        self._both_requested_charts()
        plan = capture_response_plan(self.customer, revision=self.revision)
        obligations = [row for row in plan.obligations if row["kind"] == "info:size_chart"]
        self.assertEqual({row.get("fit_code") for row in obligations}, {"classic", "oversize"})
        result, _, http = self._execute_generated([(200, json.dumps({"message_id": f"two-charts-{index}"})) for index in range(10)])
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts.get("response_coverage")))
        self.assertEqual(http.call_count, 4)
        charts = [row for row in self.revision.delivery_effects.all() if "catalog_chart" in row.projection_metadata]
        self.assertEqual(len(charts), 2)
        self.assertTrue(all(row.state == "sent" for row in charts))
        self.assertEqual({row.projection_metadata["catalog_chart"]["configuration_selector"]["fit_code"] for row in charts}, {"classic", "oversize"})
        self.assertEqual({row.part_index for row in charts}, {0, 1})
        fit = self.revision.delivery_effects.get(projection_metadata__delivery="native")
        self.assertEqual(fit.part_index, 2)
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.assertFalse(self.revision.action_receipts["response_coverage"]["remaining"])
        _, _, retry = self._execute_generated()
        retry.assert_not_called()

    def test_two_chart_reference_rechecks_changed_second_grid(self):
        from management.services.ig_catalog_choice_presentations import prepare_catalog_chart_effect
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control, build_catalog_asset_reference_binding
        self._both_requested_charts()
        prepared = prepare_catalog_chart_effect(self.revision, parse_structured_response(self.parsed), self.settings)
        self.assertTrue(prepared.ready, prepared.reasons)
        self.assertEqual(len(prepared.effects), 2)
        control, reasons = prepare_catalog_asset_control(self.customer, prepared.effects)
        self.assertFalse(reasons)
        binding, reason = build_catalog_asset_reference_binding(self.customer, control)
        self.assertEqual(reason, "")
        from django.core.files.storage import FileSystemStorage
        with patch.object(FileSystemStorage, "path", side_effect=AssertionError("two chart CAS I/O")):
            self.assertEqual(build_catalog_asset_reference_binding(self.customer, binding["selector"]), (binding, ""))
            self.oversize_grid.image = "size_grids/changed-oversize.webp"
            self.oversize_grid.save(update_fields=["image"])
            self.assertTrue(build_catalog_asset_reference_binding(self.customer, binding["selector"])[1])

    def test_missing_one_requested_chart_sends_other_chart_with_honest_absence(self):
        import json
        self._both_requested_charts()
        self.oversize_grid.image = ""
        self.oversize_grid.save(update_fields=["image"])
        result, _, http = self._execute_generated([(200, json.dumps({"message_id": f"one-missing-{index}"})) for index in range(10)])
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts.get("response_coverage")))
        charts = [row for row in self.revision.delivery_effects.all() if "catalog_chart" in row.projection_metadata]
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0].projection_metadata["catalog_chart"]["unavailable_requested_fits"], ["oversize"])
        text = " ".join(str(row.payload.get("message", {}).get("text", "")) for row in self.revision.delivery_effects.filter(group="substantive_text"))
        self.assertIn("oversize", text)
        self.assertIn("Немає підтвердженого зображення", text)
        self.assertFalse(self.revision.action_receipts["response_coverage"]["remaining"])

    def test_classic_illustration_does_not_replace_a_retained_oversize_choice(self):
        import json
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self._both_requested_charts()
        self.followup("oversize, M")
        self._reduce_current()
        self.parsed = {"reply_text": "Для футболки «Правил немає» ви обрали розмір M. Ви обрали посадку оверсайз.",
            "controls": [{"kind": "product", "value": str(self.product.pk)}, {"kind": "fit", "value": "oversize"}, {"kind": "size", "value": "M"}]}
        result, _, _ = self._execute_generated([(200, json.dumps({"message_id": f"retain-oversize-{index}"})) for index in range(6)])
        self.assertEqual(result.state, "completed", result.reasons)
        before = capture_current_selection_lines(self.customer.pk)
        before_fit = next(row for row in before["lines"] if row["line_id"] == before["active_line_id"])["fields"]["fit_option_code"]
        self.assertEqual(before_fit["value"], "oversize")
        self.followup("Покажіть приклади classic крою")
        self._reduce_current()
        after = capture_current_selection_lines(self.customer.pk)
        after_fit = next(row for row in after["lines"] if row["line_id"] == after["active_line_id"])["fields"]["fit_option_code"]
        self.assertEqual(after_fit, before_fit)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, self.product.pk)

    def test_no_requested_chart_images_discloses_absence_without_substituting_photos(self):
        from management.services.ig_revision_live import _prepare_visible_content
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_response_plan import capture_response_plan
        self._both_requested_charts()
        type(self.grid).objects.filter(pk__in=[self.grid.pk, self.oversize_grid.pk]).update(image="")
        plan = capture_response_plan(self.customer, revision=self.revision)
        response = parse_structured_response({"reply_text": "Можу показати приклади крою.", "controls": []})
        effects, reply, reasons = _prepare_visible_content(self.revision, response, self.settings, response_plan=plan)
        self.assertFalse(reasons)
        self.assertFalse(any("catalog_chart" in (row.get("projection_metadata") or {}) for row in effects))
        self.assertTrue(any("fit_illustration" in (row.get("projection_metadata") or {}) for row in effects))
        self.assertIn("Немає підтвердженого зображення", reply)
        self.assertIn("classic, oversize", reply)
        import json
        self.parsed = {"reply_text": "Classic — звична пряма посадка, oversize — вільніша. Можу показати приклади крою.",
            "controls": [{"kind": "show_products", "value": str(self.product.pk)}]}
        result, _, http = self._execute_generated([(200, json.dumps({"message_id": f"all-missing-{index}"})) for index in range(8)])
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts.get("response_coverage")))
        fit = self.revision.delivery_effects.get(projection_metadata__delivery="native")
        self.assertEqual(fit.state, "sent")
        self.assertEqual(fit.projection_metadata["fit_illustration"]["unavailable_chart_fits"], ["classic", "oversize"])
        self.assertTrue(self.revision.delivery_effects.filter(group="substantive_text", state="sent").exists())
        self.assertFalse(self.revision.action_receipts["response_coverage"]["remaining"])
        _, _, retry = self._execute_generated()
        retry.assert_not_called()

    def test_failed_graph_both_charts_uses_native_fallback_without_reopening_model(self):
        import json
        from management.models import GeminiRequest
        from management.services.ig_revision_live import execute_claimed_revision
        self._both_requested_charts()
        source = self.revision.sources.get().message
        graph = GeminiRequest.objects.create(request_id="both-charts-failed-graph", client_id=self.customer.pk,
            source_message_id=source.pk, logical_turn_id=f"ig-revision:{self.revision.pk}", terminal_resolution="failed")
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("provider reopened")) as generate, \
                patch("management.services.instagram_bot.get_page_token", return_value="fixture-token"), \
                patch("management.services.instagram_bot._provider_http", side_effect=[
                    (200, json.dumps({"message_id": f"local-both-{index}"})) for index in range(10)]) as http:
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", (result.reasons, self.revision.action_receipts.get("response_coverage")))
        generate.assert_not_called()
        sent = list(self.revision.delivery_effects.filter(state="sent"))
        self.assertEqual(http.call_count, len(sent))
        self.assertEqual(sum("catalog_chart" in row.projection_metadata for row in sent), 2)
        self.assertEqual(sum(row.projection_metadata.get("delivery") == "native" for row in sent), 1)
        self.assertFalse(self.revision.action_receipts["response_coverage"]["remaining"])
        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertIsNone(graph.winner_attempt_id)
