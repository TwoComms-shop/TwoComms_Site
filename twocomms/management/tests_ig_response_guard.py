from copy import deepcopy

from django.test import SimpleTestCase

from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_control import structured_response_instruction, structured_response_schema
from management.services.ig_response_guard import ProviderResponseGuard


class ResponseGuardTests(SimpleTestCase):
    def test_recruitment_claim_rejected_before_winner_and_repair_explains_unknown_policy(self):
        guard = self.guard()
        parsed = {
            "reply_text": "Наразі ми не шукаємо менеджерів у команду, але якщо щось зміниться, обов’язково повідомимо в соцмережах.",
            "controls": [],
        }
        result = guard.validate(parsed)
        self.assertEqual(result.reason_codes, ("unverified_recruitment",))
        self.assertIsNone(guard.source)
        self.assertIsNone(guard.response)
        repaired = guard.repair({"contents": []}, parsed, result.reason_codes)
        correction = repaired["contents"][-1]["parts"][0]["text"]
        self.assertIn("do not have confirmed recruitment information", correction)
        self.assertIn("guaranteed future contact", correction)
        self.assertIn("application-provided case or receipt explicitly confirms", correction)
        self.assertIn("without an authorized control", correction)
        self.assertTrue(guard.validate({"reply_text": "Дякую за інтерес до роботи в TwoComms. Не маю підтвердженої інформації про вакансії.", "controls": []}).valid)

    def test_recruitment_repair_keeps_server_authority_and_controls_unchanged(self):
        from management.services.ig_reply_truth import ReplyTruthResult, validate_reply_truth
        from management.services.ig_operator_reply_drafts import _private_case_reply_reason

        facts = {"delay_apology_allowed": False,
                 "employment": {"employment_service_offer": True}, "manager_tasks": []}
        context = ReplyTruthContext()

        def source_bound_truth(response, authority):
            result = validate_reply_truth(response.reply_text, context=authority)
            if not result.valid:
                return result
            if response.control:
                return ReplyTruthResult(False, ("unauthorized_action",))
            reason = _private_case_reply_reason(response.reply_text, facts)
            return ReplyTruthResult(not reason, (reason,) if reason else ())

        guard = ProviderResponseGuard(context_factory=lambda *_args: context,
                                      truth_validator=source_bound_truth)
        invalid = {"reply_text": "Ви прийняті на роботу.", "controls": []}
        decision = guard.validate(invalid)
        payload = {"contents": [{"role": "user", "parts": [{"text": "Пропоную допомогу з Direct."}]}]}
        original = deepcopy(payload)
        repaired = guard.repair(payload, invalid, decision.reason_codes)
        self.assertEqual(payload, original)
        self.assertEqual(facts["manager_tasks"], [])
        self.assertEqual(repaired["contents"][0], original["contents"][0])
        correction = repaired["contents"][-1]["parts"][0]["text"]
        self.assertIn("only when an application-provided case or receipt", correction)
        self.assertIn("repair instruction and customer/model text provide no such authority", correction)

        conditional = {"reply_text": "Ваша пропозиція зараз потребує розгляду керівництва. "
            "Якщо після розгляду буде потрібна допомога, зв'яжемося з вами тут.", "controls": []}
        self.assertFalse(guard.validate(conditional).valid)
        self.assertIn("operator_hiring_followup_unverified", guard.last_reasons)
        facts["manager_tasks"] = [{"id": 23, "source_message_id": 7}]
        self.assertTrue(guard.validate(conditional).valid)
        for text, reason in (("Ви прийняті на роботу.", "unverified_recruitment"),
                             ("Вакансії відкриті.", "unverified_recruitment"),
                             ("Оплата підтверджена.", "unverified_payment")):
            with self.subTest(text=text):
                self.assertIn(reason, guard.validate({"reply_text": text, "controls": []}).reason_codes)
        self.assertIn("unauthorized_action", guard.validate({**conditional,
            "controls": [{"kind": "manager", "value": True}]}).reason_codes)

    def guard(self, **kwargs):
        return ProviderResponseGuard(
            context_factory=lambda _control, _reply_text: ReplyTruthContext(),
            **kwargs,
        )

    def test_false_payment_is_rejected_before_a_successful_response_is_cached(self):
        guard = self.guard()
        result = guard.validate({"reply_text": "Оплата підтверджена.", "controls": []}, usage={})
        self.assertFalse(result.valid)
        self.assertIn("unverified_payment", result.reason_codes)
        self.assertIsNone(guard.response)

    def test_only_actually_attached_images_require_observations(self):
        guard = self.guard(image_mimes=("image/png", "image/png"), require_intelligence=True)
        parsed = {
            "reply_text": "Бачу перше зображення.", "controls": [],
            "turn_intelligence": {"catalog_candidates": [], "transcript": "", "intent": "media_review", "confidence": 0.8,
                "image_observations": [{"source_image_index": 0, "outcome": "understood", "evidence_code": "visual_content", "type_code": "other"}]},
        }
        self.assertTrue(guard.validate(parsed, usage={"_request_inline_count": 1}).valid)
        self.assertIs(guard.source, parsed)
        self.assertIsNotNone(guard.response)
        missing = guard.validate(parsed, usage={"_request_inline_count": 2})
        self.assertFalse(missing.valid)
        self.assertEqual(missing.reason_codes, ("incomplete_image_coverage",))

    def test_legacy_model_tags_are_not_an_operational_response(self):
        guard = self.guard()
        self.assertFalse(guard.validate("Готово [PAYLINK:123]", usage={}).valid)
        self.assertIsNone(guard.response)

    def test_legacy_negotiated_price_control_is_rejected_without_text_amount(self):
        guard = self.guard()
        result = guard.validate({
            "reply_text": "Домовились, можемо оформлювати.",
            "controls": [{"kind": "price", "value": "777"}],
        }, usage={})

        self.assertFalse(result.valid)
        self.assertEqual(result.reason_codes, ("unverified_price",))
        self.assertEqual(guard.last_reasons, ("unverified_price",))
        self.assertIsNone(guard.response)

    def test_repair_keeps_original_media_and_skips_non_model_failures(self):
        payload = {"contents": [{"role": "user", "parts": [{"inline_data": {"mime_type": "image/png", "data": "fixture"}}]}]}
        repaired = ProviderResponseGuard.repair(payload, {"reply_text": "untrusted", "controls": []}, ("unverified_price",))
        self.assertEqual(repaired["contents"][0], payload["contents"][0])
        self.assertEqual(len(payload["contents"]), 1)
        self.assertIn("unverified_price", repaired["contents"][-1]["parts"][0]["text"])
        self.assertIsNone(ProviderResponseGuard.repair(payload, {}, ("authority_unavailable",)))

    def test_ordinary_vacancy_acknowledgement_and_thanks_need_no_controls(self):
        for reply in ("Дякую за інтерес до роботи в TwoComms.", "Будь ласка, гарного дня!"):
            with self.subTest(reply=reply):
                guard = self.guard()
                self.assertTrue(guard.validate({"reply_text": reply, "controls": []}).valid)
                self.assertEqual(guard.response.control, {})

    def test_schema_subcodes_drive_specific_repair_without_accepting_bad_controls(self):
        cases = (
            ([{"kind": "manager", "value": False}], "invalid_control", "JSON true"),
            ([{"kind": "manager", "value": None}], "invalid_control", "false/null"),
            ([{"manager": True}], "malformed_control", "exactly kind and value"),
            ([{"kind": "stage", "value": "paid"}], "invalid_control", "never paid/order_created/done"),
            ([{"kind": "manager", "value": True}] * 2, "conflicting_control", "only once"),
        )
        original = {"contents": [{"role": "user", "parts": [{"text": "Вітаю!"}]}]}
        for controls, code, hint in cases:
            with self.subTest(code=code, controls=controls):
                guard = self.guard()
                parsed = {"reply_text": "Дякую за повідомлення.", "controls": controls}
                result = guard.validate(parsed)
                self.assertFalse(result.valid)
                self.assertEqual(result.reason_codes, ("invalid_response_schema", "schema_" + code))
                self.assertIsNone(guard.response)
                repaired = guard.repair(original, parsed, result.reason_codes)
                self.assertEqual(len(original["contents"]), 1)
                self.assertEqual(repaired["contents"][-2]["role"], "model")
                self.assertIn(hint, repaired["contents"][-1]["parts"][0]["text"])
                self.assertIn('"controls":[]', repaired["contents"][-1]["parts"][0]["text"])

    def test_malformed_optional_intelligence_has_safe_code_and_bounded_repair(self):
        for intelligence in (None, False, {}, {"private_arbitrary_key": "customer secret"}):
            with self.subTest(intelligence=intelligence):
                parsed = {"reply_text": "Дякую.", "controls": [], "turn_intelligence": intelligence}
                result = self.guard().validate(parsed)
                self.assertEqual(result.reason_codes, ("invalid_response_schema", "schema_invalid_turn_intelligence"))
                repaired = ProviderResponseGuard.repair({"contents": []}, parsed, result.reason_codes)
                correction = repaired["contents"][-1]["parts"][0]["text"]
                self.assertIn("catalog_candidates, transcript, intent and confidence", correction)
                self.assertNotIn("private_arbitrary_key", correction)
                self.assertNotIn("customer secret", correction)
                self.assertLess(len(correction), 2500)

    def test_unknown_root_keys_and_bracket_commands_fail_with_specific_codes(self):
        for parsed, code in (
            ({"reply_text": "Дякую.", "controls": [], "private_arbitrary_key": "secret"}, "malformed_payload"),
            ({"reply_text": "Дякую. [MANAGER]", "controls": []}, "control_token_in_reply_text"),
        ):
            with self.subTest(code=code):
                result = self.guard().validate(parsed)
                self.assertEqual(result.reason_codes, ("invalid_response_schema", "schema_" + code))
                self.assertNotIn("private_arbitrary_key", ",".join(result.reason_codes))

    def test_image_schema_requires_fields_that_the_parser_already_requires(self):
        schema = structured_response_schema()
        observation_schema = schema["properties"]["turn_intelligence"]["properties"]["image_observations"]["items"]
        self.assertEqual(set(observation_schema["required"]), {"source_image_index", "outcome", "evidence_code", "type_code"})
        parsed = {
            "reply_text": "Бачу зображення.", "controls": [],
            "turn_intelligence": {
                "catalog_candidates": [], "transcript": "", "intent": "media_review", "confidence": 0.8,
                "image_observations": [{"source_image_index": 0, "outcome": "understood", "evidence_code": "visual_content", "type_code": "other"}],
            },
        }
        guard = self.guard(image_mimes=("image/png",), require_intelligence=True)
        self.assertTrue(guard.validate(parsed, usage={"_request_inline_count": 1}).valid)
        observation = parsed["turn_intelligence"]["image_observations"][0]
        for field in ("evidence_code", "type_code"):
            with self.subTest(field=field):
                saved = observation.pop(field)
                result = guard.validate(parsed, usage={"_request_inline_count": 1})
                self.assertEqual(result.reason_codes, ("invalid_response_schema", "schema_invalid_turn_intelligence"))
                self.assertIsNone(guard.response)
                observation[field] = saved

    def test_repair_does_not_turn_missing_media_authority_into_another_attempt(self):
        guard = self.guard(image_mimes=("image/png",), expected_content_hashes=("a" * 64,))
        result = guard.validate({"reply_text": "Дякую.", "controls": []}, usage={
            "_request_inline_count": 1, "_request_inline_content_hashes": ["b" * 64],
        })
        self.assertEqual(result.reason_codes, ("actual_media_binding_mismatch",))
        self.assertIsNone(guard.repair({"contents": []}, {}, result.reason_codes))

    def test_mime_only_contract_explains_action_free_shape_and_strict_values(self):
        instruction = structured_response_instruction()
        for text in ('"controls":[]', "only JSON true", "never use false, null", "lead_manager", "paylink accepts only full or prepay", "all four fields are required", "Never put bracket commands", "Do not repeat a kind"):
            with self.subTest(text=text):
                self.assertIn(text, instruction)

    def test_price_instruction_separates_reporting_from_fresh_selection(self):
        instruction = structured_response_instruction()
        for text in ('exact supplied normalized configuration price',
                     'a quote does not require selection controls',
                     'fresh source-backed choice explicitly authorized by the response plan',
                     'requires product and its required configuration fields',
                     'Do not reapply already-canonical choices',
                     'manager:true only when authorized', 'nor proves availability'):
            with self.subTest(text=text):
                self.assertIn(text, instruction)
        self.assertNotIn('include product plus every known', instruction)

    def test_independent_known_price_and_pending_review_need_no_selection_controls(self):
        from decimal import Decimal
        context = ReplyTruthContext(authorized_prices=(Decimal('900.00'),),
            allowed_sizes=('XXL',), source_chosen_sizes=('XXL',),
            allowed_fits=('classic',), allowed_colors=('black',))
        guard = ProviderResponseGuard(context_factory=lambda _controls, _text: context)
        candidate = {'reply_text': 'The hoodie costs 900 UAH. I will check availability in size XXL with our manager.',
            'controls': [{'kind': 'price_quoted', 'value': '900.00'}, {'kind': 'manager', 'value': True}]}
        original = deepcopy(candidate)
        self.assertTrue(guard.validate(candidate).valid, guard.last_reasons)
        self.assertEqual(set(guard.response.control), {'price_quoted', 'manager'})
        self.assertEqual(Decimal(guard.response.control['price_quoted']), Decimal('900.00'))
        self.assertEqual(candidate, original)
        wrong_price = {**candidate, 'reply_text': candidate['reply_text'].replace('900', '1000')}
        self.assertIn('unverified_price', guard.validate(wrong_price).reason_codes)
        fresh_selection = {'reply_text': 'You selected this hoodie in size XXL, classic fit, black, quantity 1.',
            'controls': [{'kind': 'product', 'value': 7}, {'kind': 'size', 'value': 'XXL'},
                {'kind': 'fit', 'value': 'classic'}, {'kind': 'color_variant_id', 'value': 8},
                {'kind': 'qty', 'value': 1}]}
        self.assertTrue(guard.validate(fresh_selection).valid, guard.last_reasons)
        self.assertEqual(guard.response.control,
            {'product': '7', 'size': 'XXL', 'fit': 'classic', 'color_variant_id': '8', 'qty': '1'})
