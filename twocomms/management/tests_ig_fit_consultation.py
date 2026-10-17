"""Fit examples preserve the discussed print and scoped merchant facts."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from management.models import IgClient, InstagramBotMessage
from management.services.ig_fit_consultation import (
    build_fit_consultation, capture_fit_consultation, deterministic_fit_reply,
    fit_consultation_request, fit_topic_covered, validate_fit_consultation,
)
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import parse_structured_response
from management.services.ig_response_plan import build_response_plan, capture_response_plan


FOCUS = {"schema": "discussed-product-focus.v1", "status": "focused", "product_id": 112,
    "title": "Правил немає", "category_slug": "tshirts", "proof_digest": "a" * 64,
    "source_message_ids": [10], "selection_authority": False, "stock_authority": False, "checkout_authority": False}
PHOTO = {"schema": "fit-photo-capability.v1", "product_id": 112, "title": "Правил немає",
    "supported_fits": ["classic", "oversize"], "photos": [{"asset_id": 85, "fit_code": "oversize"}],
    "missing_fit_photos": ["classic"], "example_product_ids": [], "examples": [], "capability_digest": "b" * 64, "error": ""}


def source(text, pk=20):
    return {"message_id": pk, "role": "user", "text": text}


def response(text, **controls):
    return parse_structured_response({"reply_text": text,
        "controls": [{"kind": key, "value": value} for key, value in controls.items()]})


class FitConsultationContractTests(SimpleTestCase):
    def context(self, text="Покажіть приклади звичайного та oversize крою, жіночі є?", *, focus=FOCUS, photo=PHOTO):
        return build_fit_consultation([source(text)], focus=focus, photo_capability=photo,
            approved_fact_keys=("current_tshirt_bases", "current_tshirt_audience"))

    def test_examples_of_cuts_do_not_request_different_designs(self):
        for text in ("Покажіть приклади звичайного та oversize крою", "Можно фото обычных и оверсайз?",
            "Show regular and oversized examples", "Та ні, не інші принти, а приклади фасонів",
            "Не другие принты, а примеры обычного и oversize кроя"):
            with self.subTest(text=text):
                request = fit_consultation_request([source(text)])
                self.assertTrue(request["photos_requested"])
                self.assertFalse(request["assortment_requested"])

    def test_explicit_other_design_request_is_distinct(self):
        request = fit_consultation_request([source("Покажіть інші принти у classic крої")])
        self.assertTrue(request["assortment_requested"])
        self.assertEqual(validate_fit_consultation(self.context("Покажіть інші принти у classic крої"),
            response("Ось інші принти.", show_products=[99])), "")

    def test_current_fragmented_photo_request_keeps_source_ids_and_cancellation(self):
        request = fit_consultation_request([source("Що таке regular та oversize?", 20), source("Можна фото?", 21)])
        self.assertEqual(request["source_message_ids"], [20, 21])
        self.assertTrue(request["photos_requested"])
        self.assertFalse(fit_consultation_request([source("Regular чи oversize?"), source("Фото не треба", 21)]).get("photos_requested"))

    def test_quoted_fit_and_plain_size_question_do_not_change_purpose(self):
        for text in ('Він написав «покажи classic і oversize».', "Which size will fit me?", "Розмір L підійде?", "оверсайз, M"):
            self.assertEqual(fit_consultation_request([source(text)]), {}, text)

    def test_elliptical_audience_and_fit_questions_do_not_need_question_mark(self):
        for text in ("Є жіночі", "А женские есть", "естьженские", "Які крої є", "Какие обычные", "Який regular крій"):
            self.assertTrue(fit_consultation_request([source(text)]), text)

    def test_negative_photo_directives_win_latest_burst(self):
        for text in ("не надсилайте фото", "не показуйте приклади", "не присылайте фото", "do not send photos",
            "Поясніть regular та oversize, фото не надсилайте"):
            request = fit_consultation_request([source("Покажіть regular та oversize", 20), source(text, 21)])
            self.assertFalse(request["photos_requested"], text)

    def test_chart_request_does_not_request_garment_fit_photos(self):
        for text in ("Покажіть таблицю розмірів classic футболки", "Фото таблицы размеров regular футболки", "Show classic T-shirt size chart"):
            request = fit_consultation_request([source(text)])
            self.assertTrue(request["size_chart_requested"], text)
            self.assertFalse(request["photos_requested"], text)
        mixed = fit_consultation_request([source("Покажіть таблицю розмірів та приклади classic крою")])
        self.assertTrue(mixed["photos_requested"])

    def test_negated_cut_does_not_become_requested_example_or_other_print(self):
        request = fit_consultation_request([source("Покажіть не oversize, а regular крій, не інші принти")])
        self.assertEqual(request["requested_fits"], ["classic"])
        self.assertFalse(request["comparison_requested"])
        self.assertFalse(request["assortment_requested"])

    def test_focused_fit_controls_cannot_switch_product_or_send_unrelated_catalog(self):
        context = self.context("Покажіть приклади regular та oversize крою")
        self.assertEqual(validate_fit_consultation(context, response("Ось інший товар.", product=99)), "focused_fit_product_changed")
        self.assertEqual(validate_fit_consultation(context, response("Ось каталог.", catalog_link=True)), "focused_fit_catalog_redirect")
        self.assertEqual(validate_fit_consultation(context, response("Ось жіночі моделі.", show_products=[99])), "focused_fit_example_unproven")
        self.assertEqual(validate_fit_consultation(context, response("Який принт ви обрали?")), "focused_fit_print_reselection")
        self.assertEqual(validate_fit_consultation(context, response("Фото того самого принта.", show_products=[112])), "")

    def test_other_design_only_with_exact_illustration_capability_and_disclosure(self):
        photo = {**PHOTO, "example_product_ids": [99], "examples": [{"product_id": 99, "fit_code": "classic", "illustration_only": True}]}
        context = self.context("Покажіть приклади regular та oversize крою", photo=photo)
        allowed = response("Це інший принт, лише як приклад classic крою; ваш принт залишається тим самим.", show_products=[99])
        self.assertEqual(validate_fit_consultation(context, allowed), "")
        self.assertEqual(validate_fit_consultation(context, response("Це ваш принт у classic.", show_products=[99])), "focused_fit_example_unproven")
        self.assertEqual(validate_fit_consultation(context, response(allowed.reply_text, show_products=[100])), "focused_fit_example_unproven")
        self.assertEqual(validate_fit_consultation(self.context("Чим classic відрізняється від oversize?", photo=photo), allowed), "focused_fit_example_unproven")

    def test_unisex_fact_scope_does_not_expand_to_hoodies(self):
        tshirt = self.context("У вас є жіночі звичайні футболки?")
        self.assertIn("current_tshirt_audience", tshirt["approved_fact_keys"])
        hoodie = self.context("Є жіночі худі regular?", focus={**FOCUS, "category_slug": "hoodie"})
        self.assertEqual(hoodie["approved_fact_keys"], [])
        self.assertEqual(deterministic_fit_reply(hoodie), "")

    def test_local_fallback_answers_unisex_and_missing_photo_without_new_sale(self):
        context = self.context()
        for language, unisex in (("uk", "унісекс"), ("ru", "унисекс"), ("en", "unisex")):
            reply = deterministic_fit_reply(context, language=language)
            self.assertIn(unisex, reply.lower())
            self.assertIn(FOCUS["title"], reply)
            self.assertIn("classic", reply)
            self.assertTrue(fit_topic_covered("info:audience", response(reply), context))
            self.assertTrue(fit_topic_covered("info:fit", response(reply), context))
            self.assertEqual(validate_fit_consultation(context, response(reply)), "")

    def test_scoped_audience_question_cannot_receive_gender_catalog_denial(self):
        context = self.context("У вас є жіночі футболки regular?")
        self.assertEqual(validate_fit_consultation(context, response("Немає жіночих, оберіть інший принт.")), "focused_fit_audience_unanswered")
        self.assertEqual(validate_fit_consultation(context, response("Це не унісекс.")), "focused_fit_audience_unanswered")
        self.assertEqual(validate_fit_consultation(context, response("Весь каталог одягу — унісекс.")), "focused_fit_audience_scope_exceeded")
        self.assertEqual(validate_fit_consultation(context, response("Жінки та чоловіки носять ту саму футболку.")), "")

    def test_plan_keeps_fit_obligations_and_rejects_foreign_controls_before_coverage(self):
        sources = [source("Чим classic відрізняється від oversize? Покажіть фото крою.")]
        plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(), sources=sources)
        plan = replace(plan, fit_consultation=self.context(sources[0]["text"]), product_focus=FOCUS)
        kinds = {row["kind"] for row in plan.obligations}
        self.assertIn("info:fit", kinds)
        self.assertIn("info:fit_presentation", kinds)
        self.assertNotIn("info:presentation", kinds)
        self.assertEqual(plan.validate(response("Інші футболки.", show_products=[99])), "focused_fit_example_unproven")
        self.assertIn("fit_consultation", plan.prompt_projection())
        self.assertIn("fit_consultation", plan.as_dict())
        self.assertIn("Do not ask them to pick a print again", plan.prompt_guidance())

    def test_capture_reads_focus_and_photo_metadata_once_and_does_not_grant_selection(self):
        revision = SimpleNamespace(bundle_snapshot={"sources": [source("Покажіть classic та oversize, жіночі є?")]})
        with patch("management.services.ig_product_focus.capture_product_focus", return_value=deepcopy(FOCUS)) as focus, patch(
            "management.services.ig_revision_catalog_assets.capture_fit_photo_capability", return_value=deepcopy(PHOTO)
        ) as photos:
            context = capture_fit_consultation(revision, source_cart_capture={"status": "captured"})
        focus.assert_called_once()
        photos.assert_called_once_with(112, metadata_only=True)
        self.assertFalse(context["selection_authority"])
        self.assertFalse(context["stock_authority"])
        self.assertFalse(context["checkout_authority"])

    def test_plan_retains_focus_for_selector_and_order_without_recapturing_it(self):
        for text in ("оверсайз, M", "Хочу замовити", "M", "https://twocomms.shop/product/audited-response-tshirt/ розмір M"):
            with self.subTest(text=text):
                revision = SimpleNamespace(bundle_snapshot={"sources": [source(text)]}, action_receipts={}, snapshot_digest="c" * 64)
                base = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext())
                candidate = {"candidate_controls": {"product": 112, "fit": "oversize", "size": "M"}, "next_selector_hint": "size"}
                with patch("management.services.ig_response_plan._capture_response_plan_inputs", return_value=base), patch(
                    "management.services.ig_product_focus.capture_product_focus", return_value=deepcopy(FOCUS)
                ) as capture, patch("management.services.ig_product_focus.focus_selection_candidate", return_value=candidate):
                    plan = capture_response_plan(SimpleNamespace(), revision=revision)
                capture.assert_called_once()
                self.assertEqual(plan.product_focus["product_id"], 112)
                self.assertEqual(plan.fit_consultation, {})
                self.assertEqual(plan.next_selector, "size")
                self.assertEqual(plan.configuration["focus_selection_candidate"], candidate)
                self.assertEqual(plan.prompt_projection()["product_focus"]["product_id"], 112)

    def test_fallback_asks_one_source_unresolved_selector_not_print(self):
        revision = SimpleNamespace(bundle_snapshot={"sources": [source("Є жіночі футболки?")]})
        for fields, selector in (({}, "fit"), ({"fit_option_code": {"value": "oversize", "status": "confirmed"}}, "size"),
            ({"fit_option_code": {"value": "oversize", "status": "confirmed"}, "size": {"value": "M", "status": "confirmed"}}, "")):
            with patch("management.services.ig_revision_catalog_assets.capture_fit_photo_capability", return_value=deepcopy(PHOTO)):
                context = capture_fit_consultation(revision, product_focus=deepcopy(FOCUS),
                    source_cart_capture={"status": "captured", "lines": [{"fields": fields}]})
            self.assertEqual(context["next_consultation_selector"], selector)
            reply = deterministic_fit_reply(context)
            self.assertEqual(reply.count("?"), int(bool(selector)))
            self.assertNotIn("г/м", reply)

    def test_consultation_preserves_canonical_readiness_and_completed_choices(self):
        revision = SimpleNamespace(bundle_snapshot={"sources": [source("Є жіночі футболки?")]},
            action_receipts={}, snapshot_digest="c" * 64)
        for canonical_selector, expected in (("size", "size"), ("color", "color"), ("", ""), ("product", "fit")):
            with self.subTest(canonical_selector=canonical_selector):
                base = replace(build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext()),
                    next_selector=canonical_selector)
                with patch("management.services.ig_response_plan._capture_response_plan_inputs", return_value=base), patch(
                    "management.services.ig_product_focus.capture_product_focus", return_value=deepcopy(FOCUS)
                ), patch("management.services.ig_product_focus.focus_selection_candidate", return_value={}), patch(
                    "management.services.ig_fit_consultation.capture_fit_consultation",
                    return_value={**self.context("Є жіночі футболки?"), "next_consultation_selector": "fit"}
                ):
                    plan = capture_response_plan(SimpleNamespace(), revision=revision)
                self.assertEqual(plan.next_selector, expected)
                self.assertEqual(plan.fit_consultation["next_consultation_selector"], expected)
                self.assertEqual(plan.choices, base.choices)
                self.assertEqual(plan.authority, base.authority)


class FitConsultationIntentTests(TestCase):
    def test_contextual_colloquial_fit_answer_requires_exact_prior_delivered_selector(self):
        from management.services.ig_fit_consultation import contextual_fit_choice

        client = IgClient.objects.create(igsid="contextual-fit-client")
        namespace = "instagram_login:owner-1"
        answer = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            role="user", source="webhook", text="Звична", mid="contextless-answer", provider_namespace=namespace)
        self.assertEqual(contextual_fit_choice(client, answer), {})
        question = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            role="manager", source="echo", status="done", mid="own-fit-question", provider_namespace=namespace,
            text="Класика — звична посадка, oversize — вільніша. Яка посадка вам ближча — звична чи вільніша?")
        for ordinal, (text, expected) in enumerate((("Звична", "classic"), ("Вільніша", "oversize"))):
            # Each answer belongs to its immediately preceding native question.
            if ordinal:
                question.pk = None
                question.mid = f"own-fit-question-{ordinal}"
                question.save()
            current = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
                role="user", source="webhook", status="pending", text=text,
                mid=f"fit-answer-{ordinal}", provider_namespace=namespace)
            proof = contextual_fit_choice(client, current)
            self.assertEqual(proof["fit"], expected)
            self.assertEqual(proof["question_message_id"], question.pk)
            self.assertEqual(proof["source_message_id"], current.pk)
            self.assertFalse(proof["selection_authority"])
            current.refresh_from_db()
            self.assertEqual(current.text, text)
        intervening = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            role="user", source="webhook", text="Інше питання", mid="intervening", provider_namespace=namespace)
        current = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            role="user", source="webhook", text="Звична", mid="too-late-fit", provider_namespace=namespace)
        self.assertEqual(contextual_fit_choice(client, current), {})

    def test_actual_customer_fit_question_grants_consultation_without_selection_next_step(self):
        from management.services.ig_turn_intent import build_turn_intent, intent_generation_guidance

        client = IgClient.objects.create(igsid="fit-intent-client")
        row = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid,
            role="user", source="webhook", text="А звичайні жіночі футболки є? Чим відрізняється oversize?", mid="fit-intent-source")
        intent = build_turn_intent(client, source_messages=[row])
        self.assertEqual(intent["purpose"], "fit_consultation")
        self.assertEqual(intent["commerce_evidence_refs"], [row.pk])
        self.assertIn("retail_consultation", intent["allowed_response_acts"])
        self.assertNotIn("optional_retail_next_step", intent["allowed_response_acts"])
        self.assertIn("does not choose a different product", intent_generation_guidance(intent))


class NativeChartResponsePlanTests(SimpleTestCase):
    def plan_and_effect(self, text="Скиньте розмірну сітку"):
        from django.utils.crypto import salted_hmac
        from management.services.ig_revision_outbox import _digest

        scope = {"client_id": 7, "client_epoch": 12, "namespace": "instagram:merchant",
            "revision_snapshot_digest": "d" * 64, "source_message_ids": [20]}
        plan = build_response_plan(preferences={}, readiness={}, context=ReplyTruthContext(), sources=[source(text)])
        plan = replace(plan, product_focus=deepcopy(FOCUS), configuration={**plan.configuration,
            "native_chart_scope": {**scope, "revision_id": 90}})
        payload = {"recipient": {"id": "customer"}, "message": {"attachment": {"type": "image",
            "payload": {"url": "https://twocomms.test/media/native-classic-grid.webp", "is_reusable": True}}}}
        chart = {"schema": "catalog-choice-chart.v1", "invitation_id": 90, "scope": {**scope, "igsid": "customer"},
            "product_id": 112, "configuration_selector": {"fit_code": "classic"}, "grid_id": 8,
            "image_url": payload["message"]["attachment"]["payload"]["url"], "payload_digest": _digest(payload),
            "presentation_context": {"schema": "discussed-product-configuration.v1", "product_focus": deepcopy(FOCUS),
                "fit_code": "classic", "proof_digest": "e" * 64,
                "scope": {"revision_id": 90, "snapshot_digest": scope["revision_snapshot_digest"]}}}
        chart["url_digest"] = _digest(chart["image_url"])
        chart["chart_digest"] = _digest(chart)
        chart["hmac"] = salted_hmac("catalog-choice-chart.v1", chart["chart_digest"], algorithm="sha256").hexdigest()
        effect = {"group": "template", "kind": "image", "payload": payload,
            "projection_metadata": {"catalog_chart": chart, "prepared_payload_digest": _digest(payload)}}
        return plan, effect

    def test_explicit_chart_request_has_own_obligation_across_languages(self):
        for text in ("Скиньте розмірну сітку", "Пришлите размерную сетку", "Send the size chart",
            "Покажіть таблицю розмірів classic футболки"):
            with self.subTest(text=text):
                plan, _ = self.plan_and_effect(text)
                self.assertIn("info:size_chart", [row["kind"] for row in plan.obligations])
                self.assertNotIn("info:presentation", [row["kind"] for row in plan.obligations])
                self.assertFalse(plan.coverage(response("Ось розмірна сітка."))["covered"])
                self.assertFalse(plan.coverage(response("Ось сітка.", show_products=[112]))["covered"])

    def test_only_authenticated_current_native_chart_covers_local_request(self):
        plan, effect = self.plan_and_effect()
        covered = plan.coverage(response("Ось розмірна сітка."), local=True,
            catalog_media_evidence={"native_size_chart": effect})
        self.assertEqual(covered["covered"], ["20:info:size_chart"])
        self.assertEqual(covered["disposition"], "complete")
        for key in ("snapshot", "source", "client_epoch", "focus", "payload", "signature"):
            with self.subTest(key=key):
                changed_plan, changed = plan, deepcopy(effect)
                if key in {"snapshot", "source", "client_epoch"}:
                    field, value = {"snapshot": ("revision_snapshot_digest", "f" * 64),
                        "source": ("source_message_ids", [21]), "client_epoch": ("client_epoch", 13)}[key]
                    changed_plan = replace(plan, configuration={**plan.configuration,
                        "native_chart_scope": {**plan.configuration["native_chart_scope"], field: value}})
                elif key == "focus":
                    changed_plan = replace(plan, product_focus={**FOCUS, "proof_digest": "f" * 64})
                elif key == "payload":
                    changed["payload"]["recipient"]["id"] = "foreign"
                else:
                    changed["projection_metadata"]["catalog_chart"]["hmac"] = "forged"
                self.assertEqual(changed_plan.coverage(response("Ось сітка."), local=True,
                    catalog_media_evidence={"native_size_chart": changed})["covered"], [])

    def test_native_chart_does_not_close_mixed_price_or_purchase_obligations(self):
        for text, remaining in (("Скиньте розмірну сітку. Скільки коштує?", "info:price"),
            ("Скиньте розмірну сітку. Хочу замовити", "purchase_requested")):
            with self.subTest(text=text):
                plan, effect = self.plan_and_effect(text)
                covered = plan.coverage(response("Ось розмірна сітка."), local=True,
                    catalog_media_evidence={"native_size_chart": effect})
                self.assertIn("20:info:size_chart", covered["covered"])
                self.assertIn("20:" + remaining, covered["remaining"])
                self.assertEqual(covered["disposition"], "recovery")
