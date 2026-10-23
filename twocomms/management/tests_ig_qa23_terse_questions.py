"""Ordinary fragmented questions retain source-bound factual obligations."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib

from django.test import SimpleTestCase

from management.services.approved_public_facts import approved_base_assertion
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from management.services.ig_response_control import ValidatedResponse
from management.services.ig_response_plan import _requested_topics, build_response_plan


class TerseQuestionCoverageTests(SimpleTestCase):
    readiness = {"has_product": True, "product": {"id": 55, "title": "Selected print", "kind": "tshirts"},
                 "fit": {"selected": "regular"}, "missing": []}

    def plan(self, *texts):
        sources = [{"message_id": index + 8, "role": "user", "text": text}
                   for index, text in enumerate(texts)]
        plan = build_response_plan(preferences={"values": {"size": "L"},
            "evidence": {"size": {"source_message_id": 8,
                "source_digest": hashlib.sha256(texts[0].encode()).hexdigest()}}},
            readiness=self.readiness, context=ReplyTruthContext(authorized_prices=(Decimal("600"),)),
            sources=sources)
        return replace(plan, readiness_snapshot=deepcopy(self.readiness)), sources

    def test_care_material_and_price_survive_same_and_separate_sources(self):
        for question, topic in (("як прати", "care"), ("как стирать", "care"),
                ("яка тканина", "material"), ("какая ткань", "material"),
                ("а состав", "material"), ("матеріал", "material"),
                ("догляд", "care"), ("а цена", "price"), ("ціна", "price")):
            for texts in (("L " + question,), ("L", question)):
                with self.subTest(texts=texts):
                    plan, sources = self.plan(*texts)
                    expected = f"{sources[-1]['message_id']}:info:{topic}"
                    self.assertIn(expected, [row["id"] for row in plan.obligations])
                    coverage = plan.coverage(ValidatedResponse("Ви обрали розмір L."))
                    self.assertIn(expected, coverage["remaining"])
                    self.assertEqual(coverage["disposition"], "recovery")

    def test_three_fragments_keep_both_independent_questions(self):
        plan, _sources = self.plan("L", "як прати", "ціна")
        remaining = plan.coverage(ValidatedResponse("Ви обрали розмір L. Ціна 600 грн."))["remaining"]
        self.assertEqual(remaining, ["9:info:care"])

    def test_actual_approved_care_answer_covers_terse_request(self):
        for texts in (("L як прати",), ("L", "як прати")):
            plan, sources = self.plan(*texts)
            question = sources[-1]["text"]
            assertion = approved_base_assertion(self.readiness, question=question, topic="care")
            self.assertIsNotNone(assertion)
            information = {"resolved_information": [{"product_id": 55, "question": question,
                "resolved_topic": "care", "assertion": assertion}]}
            plan = replace(plan, product_information=information)
            reply = ValidatedResponse("Ви обрали розмір L. " + assertion["text"])
            self.assertEqual(plan.coverage(reply)["disposition"], "complete")
            self.assertTrue(validate_reply_truth(reply.reply_text,
                context=plan.truth_context(ReplyTruthContext())).valid)
            unproved = replace(plan, product_information={})
            self.assertIn(f"{sources[-1]['message_id']}:info:care", unproved.coverage(reply)["remaining"])

    def test_quoted_negated_and_title_nouns_do_not_create_requests(self):
        for text in ("Не підкажіть матеріал", "Не подскажите состав", "Не цікавить ціна",
                "Не интересует состав", "Don't tell me the price", "Склад не цікавить",
                "L не хочу знати як прати", "L як прати не цікавить", "L не важно как стирать",
                'Хочу худі Material', 'Хочу модель Care', 'Хочу принт Cotton',
                'L «як прати»', 'L "какая ткань"', 'Принт «Матеріал»',
                "Склад: 95% бавовни", "Ціна була 600 грн", "This material is cotton"):
            with self.subTest(text=text):
                self.assertEqual(_requested_topics(text), [])
        self.assertEqual(_requested_topics("Не цікавить ціна але як прати"), ["info:care"])
        self.assertEqual(_requested_topics("Чи не тріскається принт"), ["info:quality"])

    def test_fabric_density_and_garment_mass_remain_distinct_from_body_weight(self):
        self.assertEqual(_requested_topics("яка щільність тканини"), ["info:density", "info:material"])
        self.assertEqual(_requested_topics("яка вага худі"), ["info:weight"])
        self.assertNotIn("info:weight", _requested_topics("моя вага 80 кг який розмір худі"))
        self.assertNotIn("info:weight", _requested_topics("яка щільність тканини 200 г/м²"))
