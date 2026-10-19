"""Customer phrasing through real prompt, validator and outbox; fake transport only."""
import json
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_ig_revision_live as live
from management import tests_ig_media_live_integration as media
from management.models import IgCommerceSelectionSession


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class KnowledgeCustomerJourneyQATests(TransactionTestCase):
    setUp = live.RevisionLiveTests.setUp
    _message = live.RevisionLiveTests._message
    _prepare = live.RevisionLiveTests._prepare
    _replace_bundle = live.RevisionLiveTests._replace_bundle
    _execute = live.RevisionLiveTests._execute
    select_tee = media.CanonicalMediaIntegrationTests.select_published_tee_before_media

    def _generate(self, payload, **kwargs):
        validator = kwargs["result_validator"]
        self.decisions = getattr(self, "decisions", [])

        def observe(parsed, **options):
            decision = validator(parsed, **options)
            self.decisions.append(decision)
            if not decision.valid:
                from management.services.call_ai_analysis import CallAIAnalysisError
                raise CallAIAnalysisError("qa_rejected:" + ",".join(decision.reason_codes))
            return decision

        return live.RevisionLiveTests._generate(self, payload, **{**kwargs, "result_validator": observe})

    def published_hoodie(self):
        from storefront.models import Category, Product, ProductFitOption
        category = Category.objects.create(name="Худі", slug="hoodies")
        product = Product.objects.create(title="QA Night hoodie", slug="qa-night-hoodie",
            category=category, status="published", price=1400)
        ProductFitOption.objects.create(product=product, code="oversize", label="Oversize", is_active=True)
        return product

    def answer(self, sources, reply, *, controls=(), before_http=None):
        self._replace_bundle(sources)
        self.parsed = {"reply_text": reply, "controls": list(controls), "turn_intelligence": {
            "catalog_candidates": [], "transcript": "", "intent": "product_information", "confidence": 0.9}}
        result, generate, http = self._execute(before_http)
        self.revision.refresh_from_db()
        self.assertEqual(generate.call_count, 1)
        self.assertTrue(self.generation_calls)
        self.prompt = json.dumps(self.generation_calls[0], ensure_ascii=False)
        self.sent_text = " ".join(str((effect.payload.get("message") or {}).get("text") or "")
            for effect in self.revision.delivery_effects.filter(state="sent"))
        return result, http

    def assert_rejected(self, sources, reply):
        result, http = self.answer(sources, reply)
        self.assertTrue(any(not decision.valid for decision in self.decisions),
            {"state": result.state, "reply": self.sent_text})
        self.assertNotIn(reply, self.sent_text)

    def test_category_burst_answers_composition_density_and_care_without_changing_cart(self):
        self.select_tee()
        self.published_hoodie()
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        before = session.lines
        reply = ("Наші поточні худі: 90% бавовни / 10% поліестеру, близько 320 г/м². "
            "Наші поточні худі радимо прати при 30°C за рекомендаціями догляду, без агресивної хімії.")
        result, http = self.answer(["А худі?", "Який склад і щільність?", "А як їх прати?"], reply)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertIn("90%", self.prompt)
        self.assertIn("320", self.prompt)
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])
        session.refresh_from_db()
        self.assertEqual(session.lines, before)

    def test_current_tee_owner_quality_answer_survives_delivery(self):
        self.select_tee()
        reply = ("За оцінкою засновника, нанесення витримує понад 100 прань "
            "за рекомендованого догляду при 30°C. Це заява власника; конкретну проблему команда розглядає окремо.")
        result, http = self.answer(["Наскільки довговічний принт цієї футболки, що кажете про якість?"], reply)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertIn("100", self.prompt)
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])

    def test_spelled_cotton_percentage_does_not_become_false_pure_cotton_claim(self):
        self.select_tee()
        self.assert_rejected(["Який склад цієї футболки?"],
            "Склад цієї футболки — 100 відсотків бавовни.")

    def test_pure_cotton_paraphrase_does_not_override_reviewed_mixed_base(self):
        self.select_tee()
        self.assert_rejected(["Який склад цієї футболки?"],
            "Ця футболка повністю бавовняна, без домішок.")

    def test_russian_percentage_cannot_turn_hoodie_into_pure_polyester(self):
        self.published_hoodie()
        self.assert_rejected(["Какой состав у ваших худи?"],
            "Наши текущие худи — 100 процентов полиэстера.")

    def test_english_purity_claim_cannot_erase_tee_elastane(self):
        self.select_tee()
        self.assert_rejected(["What is this T-shirt made of?"],
            "This T-shirt is pure cotton without any synthetics.")

    def test_correct_spelled_percentages_cover_real_composition_question(self):
        self.select_tee()
        reply = "Склад цієї футболки: 95 відсотків бавовни / 5 відсотків еластану."
        result, http = self.answer(["Який склад цієї футболки?"], reply)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.action_receipts["response_coverage"]["remaining"], [])

    def test_negating_purity_then_giving_real_composition_is_allowed(self):
        self.select_tee()
        reply = "Ця футболка не повністю бавовняна. Склад: 95% бавовни / 5% еластану."
        result, http = self.answer(["Ця футболка повністю бавовняна? Який склад?"], reply)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)

    def test_washing_quality_claim_cannot_be_made_as_independent_question(self):
        self.select_tee()
        self.assert_rejected(["Як прати цю футболку?"],
            "Прати при 90°C. Чи зручно вам так?")

    def test_known_material_does_not_bless_future_batch_followup(self):
        self.select_tee()
        self.assert_rejected(["Який склад буде в наступній партії цих футболок?"],
            "Наступна партія цих футболок: 95% бавовни / 5% еластану.")

    def test_new_hoodie_context_cannot_get_retained_tee_composition(self):
        self.select_tee()
        self.published_hoodie()
        self.assert_rejected(["А худі?", "А склад який?"],
            "Наші поточні худі: 95% бавовни / 5% еластану.")

    def test_hoodie_cut_question_cannot_create_second_regular_base(self):
        self.published_hoodie()
        self.assert_rejected(["Який крій у ваших жіночих худі?"],
            "Наші поточні худі мають regular і oversize крій, обирайте будь-який.")

    def test_ordinary_refusal_cannot_get_false_200_uah_refund_promise(self):
        self.select_tee()
        self.assert_rejected(["А якщо я відмовлюсь від посилки на пошті, передоплату 200 грн повернете?"],
            "Так, передоплата 200 грн повністю повертається при звичайній відмові від посилки.")
        self.assertTrue(any("unverified_payment_policy" in decision.reason_codes for decision in self.decisions))

    def test_reviewed_conditional_200_policy_can_answer_what_deposit_means(self):
        from management.services.approved_public_facts import approved_public_facts

        self.select_tee()
        reply = next(row.public_text for row in approved_public_facts("uk") if row.key == "payment_boundary")
        result, http = self.answer(["Що означає передоплата 200 грн? Вона входить у вартість товару?"], reply)
        self.assertTrue(any(decision.valid for decision in self.decisions), result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertIn(reply, self.sent_text)

    def test_price_objection_cannot_be_answered_with_fake_laboratory_proof(self):
        self.select_tee()
        self.assert_rejected(["Чому так дорого? Які докази якості принта?"],
            "Лабораторний тест підтвердив, що принт витримує понад 100 прань.")

    def test_owner_wash_estimate_does_not_authorize_never_fades_promise(self):
        self.select_tee()
        self.assert_rejected(["Яка якість принта на цій футболці?"],
            "Принт на цій футболці ніколи не вицвітає.")

    def test_selected_sku_does_not_authorize_invented_design_story(self):
        self.select_tee()
        self.assert_rejected(["Розкажіть історію принта на цій футболці."],
            "Цей принт створили після нашої рятувальної місії в Херсоні.")

    def test_checking_exact_print_story_is_allowed_without_inventing_identity(self):
        from management.models import IgFollowUpTask

        self.select_tee()
        reply = "Історію саме цього принта уточню у засновника, щоб не переплутати версію малюнка."
        def case_before_send(*args, **kwargs):
            task = IgFollowUpTask.objects.get(client=self.customer, reason="revision_case:product_information_review")
            inquiry = task.manager_context["product_inquiries"]["inquiries"][0]
            self.assertEqual(inquiry["requested_value"], "design_story")
            self.assertEqual(inquiry["source_message_id"], self.source.pk)
            return 200, json.dumps({"message_id": "qa-story-answer"})
        result, http = self.answer(["Яка історія саме цього принта?"], reply,
            controls=({"kind": "manager", "value": True},), before_http=case_before_send)
        self.assertTrue(any(decision.valid for decision in self.decisions), result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertIn(reply, self.sent_text)
        task = IgFollowUpTask.objects.get(client=self.customer, reason="revision_case:product_information_review")
        self.assertEqual(task.manager_context["product_inquiries"]["inquiries"][0]["requested_value"], "design_story")

    def test_thanks_does_not_require_unrequested_custom_print_sales_copy(self):
        result, http = self.answer(["Дякую, усе зрозуміло!"], "Будь ласка!")
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.sent_text, "Будь ласка!")

    def original_brand_answer(self, language, question):
        from management.services.approved_public_facts import approved_public_facts
        from management.models import GeminiRequest

        self.select_tee()
        fact = next(row for row in approved_public_facts(language) if row.key == "brand_originality")
        result, http = self.answer([question], fact.public_text)
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertIn(fact.public_text, self.prompt)
        self.assertIn(fact.public_text, self.sent_text)
        lineage = GeminiRequest.objects.get().policy_manifest["knowledge_lineage"]["modules"]
        selected = [row for row in lineage if row["id"] == f"approved_public:{language}:brand_originality"]
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["consumed"])
        self.assertEqual(selected[0]["source_ref"], fact.source_ref)
        self.assertEqual(selected[0]["version"], fact.version)

    def test_original_prints_uk_fact_is_consumed_and_delivered(self):
        self.original_brand_answer("uk", "Ваші принти авторські чи берете з Pinterest?")

    def test_original_prints_ru_fact_is_consumed_and_delivered(self):
        self.original_brand_answer("ru", "Ваши принты авторские или берёте из Pinterest?")

    def test_original_prints_en_fact_is_consumed_and_delivered(self):
        self.original_brand_answer("en", "Are your prints original or from Pinterest?")

    def test_generic_brand_originality_cannot_launder_exact_invented_history(self):
        from management.services.approved_public_facts import approved_public_facts

        self.select_tee()
        fact = next(row for row in approved_public_facts("uk") if row.key == "brand_originality")
        self.assert_rejected(["Ваші принти авторські чи берете з Pinterest?"],
            fact.public_text + " Цей принт створили після нашої рятувальної місії в Херсоні.")


class MaterialParaphraseProofQATests(SimpleTestCase):
    def test_generic_originality_has_own_publication_and_no_exact_story_right(self):
        from unittest.mock import patch
        from management.services.approved_public_facts import approved_public_facts
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth

        for language, positive in (("uk", "Наші принти створені з нуля."),
                                   ("ru", "Наши принты созданы с нуля."),
                                   ("en", "Our prints are drawn from scratch.")):
            self.assertTrue(validate_reply_truth(positive, context=ReplyTruthContext()).valid, positive)
            def without_originality(locale):
                return tuple(row for row in original(locale) if row.key != "brand_originality")
            original = approved_public_facts
            with patch("management.services.approved_public_facts.approved_public_facts", side_effect=without_originality):
                self.assertIn("unverified_product_story", validate_reply_truth(positive, context=ReplyTruthContext()).reasons)
        for negative in ("Цей принт створений з нуля після рятувальної місії.",
            "Наші майбутні принти створені з нуля.", "Our next collection designs are drawn from scratch.",
            "Наші принти створені з нуля у Херсоні.", "Конкурентські принти створені з Pinterest."):
            self.assertIn("unverified_product_story", validate_reply_truth(negative, context=ReplyTruthContext()).reasons, negative)

    def test_reviewed_payment_proof_licenses_only_canonical_information(self):
        import hashlib
        from management.services.approved_public_facts import approved_public_facts
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth

        fact = next(row for row in approved_public_facts("uk") if row.key == "payment_boundary")
        proof = ("uk", fact.version, hashlib.sha256(fact.public_text.encode()).hexdigest())
        context = ReplyTruthContext(approved_payment_policy=(proof,))
        self.assertTrue(validate_reply_truth(fact.public_text, context=context).valid)
        self.assertTrue(validate_reply_truth(fact.public_text,
            context=ReplyTruthContext(approved_payment_policy=[list(proof)])).valid)
        self.assertTrue(validate_reply_truth(fact.public_text + " Можна повернутися до вибору кольору.", context=context).valid)
        for proofs in ((), (("uk", fact.version, "f" * 64),), (("uk", "old", proof[2]),),
                       (("xx", fact.version, proof[2]),), ({"text": fact.public_text},)):
            self.assertFalse(validate_reply_truth(fact.public_text,
                context=ReplyTruthContext(approved_payment_policy=proofs)).valid)
        for text in ("Ціна футболки — 200 грн.", "Оплату 200 грн підтверджено.",
                     "Комісія перевізника — 200 грн.", "Передоплата 200 грн повертається при звичайній відмові.",
                     fact.public_text + " Ви нічого не втрачаєте при відмові.",
                     fact.public_text + " Утримуємо тільки фактичну вартість доставки."):
            self.assertFalse(validate_reply_truth(text, context=context).valid, text)

    def test_real_reviewed_mixed_base_cannot_gain_purity_through_translation(self):
        from management.services.approved_public_facts import approved_base_assertion
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth

        proof = approved_base_assertion({"product": {"id": 22, "kind": "tshirts"},
            "fit": {"selected": "classic"}}, question="Який склад футболки?")
        self.assertIsNotNone(proof)
        context = ReplyTruthContext(approved_product_assertions=(proof,))
        for reply in ("Це 100 відсотків бавовни.", "Бавовни 100 відсотків.",
                      "Это сто процентов хлопка.", "Это полностью хлопковая футболка.",
                      "This is 100 percent cotton.", "Cotton is 100 per cent.",
                      "This is entirely cotton.", "This is only elastane.",
                      "Склад: 95,5% бавовни / 4,5% еластану."):
            with self.subTest(reply=reply):
                self.assertIn("unverified_product_material", validate_reply_truth(reply, context=context).reasons)
        for reply in ("Ця футболка містить бавовну.", "This shirt contains cotton and elastane.",
                      "All current T-shirts contain cotton.", "This is the only T-shirt with cotton.",
                      "This pure white T-shirt contains cotton and elastane.",
                      "Ця чиста біла футболка містить бавовну й еластан.",
                      "Це не 100 відсотків бавовни.", "This is not pure cotton.",
                      "Футболка повністю бавовняна?", 'Ви написали «100 відсотків бавовни».'):
            with self.subTest(reply=reply):
                quoted_context = ReplyTruthContext(approved_product_assertions=(proof,),
                    quoted_data=("100 відсотків бавовни",))
                self.assertTrue(validate_reply_truth(reply, context=quoted_context).valid, reply)

    def test_owner_estimate_does_not_authorize_absolute_color_durability(self):
        from management.services.approved_public_facts import approved_base_assertion
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth

        proof = approved_base_assertion({"product": {"id": 22, "kind": "tshirts"},
            "fit": {"selected": "classic"}}, question="Яка якість футболки?")
        context = ReplyTruthContext(approved_product_assertions=(proof,))
        for reply in ("Принт ніколи не вицвітає.", "Принт не вигоряє ніколи.",
                      "Принт никогда не выцветает.", "The print never fades."):
            self.assertIn("unverified_product_quality", validate_reply_truth(reply, context=context).reasons, reply)
        for reply in (proof["text"], "Не обіцяємо, що принт ніколи не вицвітає.",
                      "We do not guarantee the print never fades.", "Чи принт ніколи не вицвітає?"):
            self.assertTrue(validate_reply_truth(reply, context=context).valid, reply)

    def test_unknown_story_identity_preserves_neutral_examples_questions_and_source_quotes(self):
        from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth

        invented = ("Цей принт створили після рятувальної місії в Херсоні.",
            "Этот принт посвящён спасению животных после пожара.",
            "This design was inspired by our rescue mission in Kherson.",
            "The owl print symbolizes the founder's callsign.",
            "Історія цього принта почалася на війні.")
        for reply in invented:
            self.assertIn("unverified_product_story", validate_reply_truth(reply, context=ReplyTruthContext()).reasons, reply)
        for reply in ("Наші дизайни можуть мати особистий зміст.",
            "Наприклад, дизайн може бути присвячений місту.",
            "For example, a design could be inspired by a city.",
            "Історію саме цього принта уточню у засновника.",
            "Створимо принт за вашим макетом.",
            "Для цього принта regular означає прямий крій, oversize — вільніший.",
            "For this print, oversize means a looser cut.",
            "This design was not inspired by our rescue mission.",
            "Цей принт не створили після пожежі.",
            "Цей принт створили після пожежі?",
            'Ви написали «Цей принт створили після пожежі».'):
            context = ReplyTruthContext(quoted_data=("Цей принт створили після пожежі",))
            self.assertTrue(validate_reply_truth(reply, context=context).valid, reply)
