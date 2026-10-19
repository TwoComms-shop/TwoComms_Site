"""Own consultation questions must not silently change the customer's cut."""
from django.test import SimpleTestCase

from management.services.ig_commerce_turns import parse_turn
from management.services.ig_reply_truth import ReplyTruthContext
from management.services.ig_response_plan import build_response_plan


class ConsultationChoiceBoundaryTests(SimpleTestCase):
    def test_illustration_and_chart_requests_do_not_choose_the_depicted_cut(self):
        for text in ("Покажіть таблицю розмірів та приклади classic крою",
                     "Покажите фото regular", "Show oversize examples",
                     "Класична чи oversize?", "Хочу подивитися фото oversize"):
            with self.subTest(text=text):
                self.assertNotIn("fit", parse_turn(text).field_updates)

    def test_explicit_mixed_choice_and_chart_request_retains_actual_choice(self):
        for text, fit in (("Беру classic, покажіть сітку розмірів", "classic"),
                          ("Хочу oversize. Покажіть таблицю розмірів", "oversize"),
                          ("Choose regular, show size chart", "classic"),
                          ("Оверсайз", "oversize")):
            with self.subTest(text=text):
                self.assertEqual(parse_turn(text).field_updates.get("fit"), fit)

    def test_two_chart_request_has_two_independent_obligations(self):
        source = {"message_id": 7, "role": "user", "text": "Покажіть фасони й обидві сітки"}
        plan = build_response_plan(preferences={}, readiness={"has_product": True, "product": {"id": 3}},
            context=ReplyTruthContext(allowed_fits=("classic", "oversize")), sources=[source])
        charts = [row for row in plan.obligations if row["kind"] == "info:size_chart"]
        self.assertEqual({row["fit_code"] for row in charts}, {"classic", "oversize"})
        self.assertEqual(len({row["id"] for row in charts}), 2)
        self.assertFalse(any(row["kind"] == "fit_option_code" for row in plan.obligations))

    def test_one_current_hoodie_cut_does_not_invent_a_second_chart(self):
        plan = build_response_plan(preferences={}, readiness={"has_product": True, "product": {"id": 3}},
            context=ReplyTruthContext(allowed_fits=("oversize",)),
            sources=[{"message_id": 7, "role": "user", "text": "Покажіть обидві сітки"}])
        self.assertEqual(len([row for row in plan.obligations if row["kind"] == "info:size_chart"]), 1)
        self.assertNotIn("classic", str(plan.obligations))
