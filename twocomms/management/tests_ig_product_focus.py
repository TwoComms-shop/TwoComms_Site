"""Won image identity survives failed delivery without reviving its controls."""
from copy import deepcopy
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management import tests_ig_revision_live as live_fixture
from management.models import IgCustomerTurn, IgTurnMessage, IgCustomerTurnRevision, InstagramBotMessage, GeminiRequest, IgFunnelResetAudit
from management.services.ig_product_focus import capture_product_focus, product_focus_guidance, focus_selection_candidate, capture_discussed_configuration
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class ProductFocusTests(TransactionTestCase):
    reset_sequences = True
    _message = live_fixture.RevisionLiveTests._message
    _prepare = live_fixture.RevisionLiveTests._prepare
    _generate = live_fixture.RevisionLiveTests._generate
    _execute = live_fixture.RevisionLiveTests._execute

    def setUp(self):
        live_fixture.RevisionLiveTests.setUp(self)
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_revision_media import collect_revision_media
        from management.services.ig_revision_live import _generate_proposal

        category = Category.objects.create(name="Футболки", slug="tshirts")
        self.product = Product.objects.create(title="Футболка «Правил немає»", slug="focus-no-rules",
            category=category, price=900, status="published")
        color = Color.objects.create(name="Білий", primary_hex="#ffffff")
        ProductColorVariant.objects.create(product=self.product, color=color, price_override=900, stock=10, is_default=True)
        self.source.text = "Вітаю! Цікавить ця футболка"
        self.source.save(update_fields=["text"])
        self.photo = self._message("", "focus-owned-photo")
        body = b"owned-focus-image"
        self.photo.private_media_state = "active"
        self.photo.attachment_media = [{"source_part_id": "mp1_" + "7" * 32, "original_index": 0,
            "identity_origin": "ingress", "type": "image", "status": "owned", "mime": "image/jpeg",
            "bytes": len(body), "content_hash": hashlib.sha256(body).hexdigest(), "private_storage": True,
            "storage_name": f"ig-private/focus-{self.photo.pk}"}]
        self.photo.save(update_fields=["private_media_state", "attachment_media"])
        IgTurnMessage.objects.create(turn=self.turn, message=self.photo, ordinal=2, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source, self.photo], bypass_quiet=True).revision
        self._prepare()
        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        reduced = reduce_revision_commerce(self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=publication)
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        self.revision.refresh_from_db()
        self.actual_inline_count = 1
        self.parsed = {"reply_text": "Це футболка «Правил немає». Який фасон вам потрібний?",
            "controls": [{"kind": "product", "value": str(self.product.pk)}],
            "turn_intelligence": {"catalog_candidates": [{"product_id": self.product.pk, "confidence": 1.0,
                "evidence": "Принт на власному фото клієнта"}], "intent": "visual_question", "confidence": 1.0,
                "audio_status": "not_applicable", "transcript": "", "image_observations": [
                    {"source_image_index": 0, "outcome": "understood", "evidence_code": "visual_content", "type_code": "product"}]}}
        with patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", body)), patch(
                "management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate), patch(
                "management.services.instagram_bot._provider_http", side_effect=AssertionError("unexpected transport")):
            collection = collect_revision_media(self.revision.pk, self.token)
            response, boundary, reasons = _generate_proposal(self.revision, self.token, self.settings, publication, collection)
        self.assertIsNotNone(response, reasons)
        self.revision.refresh_from_db()
        self.original = self.revision
        self.assertTrue(self.original.generation_proposal_digest)
        self.assertFalse(self.original.delivery_effects.exists())
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)

    def followup(self, text="А які є фасони?"):
        source = self._message(text, f"focus-followup-{InstagramBotMessage.objects.count()}")
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        self.revision = create_collecting_revision(turn, [source], bypass_quiet=True).revision
        self._prepare()
        return self.revision

    def configure_fit_catalog(self):
        from storefront.models import Catalog, SizeGrid, ProductFitOption

        catalog = Catalog.objects.create(name="Configured focus shirts", slug="configured-focus-shirts")
        grid = SizeGrid.objects.create(catalog=catalog, name="Focus sizes", guide_data={
            "columns": [{"key": "size", "label": "Size"}], "rows": [{"size": "M"}, {"size": "L"}]})
        self.product.catalog, self.product.size_grid = catalog, grid
        self.product.save(update_fields=["catalog", "size_grid"])
        for code in ("classic", "oversize"):
            ProductFitOption.objects.create(product=self.product, code=code, label=code,
                is_active=True, is_default=code == "classic")

    def deliver_human_fit_question(self, *, text=None, context_message_id=None):
        from django.contrib.auth import get_user_model
        from management.models import IgClient
        from management.services.ig_human_reply import create_human_reply_command
        from management.services.ig_human_reply_delivery import claim_next_human_part, record_human_part_started, settle_human_part
        from management.services.ig_human_reply_transport import project_human_part_receipt

        actor, _ = get_user_model().objects.get_or_create(username="focus-manager", defaults={
            "email": "focus@example.test", "is_staff": True, "is_superuser": True})
        sent_at = timezone.now()
        command = create_human_reply_command(self.customer.pk, actor=actor,
            text=text or "Це футболка «Правил немає». Класичний крій — звична посадка, оверсайз — вільніша. Який фасон вам підходить?",
            context_message_id=context_message_id or self.photo.pk, now=sent_at).command
        claimed = claim_next_human_part(command.pk, now=sent_at)
        self.assertTrue(claimed.ready, claimed.reason)
        self.assertTrue(record_human_part_started(claimed.part.pk, claimed.token, now=sent_at).ready)
        settled = settle_human_part(claimed.part.pk, claimed.token,
            provider_namespace="instagram_login:owner-1", http_status=200,
            provider_message_id=f"focus-human-fit-question-{command.pk}", now=sent_at + timedelta(milliseconds=1))
        self.assertTrue(settled.ready, settled.reason)
        projected = project_human_part_receipt(claimed.part.pk)
        self.assertIsNotNone(projected.message, projected.reason)
        IgClient.objects.filter(pk=self.customer.pk).update(manager_takeover=False, bot_paused=False)
        self.customer.refresh_from_db()
        return projected.message

    def test_won_original_image_retains_print_across_fit_photo_and_audience_questions(self):
        for text in ("А які є фасони?", "Та ні, приклади фасонів. Скиньте їх фото", "А жіночі які?",
                "Не хочу оверсайз, краще класичну", "Не інші принти, а приклади фасонів",
                "Не другие принты, а примеры обычного и oversize кроя"):
            with self.subTest(text=text):
                revision = self.followup(text)
                with CaptureQueriesContext(connection) as queries, patch(
                        "management.services.instagram_bot._provider_http") as http:
                    focus = capture_product_focus(revision)
                self.assertEqual(focus["status"], "focused", focus)
                self.assertEqual(focus["product_id"], self.product.pk)
                self.assertEqual(focus["category_slug"], "tshirts")
                self.assertEqual(focus["basis"], "won_source_media_identification")
                self.assertEqual(focus["source_message_ids"], [self.source.pk, self.photo.pk])
                self.assertFalse(any(focus[key] for key in ("selection_authority", "checkout_authority", "stock_authority")))
                self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
                self.assertLessEqual(len(queries), 11)
                http.assert_not_called()
                self.assertIn("Do not ask which design again", product_focus_guidance(focus))
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.original.refresh_from_db()
        self.assertFalse(self.original.delivery_effects.exists())
        self.assertNotIn("client_configuration_update", self.original.action_receipts)

    def test_new_size_recovers_previously_processed_contextual_fit_through_fresh_action(self):
        self.assert_new_size_recovers_contextual_fit()

    def test_new_size_after_chart_and_human_url_keeps_customer_classic_choice(self):
        self.assert_new_size_recovers_contextual_fit(with_chart=True)

    def assert_new_size_recovers_contextual_fit(self, *, with_chart=False):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_response_plan import capture_response_plan

        self.configure_fit_catalog()
        self.deliver_human_fit_question()
        old_answer = self.followup("Звична")
        # Simulate the historical decision made before contextual-fit support.
        with patch("management.services.ig_fit_consultation.contextual_fit_choice", return_value={}):
            old_reduced = reduce_revision_commerce(old_answer.pk, self.token, settings_id=self.settings.pk,
                settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(old_reduced.ready, old_reduced.reason)
        old_source_id = old_answer.sources.get().message_id
        self.deliver_human_fit_question(context_message_id=old_source_id,
            text="Домовились: «Правил немає», класичний крій (regular). Який розмір вам потрібен?")
        if with_chart:
            chart = self.followup("Скиньте розмірну сітку")
            reduced = reduce_revision_commerce(chart.pk, self.token, settings_id=self.settings.pk,
                settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
            self.assertTrue(reduced.ready, reduced.reason)
            chart_source_id = chart.sources.get().message_id
            self.deliver_human_fit_question(context_message_id=chart_source_id,
                text="Розмірна сітка класичного крою: https://twocomms.shop/media/size-grids/classic.png")
        revision = self.followup("M")
        reduced = reduce_revision_commerce(revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        revision.refresh_from_db()
        plan = capture_response_plan(self.customer, revision=revision)
        candidate = plan.configuration["focus_selection_candidate"]
        self.assertEqual(candidate["candidate_controls"], {"product": self.product.pk, "fit": "classic", "size": "M"})
        self.assertEqual(candidate["retained_fit_source"]["source_message_id"], old_source_id)
        if with_chart:
            self.assertEqual(candidate["retained_fit_source"]["intervening_information_sources"][0]["source_message_id"],
                chart_source_id)
        self.assertFalse(candidate["checkout_authority"])
        self.actual_inline_count = 0
        self.parsed = {"reply_text": "Для футболки «Правил немає» ви обрали розмір M. Ви обрали класичну посадку.",
            "controls": [{"kind": "product", "value": str(self.product.pk)},
                {"kind": "fit", "value": "classic"}, {"kind": "size", "value": "M"}]}
        result, generated, sent = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((generated.call_count, sent.call_count), (1, 1))
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.current_product_id,
            self.customer.sales_context["assisted_checkout_selection"]["fit_option_code"], self.customer.current_size),
            (self.product.pk, "classic", "M"))
        old_answer.refresh_from_db()
        self.assertFalse(old_answer.delivery_effects.exists())
        self.assertNotIn("client_configuration_update", old_answer.action_receipts)

    def test_explicit_other_print_releases_focus_but_fit_negation_does_not(self):
        revision = self.followup("Покажіть інший принт")
        self.assertEqual(capture_product_focus(revision)["reason"], "customer_changed_product_focus")

    def test_new_customer_photo_requires_new_identification(self):
        revision = self.followup("Ось інша фотографія")
        source = revision.sources.get().message
        source.private_media_state = "active"
        source.attachment_media = [{**self.photo.attachment_media[0],
            "source_part_id": "mp1_" + "8" * 32, "content_hash": "f" * 64}]
        source.save(update_fields=["attachment_media", "private_media_state"])
        # Seal the actual changed source, rather than testing a stale snapshot.
        self.revision = create_collecting_revision(revision.turn, [source], bypass_quiet=True).revision
        self._prepare()
        self.assertEqual(capture_product_focus(self.revision)["reason"], "new_customer_media_requires_identification")

    def test_reset_does_not_restore_original_print_from_old_winner(self):
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.photo.pk, reason="new conversation")
        self.assertEqual(capture_product_focus(self.followup())["status"], "none")

    def test_changed_source_or_missing_winner_cannot_create_focus(self):
        revision = self.followup()
        self.source.text = "Changed source"
        self.source.save(update_fields=["text"])
        self.assertEqual(capture_product_focus(revision)["status"], "none")
        self.source.text = "Вітаю! Цікавить ця футболка"
        self.source.save(update_fields=["text"])
        graph = GeminiRequest.objects.get(logical_turn_id=f"ig-revision:{self.original.pk}")
        GeminiRequest.objects.filter(pk=graph.pk).update(terminal_resolution="failed")
        self.assertEqual(capture_product_focus(revision)["status"], "none")

    def test_corrupt_immutable_proposal_and_hash_are_not_context(self):
        revision = self.followup()
        proposal = deepcopy(self.original.generation_proposal)
        proposal["turn_intelligence"]["auto_product_id"] = self.product.pk + 1
        # The normal ORM blocks this corruption. Fault-inject at the storage
        # boundary to verify the reader still refuses a damaged durable row.
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {IgCustomerTurnRevision._meta.db_table} SET generation_proposal = %s WHERE id = %s",
                [json.dumps(proposal), self.original.pk])
        self.assertEqual(capture_product_focus(revision)["status"], "none")

    def test_foreign_sender_cannot_supply_historical_focus(self):
        revision = self.followup()
        InstagramBotMessage.objects.filter(pk=self.photo.pk).update(sender_id="foreign-source-owner")
        self.assertEqual(capture_product_focus(revision)["status"], "none")

    def test_unpublished_product_is_not_a_current_focus(self):
        revision = self.followup()
        self.product.status = "draft"
        self.product.save(update_fields=["status"])
        self.assertEqual(capture_product_focus(revision)["status"], "none")

    def test_new_print_inquiry_overrides_older_canonical_purchase_focus(self):
        from storefront.models import Product
        previous = Product.objects.create(title="Попередня модель", slug="previous-focus",
            category=self.product.category, price=800, status="published")
        self.customer.refresh_from_db()
        canonical_cart = {"schema": "source-selections.v1", "status": "captured",
            "scope": {"client_id": self.customer.pk, "episode_id": self.customer.current_commercial_episode_id},
            "active_line_id": "old-purchase", "lines": [{"line_id": "old-purchase", "fields": {
                "product_id": {"status": "confirmed", "value": previous.pk,
                    "source": {"source_message_id": self.source.pk}}}}]}
        focus = capture_product_focus(self.followup(), source_cart_capture=canonical_cart)
        self.assertEqual(focus["product_id"], self.product.pk, focus)
        self.assertEqual(focus["basis"], "won_source_media_identification")

    def test_changed_original_image_content_hash_cannot_reuse_old_identity(self):
        revision = self.followup()
        parts = deepcopy(self.photo.attachment_media)
        parts[0]["content_hash"] = "f" * 64
        InstagramBotMessage.objects.filter(pk=self.photo.pk).update(attachment_media=parts)
        self.assertEqual(capture_product_focus(revision)["status"], "none")

    def test_consistently_rehashed_proposal_still_requires_original_sealed_image(self):
        from management.services.ig_revision_outbox import _digest
        revision = self.followup()
        proposal = deepcopy(self.original.generation_proposal)
        proposal["request_media_manifest"]["items"][0]["content_hash"] = "f" * 64
        proposal["request_media_manifest"]["actual_content_hashes"] = ["f" * 64]
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {IgCustomerTurnRevision._meta.db_table} SET generation_proposal = %s, generation_proposal_digest = %s WHERE id = %s",
                [json.dumps(proposal), _digest(proposal), self.original.pk])
        self.assertEqual(capture_product_focus(revision)["status"], "none")

    def test_legitimate_resume_epoch_change_preserves_discussion_without_send_rights(self):
        from management.models import IgClient
        previous_epoch = self.original.permission_epoch
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=previous_epoch + 10)
        self.customer.refresh_from_db()
        revision = self.followup("А жіночі які?")
        self.assertNotEqual(revision.permission_epoch, self.original.permission_epoch)
        focus = capture_product_focus(revision)
        self.assertEqual(focus["product_id"], self.product.pk, focus)
        self.assertFalse(focus["checkout_authority"])

    def test_fresh_customer_fit_size_selects_original_print_before_purchase_request(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_response_plan import capture_response_plan
        self.configure_fit_catalog()

        revision = self.followup("Оверсайз, M")
        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        reduced = reduce_revision_commerce(revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=publication)
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        revision.refresh_from_db()
        before = capture_response_plan(self.customer, revision=revision)
        focus = capture_product_focus(revision, source_cart_capture=before.source_cart_capture)
        candidate = focus_selection_candidate(revision, focus, source_cart_capture=before.source_cart_capture)
        self.assertEqual(candidate["candidate_controls"], {"product": self.product.pk, "fit": "oversize", "size": "M"})
        self.assertFalse(candidate["checkout_authority"])
        self.actual_inline_count = 0
        self.parsed = {"reply_text": "Для футболки «Правил немає» ви обрали розмір M. Ви обрали посадку оверсайз.",
            "controls": [{"kind": "product", "value": str(self.product.pk)},
                {"kind": "fit", "value": "oversize"}, {"kind": "size", "value": "M"}]}
        result, generated, sent = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((generated.call_count, sent.call_count), (1, 1))
        self.customer.refresh_from_db()
        self.assertEqual((self.customer.current_product_id, self.customer.current_size), (self.product.pk, "M"))
        revision.refresh_from_db()
        receipt = revision.action_receipts["client_configuration_update"]
        self.assertEqual(receipt["source_message_id"], candidate["source_message_id"])
        self.assertNotEqual(receipt["generation_proposal_digest"], self.original.generation_proposal_digest)
        self.assertFalse(self.original.delivery_effects.exists())

        purchase = self.followup("Хочу замовити")
        reduced = reduce_revision_commerce(purchase.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=publication)
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        purchase.refresh_from_db()
        after = capture_response_plan(self.customer, revision=purchase)
        self.assertEqual(after.choices.get("product_id"), self.product.pk)
        self.assertNotIn(after.next_selector, {"product", "model"})

    def test_confirmed_human_fit_question_then_zvychna_selects_original_print_and_asks_size(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_response_plan import capture_response_plan

        self.configure_fit_catalog()
        question = self.deliver_human_fit_question()
        self.assertIsNone(question.provider_created_at)
        revision = self.followup("Звична")
        from management.services.ig_fit_consultation import contextual_fit_choice
        self.assertEqual(contextual_fit_choice(self.customer, revision.sources.get().message).get("fit"), "classic")
        reduced = reduce_revision_commerce(revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        revision.refresh_from_db()
        plan = capture_response_plan(self.customer, revision=revision)
        self.assertIn("focus_selection_candidate", plan.configuration,
            {"cart": plan.source_cart_capture, "focus": plan.product_focus, "question": {
                "status": question.status, "send_state": question.send_state, "source": question.source,
                "provider_message_id": question.provider_message_id}})
        candidate = plan.configuration["focus_selection_candidate"]
        self.assertEqual(candidate["candidate_controls"], {"product": self.product.pk, "fit": "classic"})
        self.assertEqual(plan.next_selector, "size")
        self.actual_inline_count = 0
        self.parsed = {"reply_text": "Ви обрали класичну посадку. Який розмір ви зазвичай носите?",
            "controls": [{"kind": "product", "value": str(self.product.pk)}, {"kind": "fit", "value": "classic"}]}
        result, generated, sent = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual((generated.call_count, sent.call_count), (1, 1))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, self.product.pk)
        self.assertEqual(self.customer.sales_context["assisted_checkout_selection"]["fit_option_code"], "classic")
        self.assertFalse(self.customer.current_size)
        self.original.refresh_from_db()
        self.assertFalse(self.original.delivery_effects.exists())
        self.assertNotIn("client_configuration_update", self.original.action_receipts)

    def test_positive_fit_without_size_nominates_print_and_next_size_only(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_response_plan import capture_response_plan

        revision = self.followup("Оверсайз")
        reduced = reduce_revision_commerce(revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        revision.refresh_from_db()
        plan = capture_response_plan(self.customer, revision=revision)
        focus = capture_product_focus(revision, source_cart_capture=plan.source_cart_capture)
        candidate = focus_selection_candidate(revision, focus, source_cart_capture=plan.source_cart_capture)
        self.assertEqual(candidate["candidate_controls"], {"product": self.product.pk, "fit": "oversize"})
        self.assertEqual(candidate["next_selector_hint"], "size")

    def test_consultation_and_ambiguous_deictic_cannot_nominate_selection(self):
        cart = {"schema": "source-selections.v1", "status": "captured", "scope": {"client_id": self.customer.pk},
            "lines": [{"line_id": "line:0", "fields": {}}]}
        for text in ("А жіночі які?", "Які є фасони?", "Хочу цю, M", "Чи є оверсайз M?", "Хочу худі, M"):
            revision = self.followup(text)
            focus = capture_product_focus(revision)
            self.assertEqual(focus_selection_candidate(revision, focus, source_cart_capture=cart), {}, text)

    def test_audited_clear_prevents_old_sealed_size_from_becoming_a_fresh_candidate(self):
        import uuid
        from django.contrib.auth import get_user_model
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_admin_state_capture import current_admin_state
        from management.services.ig_selection_corrections import save_size_correction

        revision = self.followup("Оверсайз, M")
        reduced = reduce_revision_commerce(revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        revision.refresh_from_db()
        before = capture_response_plan(self.customer, revision=revision)
        self.assertEqual(before.configuration["focus_selection_candidate"]["candidate_controls"]["size"], "M")
        actor = get_user_model().objects.create_superuser(username="focus-correction-manager", email="correction@example.test")
        now = timezone.now()
        context = current_admin_state(self.customer.pk, now=now).state.as_dict()["boundary"]["size_correction_context"]
        self.assertTrue(context["available"], context)
        corrected = save_size_correction(self.customer.pk, actor=actor, operation_id=uuid.uuid4(),
            operation="clear", value=None, expected_selection_revision=context["context"]["selection_revision"],
            expected_context_digest=context["context_digest"], now=now)
        self.assertEqual(corrected.status, "applied")
        after = capture_response_plan(self.customer, revision=revision)
        focus = capture_product_focus(revision, source_cart_capture=after.source_cart_capture)
        self.assertEqual(focus_selection_candidate(revision, focus, source_cart_capture=after.source_cart_capture), {})
        self.assertNotIn("focus_selection_candidate", after.configuration)
        self.assertNotIn("size", after.choices)
        self.assertEqual(revision.bundle_snapshot["sources"][0]["text"], "Оверсайз, M")

    def test_chart_request_recovers_old_customer_fit_for_presentation_without_selecting_product(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_response_plan import capture_response_plan

        self.deliver_human_fit_question()
        old_answer = self.followup("Звична")
        with patch("management.services.ig_fit_consultation.contextual_fit_choice", return_value={}):
            reduced = reduce_revision_commerce(old_answer.pk, self.token, settings_id=self.settings.pk,
                settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        old_source_id = old_answer.sources.get().message_id
        self.deliver_human_fit_question(context_message_id=old_source_id,
            text="Домовились: «Правил немає», класичний крій (regular). Який розмір вам потрібен?")
        revision = self.followup("Скиньте розмірну сітку")
        reduced = reduce_revision_commerce(revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        revision.refresh_from_db()
        plan = capture_response_plan(self.customer, revision=revision)
        self.assertNotIn("focus_selection_candidate", plan.configuration)
        with CaptureQueriesContext(connection) as queries:
            config = capture_discussed_configuration(revision, source_cart_capture=plan.source_cart_capture)
        self.assertEqual((config["product_id"], config["fit_code"]), (self.product.pk, "classic"))
        self.assertEqual(config["fit_source"]["source_message_id"], old_source_id)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        self.assertFalse(any(config[key] for key in ("selection_authority", "stock_authority", "checkout_authority")))
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.assertFalse(old_answer.delivery_effects.exists())

    def test_manager_acknowledgement_alone_cannot_supply_discussed_chart_fit(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_response_plan import capture_response_plan

        self.deliver_human_fit_question(text="Домовились: класичний крій. Який розмір вам потрібен?")
        revision = self.followup("Скиньте розмірну сітку")
        reduced = reduce_revision_commerce(revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        self.customer.refresh_from_db()
        revision.refresh_from_db()
        plan = capture_response_plan(self.customer, revision=revision)
        self.assertEqual(capture_discussed_configuration(revision, source_cart_capture=plan.source_cart_capture), {})

    def test_retained_fit_stops_at_conflicting_or_ambiguous_customer_choice(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce

        self.deliver_human_fit_question()
        old = self.followup("Звична")
        with patch("management.services.ig_fit_consultation.contextual_fit_choice", return_value={}):
            reduced = reduce_revision_commerce(old.pk, self.token, settings_id=self.settings.pk,
                settings_permission_epoch=self.settings.reply_permission_epoch,
                publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(reduced.ready, reduced.reason)
        for text in ("Оверсайз", "Хочу цю"):
            with self.subTest(text=text):
                self.followup(text)
                revision = self.followup("Скиньте розмірну сітку")
                self.customer.refresh_from_db()
                self.assertEqual(capture_discussed_configuration(revision), {})
