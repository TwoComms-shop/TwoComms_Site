"""Real delivery receipts prevent fit examples from reviving an old SKU."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_commerce_source_facts as source_fixtures
from management.models import IgCustomerTurn, IgTurnMessage, IgRevisionDeliveryEffect
from management.services.ig_catalog_media import prepare_catalog_media, select_catalog_media
from management.services.ig_commerce_source_identity import resolve_source_product_request
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_fit_consultation import capture_fit_consultation
from management.services.ig_product_focus import capture_product_focus
from management.services.ig_product_inquiry_presentation import prepare_fit_illustration
from management.services.ig_response_control import ValidatedResponse
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_ASSET_REFERENCES, build_revision_authority_bindings, check_fact_bindings,
)
from management.services.ig_revision_catalog_assets import prepare_catalog_asset_control
from management.services.ig_revision_commerce import reduce_revision_commerce
from management.services.ig_revision_delivery import ProviderPartResult, drain_group
from management.services.ig_revision_live import _generate_proposal
from management.services.ig_revision_media import collect_revision_media
from management.services.ig_revision_outbox import PublicationBinding, plan_revision_effects
from management.services.ig_revision_reply_projection import project_sent_reply
from management.services.ig_revision_transport import prepare_text_effects
from management.services.ig_turn_revisions import create_collecting_revision
from productcolors.models import Color, ProductColorImage, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption


@override_settings(GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class FitIllustrationSourceIdentityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        source_fixtures.SourcePresentationTests.setUp(self)
        self.addCleanup(self.fixture.doCleanups)
        directory = TemporaryDirectory(prefix="fit-choice-boundary-")
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name, MEDIA_URL="/media/")
        media.enable()
        self.addCleanup(media.disable)
        self.media_root = Path(directory.name)
        self.f = self.fixture
        self.namespace = self.f.source.provider_namespace
        self.binding = PublicationBinding(self.f.publication.pk, self.f.publication.version,
            self.f.publication.snapshot_hash)
        self.category = Category.objects.create(name="Футболки", slug="tshirts")
        self.old = Product.objects.create(pk=99, title="Prior Print", slug="prior-print",
            category=self.category, price=900, status="published", main_image="products/prior.jpg")
        self._file("products/prior.jpg")
        gallery = prepare_catalog_media(self.f.settings, self.f.customer.igsid,
            select_catalog_media([self.old.pk]))
        self.assertFalse(gallery.error)
        effects = tuple({"group": "catalog_media", "kind": "image", "payload": payload,
            "projection_metadata": gallery.product_refs[index]}
            for index, payload in enumerate(gallery.payloads))
        self._deliver(effects)
        self.old_receipt = self.f.revision.delivery_effects.get(group="catalog_media")
        self.assertEqual(self.old_receipt.state, "sent")
        self.assertTrue(self.old_receipt.provider_message_id)
        self._identify_focused_print()

    def _file(self, name):
        target = self.media_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"local public image fixture")

    def _deliver(self, effects, *, unknown=False):
        f = self.f
        text = prepare_text_effects(f.customer.igsid, "Ось фото для поточного питання.",
            provider_namespace=self.namespace)
        effects = (*effects, *text.effects)
        control, reasons = prepare_catalog_asset_control(f.customer, effects)
        self.assertFalse(reasons, reasons)
        authority = build_revision_authority_bindings(f.customer,
            claims=(CLAIM_CATALOG_ASSET_REFERENCES,), control=control, settings_obj=f.settings)
        self.assertTrue(authority.ready, authority.reasons)
        planned = plan_revision_effects(f.revision.pk, f.token, source_message_id=f.source.pk,
            settings_id=f.settings.pk, settings_permission_epoch=f.settings.reply_permission_epoch,
            publication=self.binding, authority_context_digest=authority.authority_digest,
            effects=effects, fact_bindings=authority.fact_bindings, fact_checker=check_fact_bindings)
        self.assertTrue(planned.created, planned.reasons)
        groups = list(dict.fromkeys(row["group"] for row in effects))
        self.serial = getattr(self, "serial", 0)

        def transport(_payload):
            self.serial += 1
            return ProviderPartResult(self.namespace, outcome="timeout") if unknown else ProviderPartResult(
                self.namespace, http_status=200, provider_message_id=f"local-receipt-{self.serial}")

        for group in groups:
            result = drain_group(f.revision.pk, f.token, group, transport, fact_checker=check_fact_bindings)
            if unknown:
                self.assertEqual(result.state, "unknown", result.reason)
                break
            self.assertIn(result.state, {"sent", "cancelled", "empty"}, result.reason)
        if not unknown:
            project_sent_reply(f.revision.pk)
        f.revision.refresh_from_db()
        f.customer.refresh_from_db()

    def _new_revision(self, messages):
        f = self.f
        turn = IgCustomerTurn.objects.create(client=f.customer, primary_source_message=messages[0],
            window_started_at=timezone.now(), window_deadline=timezone.now())
        for index, message in enumerate(messages):
            IgTurnMessage.objects.create(turn=turn, message=message, ordinal=index + 1, role="user")
        f.source = messages[0]
        f.revision = create_collecting_revision(turn, messages, bypass_quiet=True).revision
        f._prepare()
        return f.revision

    def _identify_focused_print(self):
        f = self.f
        self.focused = Product.objects.create(pk=112, title="Футболка «Правил немає»",
            slug="fit-focus-no-rules", category=self.category, price=900, status="published")
        color = Color.objects.create(name="Білий", primary_hex="#ffffff")
        variant = ProductColorVariant.objects.create(product=self.focused, color=color, stock=10,
            price_override=900, is_default=True)
        for fit in ("classic", "oversize"):
            ProductFitOption.objects.create(product=self.focused, code=fit, label=fit,
                is_default=fit == "classic", is_active=True)
        ProductColorImage.objects.create(variant=variant, image="products/focus-oversize.jpg",
            alt_text="Біла oversize-футболка «Правил немає»")
        self._file("products/focus-oversize.jpg")
        self.example = Product.objects.create(title="Classic example", slug="classic-example",
            category=self.category, price=800, status="published")
        example_variant = ProductColorVariant.objects.create(product=self.example, color=color, stock=0)
        ProductColorImage.objects.create(variant=example_variant, image="products/example-classic.jpg",
            alt_text="Класична футболка з іншим принтом")
        self._file("products/example-classic.jpg")
        text = f._message("Цікавить ця футболка", "focus-inquiry-text")
        photo = f._message("", "focus-inquiry-photo")
        body = b"owned inquiry photo"
        photo.private_media_state = "active"
        photo.attachment_media = [{"source_part_id": "mp1_" + "7" * 32, "original_index": 0,
            "identity_origin": "ingress", "type": "image", "status": "owned", "mime": "image/jpeg",
            "bytes": len(body), "content_hash": hashlib.sha256(body).hexdigest(), "private_storage": True,
            "storage_name": f"ig-private/choice-focus-{photo.pk}"}]
        photo.save(update_fields=["private_media_state", "attachment_media"])
        self._new_revision([text, photo])
        reduced = reduce_revision_commerce(f.revision.pk, f.token, settings_id=f.settings.pk,
            settings_permission_epoch=f.settings.reply_permission_epoch, publication=self.binding)
        self.assertTrue(reduced.ready, reduced.reason)
        f.revision.refresh_from_db()
        f.customer.refresh_from_db()
        f.actual_inline_count = 1
        f.parsed = {"reply_text": "Це футболка «Правил немає». Який розмір вам потрібен?",
            "controls": [{"kind": "product", "value": str(self.focused.pk)}],
            "turn_intelligence": {"catalog_candidates": [{"product_id": self.focused.pk,
                "confidence": 1.0, "evidence": "Принт на власному фото"}], "intent": "visual_question",
                "confidence": 1.0, "audio_status": "not_applicable", "transcript": "",
                "image_observations": [{"source_image_index": 0, "outcome": "understood",
                    "evidence_code": "visual_content", "type_code": "product"}]}}
        with patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", body)), patch(
            "management.services.call_ai_analysis.gemini_generate_text", side_effect=f._generate), patch(
            "management.services.instagram_bot._provider_http", side_effect=AssertionError("external transport")):
            collection = collect_revision_media(f.revision.pk, f.token)
            generated, _boundary, reasons = _generate_proposal(f.revision, f.token, f.settings,
                self.binding, collection)
        self.assertIsNotNone(generated, reasons)
        f.revision.refresh_from_db()
        self.assertTrue(f.revision.generation_proposal_digest)
        source = f._message("Покажіть classic та oversize крій", "fit-illustration-request")
        self._new_revision([source])
        context = capture_fit_consultation(f.revision)
        self.assertEqual(capture_product_focus(f.revision)["product_id"], 112)
        illustrated = prepare_fit_illustration(f.revision, ValidatedResponse(reply_text="Порівняння посадки."),
            f.settings, context=context)
        self.assertTrue(illustrated.ready, illustrated.reasons)
        roles = {row["role"] for row in illustrated.effects[0]["projection_metadata"]["fit_illustration"]["items"]}
        self.assertEqual(roles, {"focus", "example"})
        self.illustrations = illustrated.effects

    def _resolve(self, text="Беру цю", *, reply_to=""):
        source = self.f._message(text, f"choice-source-{getattr(self, 'serial', 0)}-{reply_to}")
        source.reply_to_provider_message_id = reply_to
        source.save(update_fields=["reply_to_provider_message_id"])
        self.f.customer.refresh_from_db()
        return source, resolve_source_product_request(self.f.customer, source, parse_turn(text))

    def test_sent_mixed_fit_illustration_blocks_old_catalog_and_reducer_product_change(self):
        self._deliver(self.illustrations)
        source, request = self._resolve()
        self.assertIsNone(request.exact_product_id)
        self.assertEqual(request.pending_clarification, "which_product")
        self.assertNotIn(self.old.pk, request.source_binding.get("candidate_product_ids", ()))
        decision = apply_turn(self.f.customer, source, request, reply_payload={})
        self.assertIsNotNone(decision)
        self.f.customer.refresh_from_db()
        self.assertIsNone(self.f.customer.current_product_id)

    def test_unknown_newer_illustration_does_not_revive_an_older_choice(self):
        self._deliver(self.illustrations, unknown=True)
        self.assertTrue(IgRevisionDeliveryEffect.objects.filter(revision=self.f.revision,
            projection_metadata__has_key="fit_illustration", state="unknown").exists())
        _source, request = self._resolve()
        self.assertIsNone(request.exact_product_id)
        self.assertEqual(request.pending_clarification, "which_product")

    def test_explicit_old_catalog_reply_and_named_new_print_keep_exact_identity(self):
        self._deliver(self.illustrations)
        _source, request = self._resolve(reply_to=self.old_receipt.provider_message_id)
        self.assertEqual(request.exact_product_id, self.old.pk)
        self.assertEqual(request.source_binding["product_resolution"], "exact_presentation")
        _source, named = self._resolve("Беру Prior Print")
        self.assertEqual(named.exact_product_id, self.old.pk)
        self.assertEqual(named.source_binding["product_resolution"], "exact_name")
