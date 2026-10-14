"""Real catalogue -> immutable outbox receipt -> own source -> cart integration.

Only HTTP and the separate echo registry are replaced. No delivery witness,
configuration capture, native admission, snapshot or terminal status is forged.
These TransactionTestCase tests can run against the isolated MariaDB test DB.
"""
import base64
import json
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_delivery as delivery_fixtures
from management import tests_ig_source_cart_catalog as catalog_fixtures
from management.models import (
    IgClient, IgCommercialEpisode, IgCommerceSelectionSession, IgCustomerTurn,
    IgRevisionDeliveryEffect, IgTurnMessage, InstagramBotMessage,
)
from management.services import ig_catalog_choice_presentations as presentations
from management.services.ig_catalog_choice_actions import consume_catalog_choice
from management.services.ig_catalog_configured_choices import capture_configured_choices
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_commerce_source_identity import resolve_source_product_request
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_response_control import ResponseControl, ValidatedResponse
from management.services.ig_response_plan import capture_response_plan
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_ASSET_REFERENCES, build_revision_authority_bindings, check_fact_bindings,
)
from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control
from management.services.ig_revision_delivery import drain_group
from management.services.ig_revision_execution import finalize_sent_revision_effects
from management.services.ig_revision_outbox import _canonical, plan_revision_effects
from management.services.ig_revision_reply_projection import project_sent_reply
from management.services.ig_revision_transport import build_provider_part_callback, prepare_text_effects
from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
from management.services.ig_turn_revisions import (
    claim_revision_preparation, claim_sealed_revision, create_collecting_revision, seal_revision,
)
from product_catalog.models import VariantSizeRule
from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import Catalog, Product, ProductFitOption, SizeGrid


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
NAMESPACE = "instagram_login:owner-1"


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class CatalogNativeDeliveryTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.serial = 0
        temporary = TemporaryDirectory(prefix="twc-native-catalog-")
        self.addCleanup(temporary.cleanup)
        media = override_settings(MEDIA_ROOT=temporary.name, MEDIA_URL="/media/")
        media.enable()
        self.addCleanup(media.disable)
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        for target in ("management.services.instagram_bot._provider_http",
            "management.services.call_ai_analysis.gemini_generate_text"):
            guard = patch(target, side_effect=AssertionError("unmocked provider I/O"))
            guard.start()
            self.addCleanup(guard.stop)
        delivery_fixtures.RevisionDeliveryTests.setUp(self)
        self.settings.ig_user_id = "owner-1"
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ig_user_id", "ai_enabled"])
        catalog_fixtures.SourceCartCatalogTests.setUp(self)
        for index in (2, 3):
            catalog = Catalog.objects.create(name=f"Native catalog {index}", slug=f"native-catalog-{index}")
            grid = SizeGrid.objects.create(catalog=catalog, name=f"Native grid {index}", guide_data={
                "columns": [{"key": "size", "label": "Size"}],
                "rows": [{"size": size, "display_size": size} for size in ("M", "L")],
            })
            product = Product.objects.create(title=f"Native model {index}", slug=f"native-model-{index}",
                category=self.category, price=900 + index, status="published", catalog=catalog, size_grid=grid)
            ProductFitOption.objects.create(product=product, code="classic", label="Classic", is_active=True, is_default=True)
            color = Color.objects.create(name=f"Native color {index}", primary_hex="#333333")
            variant = ProductColorVariant.objects.create(product=product, color=color, stock=20, is_default=True)
            self.products.append(product)
            self.variants.append(variant)
        for index, (product, variant) in enumerate(zip(self.products, self.variants)):
            product.main_image.save(f"native-product-{index}.png", ContentFile(PNG), save=True)
            product.size_grid.image.save(f"native-grid-{index}.png", ContentFile(PNG), save=True)
            image = ProductColorImage(variant=variant)
            image.image.save(f"native-variant-{index}.png", ContentFile(PNG), save=True)
            variant.stock = 20
            variant.save(update_fields=["stock"])
            for size in ("M", "L"):
                VariantSizeRule.objects.create(variant=variant, fit_code="classic", size=size,
                    is_enabled=True, stock=20)

    def response(self, products=(), text="Оберіть товар, який вам подобається."):
        controls = (ResponseControl("show_products", ",".join(str(product.pk) for product in products)),) if products else ()
        return ValidatedResponse(reply_text=text, controls=controls)

    def source_row(self, *, payload="", text="Обрати"):
        self.serial += 1
        return InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="user", source="webhook", status="pending", text=text,
            mid=f"native-user-{self.client_row.pk}-{self.serial}", provider_message_id=f"native-user-{self.client_row.pk}-{self.serial}",
            provider_namespace=NAMESPACE, quick_reply_payload=payload, provider_created_at=timezone.now())

    def seal(self, source):
        self.client_row.refresh_from_db()
        now = timezone.now()
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=source,
            window_started_at=now, window_deadline=now)
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        created = create_collecting_revision(turn, [source], now=now, bypass_quiet=True)
        self.assertIsNotNone(created.revision, created.reason)
        claimed = claim_revision_preparation(created.revision.pk, now=now)
        sealed = seal_revision(created.revision.pk, claimed.token, now=now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(sealed.revision.pk, now=now)
        self.revision, self.revision_token, self.source = claimed.revision, claimed.token, source
        self.revision.client = self.client_row
        return self.revision

    def prepare(self, products=None):
        self.client_row.refresh_from_db()
        self.revision.client = self.client_row
        response = self.response(products or self.products[:3])
        prepared = presentations.prepare_catalog_choice_presentations(self.revision, response, self.settings)
        self.assertTrue(prepared.ready, prepared.reasons)
        return prepared, response

    def plan(self, prepared, response):
        text = prepare_text_effects(self.client_row.igsid, response.reply_text, provider_namespace=NAMESPACE)
        effects = (*prepared.effects, *text.effects)
        control, reasons = prepare_catalog_asset_control(self.client_row, effects, control=response.control)
        self.assertFalse(reasons, reasons)
        authority = build_revision_authority_bindings(self.client_row, claims=(CLAIM_CATALOG_ASSET_REFERENCES,),
            control=control, settings_obj=self.settings)
        self.assertTrue(authority.ready, authority.reasons)
        self.assertTrue(all(len(_canonical(binding)) <= 4096 for binding in authority.fact_bindings))
        self.assertNotIn("https://", json.dumps(authority.fact_bindings))
        planned = plan_revision_effects(self.revision.pk, self.revision_token, source_message_id=self.source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest=authority.authority_digest, effects=effects,
            fact_bindings=authority.fact_bindings, fact_checker=check_fact_bindings)
        self.assertTrue(planned.created, planned.reasons)
        return authority

    def deliver(self, *, native_status=200, missing_mid=False):
        sent = []
        def http(_settings, _url, *, token, data):
            payload = json.loads(data)
            sent.append(payload)
            if len(sent) == 1 and native_status != 200:
                return native_status, json.dumps({"error": {"message": "Invalid template parameter", "code": 100}})
            if len(sent) == 1 and missing_mid:
                return 200, "{}"
            return 200, json.dumps({"message_id": f"native-mid-{self.revision.pk}-{len(sent)}"})
        transport = build_provider_part_callback(self.settings, expected_namespace=NAMESPACE,
            expected_recipient=self.client_row.igsid, access_token="fixture-memory-token")
        with patch("management.services.instagram_bot._provider_http", side_effect=http), patch(
            "management.services.instagram_bot._register_outgoing_message"):
            outcomes = {group: drain_group(self.revision.pk, self.revision_token, group, transport,
                fact_checker=check_fact_bindings) for group in ("template", "template_fallback", "substantive_text")}
        if self.revision.delivery_effects.filter(state=IgRevisionDeliveryEffect.State.SENT).exists():
            project_sent_reply(self.revision.pk)
        native_complete = outcomes["template"].state == "sent" or (
            outcomes["template"].state == "definite_failed" and outcomes["template_fallback"].state == "sent")
        if native_complete and outcomes["substantive_text"].state == "sent":
            finalized = finalize_sent_revision_effects(self.revision.pk, execution_token=self.revision_token)
            self.assertTrue(finalized.completed, finalized.reason)
        return sent, outcomes

    def action_source(self, prepared, *, kind="product", product_id=None, value=None):
        action_index = next(index for index, action in enumerate(prepared.manifest["actions"])
            if action["kind"] == kind and (product_id is None or action.get("product_id") == product_id)
            and (value is None or action.get("value") == value))
        native = prepared.effects[0]["payload"]["message"]
        payloads = []
        attached = (native.get("attachment") or {}).get("payload") or {}
        if attached.get("template_type") == "generic":
            payloads.extend(button["payload"] for card in attached["elements"] for button in card["buttons"])
        elif attached.get("template_type") == "button":
            payloads.extend(button["payload"] for button in attached["buttons"])
        payloads.extend(quick["payload"] for quick in native.get("quick_replies") or [])
        return self.source_row(payload=payloads[action_index], text=prepared.manifest["actions"][action_index]["label"])

    def consume(self, source):
        self.client_row.refresh_from_db()
        delivered = presentations.delivered_catalog_choice(self.client_row, source)
        self.assertTrue(delivered["ready"], delivered)
        with transaction.atomic():
            result = consume_catalog_choice(self.client_row, source)
        self.assertTrue(result["ready"], result)
        self.client_row.refresh_from_db()
        return delivered

    def test_sessionless_three_cards_actual_receipts_then_three_distinct_picks(self):
        self.assertFalse(IgCommerceSelectionSession.objects.filter(client=self.client_row).exists())
        prepared, response = self.prepare()
        self.assertIsNone(prepared.manifest["scope"]["session_id"])
        self.assertFalse(IgCommerceSelectionSession.objects.filter(client=self.client_row).exists())
        self.plan(prepared, response)
        sent, outcomes = self.deliver()
        self.assertEqual(outcomes["template"].state, "sent")
        self.assertEqual(len(sent[0]["message"]["attachment"]["payload"]["elements"]), 3)
        self.assertEqual(len(sent), 2)
        for product in self.products[:3]:
            self.consume(self.action_source(prepared, product_id=product.pk))
        cart = capture_current_selection_lines(self.client_row.pk)
        self.assertEqual(cart["status"], "captured", cart)
        self.assertTrue(cart["coverage_complete"], cart)
        self.assertEqual([line["fields"]["product_id"]["value"] for line in cart["lines"]],
            [product.pk for product in self.products[:3]])
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.client_row, role="model",
            text__startswith="(картки товарів:").count(), 1)

    def test_definite_four_xx_fallback_sends_real_photos_before_signed_choices(self):
        prepared, response = self.prepare()
        self.plan(prepared, response)
        sent, outcomes = self.deliver(native_status=400)
        self.assertEqual(outcomes["template"].state, "definite_failed")
        self.assertEqual(outcomes["template_fallback"].state, "sent")
        self.assertEqual([payload["message"]["attachment"]["payload"]["url"] for payload in sent[1:4]],
            [card["image_url"] for card in prepared.manifest["cards"]])
        self.assertIn("quick_replies", sent[4]["message"])
        source = self.action_source(prepared, product_id=self.products[1].pk)
        delivered = self.consume(source)
        fallback = IgRevisionDeliveryEffect.objects.get(pk=delivered["effect_id"])
        self.assertEqual(fallback.group, "template_fallback")
        projected = InstagramBotMessage.objects.get(provider_message_id=fallback.provider_message_id, role="model")
        self.assertEqual(projected.attachments, "")
        self.assertIn("Оберіть товар", projected.text)
        photos = list(InstagramBotMessage.objects.filter(client=self.client_row, role="model",
            text__startswith="(фото товару:").order_by("pk"))
        self.assertEqual([json.loads(row.attachments)[0] for row in photos],
            [card["image_url"] for card in prepared.manifest["cards"]])

    def test_unknown_and_two_xx_without_mid_never_dispatch_conditional_fallback(self):
        for index, (status, missing) in enumerate(((503, False), (200, True))):
            with self.subTest(status=status):
                if index:
                    self.client_row = IgClient.objects.create(igsid=f"native-unknown-client-{index}", reply_permission_epoch=3)
                    self.seal(self.source_row(text="Покажіть футболки"))
                prepared, response = self.prepare()
                self.plan(prepared, response)
                sent, outcomes = self.deliver(native_status=status, missing_mid=missing)
                self.assertEqual(outcomes["template"].state, "unknown")
                self.assertNotEqual(outcomes["template_fallback"].state, "sent")
                self.assertFalse(any(payload["message"].get("quick_replies") for payload in sent))
                source = self.action_source(prepared)
                self.assertFalse(presentations.delivered_catalog_choice(self.client_row, source)["ready"])

    def test_configured_color_size_real_sources_feed_exact_variant_cart_and_gemini_plan(self):
        duplicate_color = Color.objects.create(name=self.variants[0].color.name, primary_hex="#444445")
        duplicate = ProductColorVariant.objects.create(product=self.products[0], color=duplicate_color,
            slug="native-same-label", stock=20, price_override=990)
        duplicate_image = ProductColorImage(variant=duplicate)
        duplicate_image.image.save("native-same-label.png", ContentFile(PNG), save=True)
        for value in ("M", "L"):
            VariantSizeRule.objects.create(variant=duplicate, fit_code="classic", size=value,
                is_enabled=True, stock=20)
        prepared, response = self.prepare([self.products[0]])
        self.plan(prepared, response)
        self.deliver()
        self.consume(self.action_source(prepared))
        fit = self.source_row(text="Крій classic")
        request = resolve_source_product_request(self.client_row, fit, parse_turn(fit.text))
        decision = apply_turn(self.client_row, fit, request, reply_payload={})
        self.assertTrue(decision.accepted, decision.result_payload)
        self.seal(fit)
        response = self.response(text="Оберіть колір.")
        colors = presentations.prepare_catalog_configuration_choices(self.revision, response, self.settings, kind="color")
        self.assertTrue(colors.ready, colors.reasons)
        self.plan(colors, response)
        self.deliver()
        selected_color = self.action_source(colors, kind="color", value=self.variants[0].pk)
        self.consume(selected_color)
        self.seal(selected_color)
        response = self.response(text="Оберіть розмір.")
        sizes = presentations.prepare_catalog_configuration_choices(self.revision, response, self.settings,
            kind="size")
        self.assertTrue(sizes.ready, sizes.reasons)
        self.assertEqual(sizes.manifest["cards"][0]["configuration_selector"]["variant_id"], self.variants[0].pk)
        self.assertEqual(sizes.manifest["cards"][0]["configuration_selector"]["fit_code"], "classic")
        self.plan(sizes, response)
        self.deliver()
        selected_size = self.action_source(sizes, kind="size", value="L")
        self.consume(selected_size)
        cart = capture_current_selection_lines(self.client_row.pk)
        self.assertTrue(cart["coverage_complete"], cart)
        catalog = resolve_source_cart_catalog(cart)
        self.assertTrue(catalog.complete, catalog.gaps)
        line_id = cart["lines"][0]["line_id"]
        self.assertEqual(catalog.item_specs_by_line[line_id]["color_variant_id"], self.variants[0].pk)
        self.assertEqual(catalog.item_specs_by_line[line_id]["size"], "L")
        self.seal(selected_size)
        plan = capture_response_plan(self.client_row, revision=self.revision, source_cart_capture=cart)
        self.assertFalse(plan.plan_gap, plan.plan_gap)
        projection = plan.prompt_projection()
        self.assertEqual(projection["lines"][0]["product_id"], self.products[0].pk)
        self.assertIn(str(selected_color.pk), plan.prompt_guidance())
        self.assertIn(str(selected_size.pk), plan.prompt_guidance())
        self.assertIn("[SERVER RESPONSE PLAN]", plan.prompt_guidance())

    def test_asset_or_configuration_drift_blocks_real_provider_start(self):
        prepared, response = self.prepare()
        authority = self.plan(prepared, response)
        original = self.products[0].main_image.name
        Product.objects.filter(pk=self.products[0].pk).update(main_image="products/unowned-native.png")
        sent, outcomes = self.deliver()
        self.assertEqual(sent, [])
        self.assertEqual(outcomes["template"].reason, "fact_binding_unavailable")
        self.assertFalse(self.revision.delivery_effects.filter(provider_started_at__isnull=False).exists())
        Product.objects.filter(pk=self.products[0].pk).update(main_image=original)
        rule = VariantSizeRule.objects.get(variant=self.variants[0], fit_code="classic", size="M")
        rule.is_enabled = False
        rule.save(update_fields=["is_enabled"])
        self.assertFalse(check_fact_bindings(authority.fact_bindings, revision=self.revision,
            client=self.client_row, settings_obj=self.settings))

    def test_real_chart_image_precedes_text_and_is_never_projected_as_product_photo(self):
        prepared, response = self.prepare([self.products[0]])
        self.plan(prepared, response)
        self.deliver()
        source = self.action_source(prepared, kind="chart")
        choice = self.consume(source)
        self.seal(source)
        response = self.response(text="Ось таблиця розмірів для цього товару.")
        capture = capture_configured_choices(self.products[0].pk)
        chart = presentations.prepare_catalog_chart_effect(self.revision, response, self.settings,
            choice=choice, capture=capture)
        self.assertTrue(chart.ready, chart.reasons)
        self.plan(chart, response)
        sent, outcomes = self.deliver()
        diagnostics = [(row.group, row.kind, row.state, row.failure_code) for row in self.revision.delivery_effects.order_by("order_index")]
        self.assertEqual(outcomes["template"].state, "sent", (outcomes, diagnostics))
        self.assertEqual(sent[0]["message"]["attachment"]["type"], "image")
        self.assertIn("/size_grids/", sent[0]["message"]["attachment"]["payload"]["url"])
        self.assertEqual(sent[1]["message"]["text"], response.reply_text)
        effect = self.revision.delivery_effects.get(group="template")
        message = InstagramBotMessage.objects.get(provider_message_id=effect.provider_message_id, role="model")
        self.assertTrue(message.text.startswith("(таблиця розмірів:"))
        self.assertNotIn("catalog_media", effect.projection_metadata)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.client_row,
            role="model", text__startswith="(фото товару:").count(), 0)

    def test_more_uses_actual_delivered_order_and_remainder_without_model_rerank(self):
        ordered = [self.products[index] for index in (2, 0, 3, 1)]
        prepared, response = self.prepare(ordered)
        self.plan(prepared, response)
        self.deliver()
        source = self.action_source(prepared, kind="page")
        choice = self.consume(source)
        self.seal(source)
        response = self.response(list(reversed(ordered)))
        page = presentations.prepare_catalog_choice_page(self.revision, response, self.settings, choice=choice)
        self.assertTrue(page.ready, page.reasons)
        self.assertEqual(page.manifest["scope"]["ordered_product_ids"], [product.pk for product in ordered])
        self.assertEqual([card["product_id"] for card in page.manifest["cards"]], [ordered[-1].pk])
        self.assertFalse(any(action["kind"] == "page" for action in page.manifest["actions"]))
        self.plan(page, response)
        self.deliver()
        self.consume(self.action_source(page))

    def test_existing_open_session_does_not_renew_old_menu_after_unrelated_cart_turn(self):
        episode = IgCommercialEpisode.objects.create(client=self.client_row, sequence=1,
            materialization_key="native-existing-empty-session")
        self.client_row.current_commercial_episode = episode
        self.client_row.save(update_fields=["current_commercial_episode"])
        session = IgCommerceSelectionSession.objects.create(client=self.client_row,
            commercial_episode=episode, generation=1, lines=[])
        prepared, response = self.prepare()
        self.assertEqual(prepared.manifest["scope"]["session_id"], session.pk)
        self.plan(prepared, response)
        self.deliver()
        other = self.source_row(text="Додайте Storm")
        request = resolve_source_product_request(self.client_row, other, parse_turn(other.text))
        decision = apply_turn(self.client_row, other, request, reply_payload={})
        self.assertTrue(decision.accepted, decision.result_payload)
        self.client_row.refresh_from_db()
        tap = self.action_source(prepared)
        delivered = presentations.delivered_catalog_choice(self.client_row, tap)
        self.assertFalse(delivered["ready"], delivered)
        with transaction.atomic():
            refused = consume_catalog_choice(self.client_row, tap)
        self.assertFalse(refused["ready"], refused)
