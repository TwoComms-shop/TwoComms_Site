"""No-provider bounded selection response contract regressions."""
from decimal import Decimal

from django.test import SimpleTestCase

from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.services.ig_response_control import parse_structured_response, structured_response_instruction, structured_response_schema
from management.services.ig_response_plan import build_response_plan, asks_next_selector, REVISION_PROVIDER_CONTROL_KINDS


class ResponsePlanTests(SimpleTestCase):
    def plan(self, *, size="L", product=None, missing=None, purchase=True):
        values = {"size": size}
        if purchase:
            values["purchase_requested"] = True
        return build_response_plan(
            preferences={"values": values, "evidence": {key: {"source_message_id": 8, "decision_id": 2,
                         "transition_id": 3, "source_digest": "a" * 64} for key in values}, "session_id": 4, "line_id": "line:1"},
            readiness={"has_product": bool(product), "product": {"id": product}, "missing": missing or ["product"],
                       "size": {"selected": size}}, context=ReplyTruthContext(),
            sources=[{"message_id": 8, "role": "user", "text": "L, хочу замовити"}],
        )

    def response(self, text, controls=()):
        return parse_structured_response({"reply_text": text, "controls": list(controls)})

    def test_size_without_product_is_choice_not_configuration(self):
        plan = self.plan()
        context = plan.truth_context(ReplyTruthContext())
        self.assertTrue(validate_reply_truth("Ви обрали розмір L.", context=context).valid)
        self.assertTrue(validate_reply_truth("Вы выбрали размер L.", context=context).valid)
        self.assertTrue(validate_reply_truth("You selected size L.", context=context).valid)
        self.assertTrue(validate_reply_truth("You have chosen size L.", context=context).valid)
        self.assertFalse(validate_reply_truth("Розмір L є в наявності.", context=context).valid)
        self.assertIn("unverified_availability", validate_reply_truth("L есть в наличии.", context=context).reasons)
        self.assertFalse(validate_reply_truth("Заказ оформлен.", context=context).valid)
        self.assertFalse(validate_reply_truth("Ціна 500 грн.", context=context).valid)
        self.assertEqual(context.allowed_sizes, ())

    def test_mixed_source_choice_does_not_authorize_stock_in_another_clause(self):
        context = self.plan().truth_context(ReplyTruthContext())
        for reply in ("You selected size L, we have size L.", "Ви обрали розмір L, ми маємо розмір L.",
                      "Вы выбрали размер L, у нас есть размер L.", "Ви обрали розмір L, у нас є L."):
            result = validate_reply_truth(reply, context=context)
            self.assertFalse(result.valid, reply)
            self.assertIn("unverified_availability", result.reasons)

    def test_choice_does_not_authorize_wrong_size(self):
        context = self.plan().truth_context(ReplyTruthContext())
        self.assertIn("configuration_mismatch", validate_reply_truth("Ви обрали розмір XL.", context=context).reasons)

    def test_size_ack_does_not_close_purchase(self):
        coverage = self.plan().coverage(self.response("Ви обрали розмір L."))
        self.assertEqual(coverage["covered"], ["8:size"])
        self.assertEqual(coverage["remaining"], ["8:purchase_requested"])
        self.assertEqual(coverage["disposition"], "recovery")

    def test_negative_or_quoted_size_mention_is_not_acknowledgment(self):
        plan = self.plan()
        for text in ("Ви не обрали розмір L.", "Ви обрали не розмір L.", "Клієнт написав «L».", "You did not select size L."):
            self.assertIn("8:size", plan.coverage(self.response(text))["remaining"])
        self.assertIn("8:size", plan.coverage(self.response("Ви обрали розмір L."))["covered"])

    def test_single_quoted_choice_and_negative_contractions_leave_size_uncovered(self):
        plan = build_response_plan(preferences={"values": {"size": "L"}, "evidence": {"size": {"source_message_id": 8}}},
            readiness={"missing": ["product"]}, context=ReplyTruthContext(),
            sources=[{"message_id": 8, "role": "user", "text": "L"}])
        for text in ("Клієнт написав 'Ви обрали розмір L'.", "Клієнт написав ‘Ви обрали розмір L’.",
                     "Клієнт написав 'I'm sure you selected size L'.",
                     "Клієнт написав ‘I’m sure you selected size L’.",
                     "You haven't requested size L.", "You haven’t requested size L.",
                     "You hasn't requested size L.", "You didn’t select size L."):
            self.assertEqual(plan.coverage(self.response(text))["remaining"], ["8:size"], text)
        self.assertEqual(plan.coverage(self.response("You have chosen size L."))["remaining"], [])
        self.assertEqual(plan.coverage(self.response("I'm sure you selected size L."))["remaining"], [])

    def test_quoted_product_report_does_not_acknowledge_selection(self):
        plan = build_response_plan(preferences={"values": {"product_id": 55}, "evidence": {"product_id": {"source_message_id": 8}}},
            readiness={"has_product": True, "product": {"id": 55, "title": "Reality Bends"}}, context=ReplyTruthContext(),
            sources=[{"message_id": 8, "role": "user", "text": "Reality Bends"}])
        for text in ("Клієнт написав «Reality Bends».", "Ви не обрали Reality Bends.",
                     "Клієнт написав 'Ви обрали Reality Bends'.", "Клієнт написав ‘Ви обрали Reality Bends’."):
            self.assertEqual(plan.coverage(self.response(text))["remaining"], ["8:product_id"])
        self.assertEqual(plan.coverage(self.response("Ви обрали Reality Bends."))["remaining"], [])

    def test_missing_model_question_waits_customer_without_repeating_size(self):
        plan = self.plan()
        response = self.response("Ви обрали розмір L. Яку модель або принт ви хочете?")
        self.assertEqual(plan.next_selector, "product")
        self.assertEqual(plan.validate(response), "")
        self.assertEqual(plan.coverage(response)["disposition"], "waiting_on_customer")
        self.assertEqual(plan.validate(self.response("Який розмір ви обираєте?")), "response_plan_repeated_selector")

    def test_unrelated_question_does_not_discharge_missing_model_wait(self):
        plan = self.plan()
        for text in ("Ви обрали розмір L. Оберемо модель. Як ваші справи?",
                     "Ви обрали розмір L. Модель обрана. Вам зручно завтра?"):
            self.assertFalse(asks_next_selector(text, "product"))
            self.assertEqual(plan.coverage(self.response(text))["disposition"], "recovery")
        self.assertFalse(asks_next_selector("Ви написали «Яку модель?»", "product"))
        self.assertTrue(asks_next_selector("Яку модель: «A» чи «B»?", "product"))

    def topic_plan(self, text, *, priced=False):
        return build_response_plan(preferences={"values": {"size": "L"},
            "evidence": {"size": {"source_message_id": 8}}},
            readiness={"has_product": priced, "product": {"id": 55 if priced else None}, "missing": [] if priced else ["product"]},
            context=ReplyTruthContext(authorized_prices=(Decimal("600"),) if priced else (),
                explicitly_qualified_standard_dispatch_days=(1, 3)),
            sources=[{"message_id": 8, "role": "user", "text": text}])

    def test_same_source_price_request_needs_canonical_price_answer(self):
        plan = self.topic_plan("L. Скільки коштує?", priced=True)
        self.assertIn("8:info:price", plan.coverage(self.response("Ви обрали розмір L."))["remaining"])
        self.assertEqual(plan.coverage(self.response("Ви обрали розмір L. Ціна 600 грн."))["disposition"], "complete")
        for text in ("Ви обрали розмір L. Ціна 500 грн.", "Ви обрали розмір L. Вам 600 грн підійде?",
                     "Ви обрали розмір L. Ціна 600 грн?", "Ви обрали розмір L. Не знаю ціну 600 грн."):
            self.assertIn("8:info:price", plan.coverage(self.response(text))["remaining"])

    def test_imperative_price_request_survives_generic_selector_reply(self):
        plan = self.topic_plan("L. Підкажіть ціну цього худі", priced=True)
        coverage = plan.coverage(self.response("Ви обрали розмір L. Який стиль вам подобається?"))
        self.assertIn("8:info:price", coverage["remaining"])
        self.assertEqual(coverage["disposition"], "recovery")
        for text in ("L. Не підкажіть ціну цього худі", "L. Клієнт написав «Підкажіть ціну цього худі»",
                     "L. Клієнт написав 'Please tell me the price'", "L. Don't tell me the price"):
            plan = self.topic_plan(text, priced=True)
            self.assertFalse(any(item["kind"] == "info:price" for item in plan.obligations), text)

    def test_dispatch_answer_requires_canonical_qualified_window(self):
        plan = self.topic_plan("L. Коли відправите?")
        self.assertEqual(plan.coverage(self.response("Ви обрали розмір L. Зазвичай підготовка до відправлення займає 1–3 дні після оплати."))["disposition"], "complete")
        self.assertIn("8:info:dispatch_timing", plan.coverage(self.response("Ви обрали розмір L. Відправимо завтра."))["remaining"])

    def test_requested_photo_requires_presentation_effect(self):
        for text in ("L. Покажіть фото", "L. Покажіть фото?", "L. Show photos?", "L. Send images?"):
            plan = self.topic_plan(text)
            self.assertEqual(plan.coverage(self.response("Ви обрали розмір L."))["remaining"], ["8:info:presentation"])
            self.assertEqual(plan.coverage(self.response("Ви обрали розмір L.", [{"kind": "show_products", "value": [55]}]))["disposition"], "complete")

    def test_multilingual_color_and_garment_wishes_are_acknowledged_without_stock(self):
        plan = build_response_plan(preferences={"values": {"color": "black", "garment_type": "tshirt"},
            "evidence": {field: {"source_message_id": 8} for field in ("color", "garment_type")}},
            readiness={"missing": ["product"]}, context=ReplyTruthContext(),
            sources=[{"message_id": 8, "role": "user", "text": "Чорна футболка"}])
        for text in ("Ви обрали колір чорний. Ви обрали футболку.",
                     "Вы выбрали цвет чёрный. Вы выбрали футболку.",
                     "You selected color black. You selected a t-shirt."):
            self.assertTrue(validate_reply_truth(text, context=plan.truth_context(ReplyTruthContext())).valid)
            self.assertEqual(plan.coverage(self.response(text))["disposition"], "complete")
        self.assertFalse(validate_reply_truth("Чорний колір є в наявності, розмір L є.", context=plan.truth_context(ReplyTruthContext())).valid)

    def test_all_parser_colors_support_scoped_ukrainian_russian_acknowledgment(self):
        from management.services.ig_commerce_turns import parse_turn
        colors = (("black", "чорний", "чёрный", "чорну", "черную"),
                  ("white", "білий", "белый", "білу", "белую"),
                  ("blue", "синій", "синий", "синю", "синюю"),
                  ("pink", "рожевий", "розовый", "рожеву", "розовую"),
                  ("grey", "сірий", "серый", "сіру", "серую"),
                  ("green", "зелений", "зелёный", "зелену", "зелёную"))
        for canonical, uk, ru, uk_inflected, ru_inflected in colors:
            for source_color, reply in ((uk, f"Ви обрали колір {uk}."), (ru, f"Вы выбрали цвет {ru}."),
                    (uk, f"Ви обрали {uk_inflected} футболку."),
                    (ru_inflected, f"Вы выбрали {ru_inflected} футболку.")):
                with self.subTest(color=canonical, reply=reply):
                    source = f"Хочу {source_color} футболку"
                    self.assertEqual(parse_turn(source).field_updates.get("color"), canonical)
                    plan = build_response_plan(preferences={"values": {"color": canonical},
                        "evidence": {"color": {"source_message_id": 8}}}, readiness={"missing": ["product"]},
                        context=ReplyTruthContext(), sources=[{"message_id": 8, "role": "user", "text": source}])
                    context = plan.truth_context(ReplyTruthContext())
                    self.assertTrue(validate_reply_truth(reply, context=context).valid)
                    self.assertEqual(plan.coverage(self.response(reply))["remaining"], [])
                    for negative in (f"Ви не обрали колір {source_color}.", f"Клієнт написав «{source_color}»."):
                        self.assertIn("8:color", plan.coverage(self.response(negative))["remaining"])
                    self.assertFalse(validate_reply_truth(f"Колір {source_color} доступний.", context=context).valid)
                    self.assertFalse(validate_reply_truth(f"Ви обрали колір {source_color} і ми маємо колір {source_color}.", context=context).valid)

    def test_fit_withdrawal_is_covered_by_replacement_question_not_generic_debt(self):
        plan = build_response_plan(preferences={"values": {"purchase_requested": True},
            "evidence": {"purchase_requested": {"source_message_id": 8}}},
            readiness={"missing": ["product"]}, context=ReplyTruthContext(), sources=[
                {"message_id": 8, "role": "user", "text": "Хочу замовити"},
                {"message_id": 9, "role": "user", "text": "Не хочу оверсайз"}])
        coverage = plan.coverage(self.response("Зрозуміло. Яку посадку ви обираєте?"))
        self.assertEqual(plan.next_selector, "fit")
        self.assertEqual(coverage["covered"], ["9:withdrawal:fit"])
        self.assertEqual(coverage["remaining"], ["8:purchase_requested"])
        self.assertEqual(coverage["disposition"], "waiting_on_customer")

    def test_missing_model_can_wait_for_price_but_independent_service_remains(self):
        plan = self.topic_plan("L. Скільки коштує?")
        self.assertEqual(plan.coverage(self.response("Ви обрали розмір L. Яку модель ви хочете?"))["disposition"], "waiting_on_customer")
        for source in ("L. Коли доставка?", "L. Чи можна свій принт?"):
            plan = self.topic_plan(source)
            self.assertEqual(plan.coverage(self.response("Ви обрали розмір L. Яку модель ви хочете?"))["disposition"], "recovery")

    def test_known_and_unknown_question_in_same_source_remain_distinct(self):
        plan = self.topic_plan("L. Скільки коштує? Чи можна самовивозом?", priced=True)
        coverage = plan.coverage(self.response("Ви обрали розмір L. Ціна 600 грн."))
        self.assertEqual(coverage["remaining"], ["8:info:question"])
        self.assertEqual(coverage["disposition"], "recovery")

    def test_greeting_in_choice_bundle_has_no_false_obligation(self):
        plan = build_response_plan(preferences={"values": {"size": "L"}, "evidence": {"size": {"source_message_id": 8}}},
            readiness={"missing": ["product"]}, context=ReplyTruthContext(), sources=[
                {"message_id": 7, "role": "user", "text": "Привіт!"}, {"message_id": 8, "role": "user", "text": "L"}])
        self.assertEqual(plan.coverage(self.response("Ви обрали розмір L."))["disposition"], "complete")

    def test_ambiguous_model_choice_is_not_closed_by_size_ack(self):
        plan = build_response_plan(preferences={"values": {"size": "L", "model_query": "Reality Bends або Classic"},
            "evidence": {key: {"source_message_id": 8} for key in ("size", "model_query")}},
            readiness={"missing": ["product"]}, context=ReplyTruthContext(),
            sources=[{"message_id": 8, "role": "user", "text": "Reality Bends або Classic, розмір L"}])
        self.assertEqual(plan.coverage(self.response("Ви обрали розмір L."))["remaining"], ["8:model_query"])
        self.assertEqual(plan.coverage(self.response("Ви обрали розмір L. Яку саме модель ви обираєте?"))["disposition"], "waiting_on_customer")

    def test_fit_missing_preserves_size_and_unknown_stock(self):
        plan = self.plan(product=55, missing=["fit", "size", "color"])
        self.assertEqual(plan.next_selector, "fit")
        self.assertFalse(plan.configuration["applicability_known"])
        self.assertEqual(plan.authority["sizes"], [])

    def test_source_values_require_per_field_evidence(self):
        plan = build_response_plan(preferences={"values": {"size": "L", "purchase_requested": True}},
                                   readiness={"missing": ["product"]}, context=ReplyTruthContext())
        self.assertEqual(plan.choices, {})
        self.assertEqual(plan.obligations, ())

    def test_bounded_schema_has_selection_without_artificial_checkout(self):
        schema = structured_response_schema(allowed_kinds=REVISION_PROVIDER_CONTROL_KINDS)
        kinds = schema["properties"]["controls"]["items"]["properties"]["kind"]["enum"]
        self.assertIn("size", kinds)
        self.assertIn("product", kinds)
        self.assertNotIn("item", kinds)
        self.assertNotIn("objhandle", kinds)
        self.assertNotIn("price", kinds)
        # Existing trusted parser remains compatible but executor rejects items.
        response = self.response("I'll create the order.", [{"kind": "item", "value": "55|1|L|classic"}])
        self.assertEqual(self.plan().validate(response), "revision_cart_selection_unsupported")
        instruction = structured_response_instruction(allowed_kinds=REVISION_PROVIDER_CONTROL_KINDS)
        self.assertIn("Allowed kind values:", instruction)
        self.assertEqual(structured_response_schema()["properties"]["controls"]["items"]["properties"]["kind"]["enum"].count("item"), 1)

    def test_exact_money_authority_is_not_changed(self):
        context = self.plan().truth_context(ReplyTruthContext(authorized_prices=(Decimal("600"),)))
        self.assertTrue(validate_reply_truth("Ціна 600 грн.", context=context).valid)
        self.assertFalse(validate_reply_truth("Ціна 500 грн.", context=context).valid)

    def test_old_choice_does_not_make_current_noncommerce_sources_owed(self):
        for text in ("Дякую!", "А чи шукаєте ви працівників?"):
            plan = build_response_plan(preferences={"values": {"size": "L"}, "evidence": {"size": {"source_message_id": 2}}},
                readiness={"has_product": False, "missing": ["product"]}, context=ReplyTruthContext(),
                sources=[{"message_id": 9, "role": "user", "text": text}])
            self.assertEqual(plan.obligations, ())
            self.assertEqual(plan.coverage(self.response("Дякую за повідомлення."))["disposition"], "complete")

    def test_newer_source_preference_does_not_enter_sealed_plan(self):
        plan = build_response_plan(preferences={"values": {"size": "XL"}, "evidence": {"size": {"source_message_id": 10}}},
            readiness={"has_product": False, "missing": ["product"]}, context=ReplyTruthContext(),
            sources=[{"message_id": 9, "role": "user", "text": "L"}])
        self.assertEqual(plan.choices, {})
        self.assertNotIn("XL", plan.prompt_guidance())


from django.test import TransactionTestCase, override_settings
from management import tests_ig_revision_live as live_fixture


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class ResponsePlanSourceTests(TransactionTestCase):
    """Retained anonymized source→plan→fallback contract; no provider dispatch."""
    reset_sequences = True
    setUp = live_fixture.RevisionLiveTests.setUp
    _message = live_fixture.RevisionLiveTests._message
    _prepare = live_fixture.RevisionLiveTests._prepare
    _replace_bundle = live_fixture.RevisionLiveTests._replace_bundle
    _generate = live_fixture.RevisionLiveTests._generate
    _execute = live_fixture.RevisionLiveTests._execute

    def reduce(self):
        from management.services.ig_revision_commerce import reduce_revision_commerce
        from management.services.ig_revision_outbox import PublicationBinding
        result = reduce_revision_commerce(self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(result.ready, result.reason)
        self.revision.refresh_from_db()
        self.customer.refresh_from_db()

    def test_partial_l_purchase_acknowledged_with_missing_model_and_owed_purchase(self):
        from management.services.ig_response_guard import build_source_preference_fallback
        self._replace_bundle(["L, хочу замовити"])
        self.reduce()
        response, proof = build_source_preference_fallback(self.customer, revision=self.revision)
        self.assertIsNotNone(response, proof)
        self.assertIn("розмір L", response.reply_text)
        self.assertIn("модель", response.reply_text)
        self.assertEqual(response.control, {})
        self.assertEqual(proof["coverage"]["disposition"], "waiting_on_customer")
        self.assertEqual(len(proof["coverage"]["remaining"]), 1)
        self.assertTrue(proof["coverage"]["remaining"][0].endswith(":purchase_requested"))
        self.assertIsNone(self.customer.current_product_id)

    def test_current_receipt_admits_fallback_with_source_authority_not_stock(self):
        from management.services.ig_revision_live import RevisionGenerationBoundary
        from management.services.ig_revision_outbox import PublicationBinding
        self._replace_bundle(["L, хочу замовити"])
        self.reduce()
        boundary = RevisionGenerationBoundary(self.revision, self.token, self.settings,
            PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        policy = {"instruction_publication": {"id": self.publication.pk,
                  "version": self.publication.version, "hash": self.publication.snapshot_hash}}
        response, proof = boundary.contextual_fallback(policy_manifest=policy)
        self.assertIsNotNone(response, proof)
        self.assertIn("source_preferences", {row["claim"] for row in boundary.authority.fact_bindings})
        self.assertNotIn("catalog_configuration", {row["claim"] for row in boundary.authority.fact_bindings})

    def test_unknown_independent_request_is_retained_in_coverage(self):
        from management.services.ig_response_plan import capture_response_plan
        self._replace_bundle(["L, хочу замовити", "А чи шукаєте ви працівників?"])
        self.reduce()
        plan = capture_response_plan(self.customer, revision=self.revision)
        coverage = plan.coverage(self.response("Ви обрали розмір L. Яку модель ви хочете?"))
        self.assertEqual(coverage["disposition"], "recovery")
        self.assertTrue(any(item.endswith(":info:recruitment") for item in coverage["remaining"]))

    response = ResponsePlanTests.response

    def _assert_mixed_delivery(self, question, topic):
        self._replace_bundle(["L. " + question])
        self.parsed = {"reply_text": "Ви обрали розмір L.", "controls": []}
        if topic == "service":
            # Unified routing recognizes this current custom-print request.
            # Supply its required structured evidence while still omitting the
            # service answer, whose delivery debt remains asserted below.
            self.parsed["turn_intelligence"] = {
                "catalog_candidates": [], "intent": "custom_print", "confidence": 0.9,
                "audio_status": "not_applicable", "transcript": "",
            }
        result, generation, http = self._execute()
        self.assertEqual(http.call_count, 1, result.reasons)
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.revision.refresh_from_db()
        coverage = self.revision.action_receipts["response_coverage"]
        self.assertTrue(any(value.endswith(":info:" + topic) for value in coverage["remaining"]), coverage)

    def test_mixed_size_price_survives_actual_delivery(self):
        self._assert_mixed_delivery("Скільки коштує?", "price")

    def test_mixed_size_shipping_survives_actual_delivery(self):
        self._assert_mixed_delivery("Коли доставка?", "shipping")

    def test_mixed_size_service_survives_actual_delivery(self):
        self._assert_mixed_delivery("Чи можна свій принт?", "service")

    def test_photo_question_with_choice_closes_only_after_real_presentation_sent(self):
        import json
        import tempfile
        from django.core.files.base import ContentFile
        from storefront.models import Category, Product
        media_dir = tempfile.TemporaryDirectory(prefix="twc-plan-photo-", dir="/tmp")
        self.addCleanup(media_dir.cleanup)
        media_settings = override_settings(MEDIA_ROOT=media_dir.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        category = Category.objects.create(name="Plan photos", slug="plan-photos")
        product = Product.objects.create(title="Plan photo product", slug="plan-photo-product",
            category=category, price=900, status="published")
        product.main_image.save("plan-photo.jpg", ContentFile(b"trusted-local-catalog-test-image"), save=True)
        self._replace_bundle(["L. Покажіть фото?"])
        self.parsed = {"reply_text": "Ви обрали розмір L.", "controls": [{"kind": "show_products", "value": [product.pk]}]}
        result, generation, http = self._execute([
            (200, json.dumps({"message_id": "photo-sent"})),
            (200, json.dumps({"message_id": "photo-answer-sent"})),
        ])
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 2)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        effects = self.revision.delivery_effects.all()
        self.assertTrue(any(effect.group == "catalog_media" and effect.state == "sent" for effect in effects))
        self.assertTrue(all(effect.state == "sent" for effect in effects))

    def test_mixed_size_canonical_dispatch_answer_completes_actual_delivery(self):
        self._replace_bundle(["L. Коли відправите?"])
        self.parsed = {"reply_text": "Ви обрали розмір L. Зазвичай підготовка до відправлення займає 1–3 дні після оплати.", "controls": []}
        result, generation, http = self._execute()
        self.assertEqual(http.call_count, 1, result.reasons)
        self.assertEqual(result.state, "completed", result.reasons)

    def test_mixed_size_canonical_price_answer_completes_actual_delivery(self):
        from storefront.models import Category, Product
        from productcolors.models import Color, ProductColorVariant
        category = Category.objects.create(name="Plan price", slug="plan-price")
        product = Product.objects.create(title="Reality Bends", slug="plan-priced-product", category=category, price=900, status="published")
        color = Color.objects.create(name="Plan price black", primary_hex="#111111")
        ProductColorVariant.objects.create(product=product, color=color, price_override=900, is_default=True)
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import parse_turn
        from management.services.ig_revision_commerce import resolve_source_product_request
        prior = self._message("Reality Bends", "price-model-prior")
        request = resolve_source_product_request(self.customer, prior, parse_turn(prior.text))
        self.assertEqual(request.exact_product_id, product.pk)
        apply_turn(self.customer, prior, request, reply_payload={})
        self.source = self._message("L. Скільки коштує?", "price-current-source")
        from management.models import IgTurnMessage
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=2, role="user")
        self._replace_bundle([self.source.text])
        self.parsed = {"reply_text": "Ви обрали розмір L. Ціна 900 грн.", "controls": []}
        result, generation, http = self._execute()
        self.assertEqual(http.call_count, 1, result.reasons)
        self.assertEqual(result.state, "completed", result.reasons)

    def test_disabled_ai_captures_source_choices_without_generation(self):
        from management.services.ig_commerce_projection import source_preferences_for
        self.settings.ai_enabled = False
        self.settings.trigger_text = "start"
        self.settings.save(update_fields=["ai_enabled", "trigger_text"])
        self._replace_bundle(["L, хочу замовити"])
        result, generation, http = self._execute()
        generation.assert_not_called()
        http.assert_not_called()
        self.assertEqual(source_preferences_for(self.customer)["values"]["size"], "L")
        self.assertTrue(source_preferences_for(self.customer)["values"]["purchase_requested"])

    def test_static_reply_keeps_purchase_semantic_debt(self):
        self.settings.ai_enabled = False
        self.settings.trigger_text = "start"
        self.settings.reply_text = "Вітаємо! Чим можемо допомогти?"
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        self._replace_bundle(["start", "L, хочу замовити"])
        result, generation, http = self._execute()
        generation.assert_not_called()
        self.assertEqual(http.call_count, 1, result.reasons)
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.revision.refresh_from_db()
        self.assertTrue(any(item.endswith(":purchase_requested") for item in self.revision.action_receipts["response_coverage"]["remaining"]))

    def test_ambiguous_named_models_keep_source_debt_after_size_ack_delivery(self):
        from storefront.models import Category, Product
        category = Category.objects.create(name="Plan models", slug="plan-models")
        for index, title in enumerate(("Reality Bends", "Classic")):
            Product.objects.create(title=title, slug=f"plan-model-{index}", category=category, price=900, status="published")
        self._replace_bundle(["Reality Bends або Classic, розмір L"])
        self.parsed = {"reply_text": "Ви обрали розмір L.", "controls": []}
        result, generation, http = self._execute()
        self.assertEqual(http.call_count, 1, result.reasons)
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.revision.refresh_from_db()
        self.assertTrue(any(item.endswith(":model_query") for item in self.revision.action_receipts["response_coverage"]["remaining"]))

    def test_newer_reduced_inbound_is_omitted_from_captured_choice(self):
        from management.services.ig_commerce_state import apply_turn
        from management.services.ig_commerce_turns import understand_turn
        from management.services.ig_response_plan import capture_response_plan
        self._replace_bundle(["L, хочу замовити"])
        self.reduce()
        newer = self._message("Не L, а XL", "newer-unsealed")
        decision = apply_turn(self.customer, newer, understand_turn(newer.text), reply_payload={})
        self.assertTrue(decision.accepted)
        self.customer.refresh_from_db()
        plan = capture_response_plan(self.customer, revision=self.revision)
        self.assertEqual(plan.choices, {})
        self.assertNotIn("XL", plan.prompt_guidance())
