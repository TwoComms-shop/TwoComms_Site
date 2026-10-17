from django.test import SimpleTestCase

from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth


class ReplyTruthUrlTokenTests(SimpleTestCase):
    chart = "https://twocomms.shop/media/size_grids/classic-tshirt.webp"

    def test_authorized_native_chart_path_is_not_a_configuration_claim(self):
        result = validate_reply_truth("Ось ваша розмірна сітка 🤍\n" + self.chart,
            context=ReplyTruthContext(authorized_urls=(self.chart,)))
        self.assertTrue(result.valid, result.reasons)

    def test_unknown_url_still_requires_its_own_authority(self):
        result = validate_reply_truth("Ось сітка: " + self.chart, context=ReplyTruthContext())
        self.assertEqual(result.reasons, ("unauthorized_url",))

    def test_url_cannot_authorize_neighboring_size_stock_or_price_claims(self):
        for claim, reason in (("Обраний розмір M.", "configuration_mismatch"),
                              ("Футболка є в наявності.", "unverified_availability"),
                              ("Ціна 900 грн.", "unverified_price")):
            with self.subTest(claim=claim):
                result = validate_reply_truth(self.chart + "\n" + claim,
                    context=ReplyTruthContext(authorized_urls=(self.chart,)))
                self.assertIn(reason, result.reasons)
