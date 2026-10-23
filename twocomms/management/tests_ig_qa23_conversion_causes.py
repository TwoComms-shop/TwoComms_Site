"""Pure regressions for the polarity of customer-reported refusal causes."""
from unittest import TestCase

from management.services.ig_conversion_audit import normalize_conversion_audit


class ConversionCausePolarityTests(TestCase):
    def normalize(self, text, code="price", *, counterevidence=None):
        sources = {1: {"role": "user", "text": text}}
        if counterevidence:
            sources[2] = {"role": "user", "text": counterevidence}
        value = {"schema_version": 1, "nonconversion_reasons": [{
            "code": code, "basis": "reported_by_customer", "confidence": "high",
            "evidence": [{"message_id": 1, "quote": text}],
            "counterevidence": ([{"message_id": 2, "quote": counterevidence}]
                                if counterevidence else []),
        }]}
        return normalize_conversion_audit(value, sources)["nonconversion_reasons"]

    def test_negated_causal_connector_cannot_borrow_positive_topic_elsewhere(self):
        for text in (
            "Не беру, не через ціну. Ціна підходить.",
            "Не буду покупать, не из-за цены. Цена подходит.",
            "I will not buy, not because of price. The price is fine.",
            "I will not buy, not due to price. The price is fine.",
        ):
            with self.subTest(text=text):
                rows = self.normalize(text)
                self.assertTrue(all(row["basis"] != "reported_by_customer" for row in rows))

    def test_ordinary_causal_refusals_remain_customer_reported(self):
        for text in (
            "Не беру, бо дорого.",
            "Не буду покупать, потому что цена высокая.",
            "I will not buy because the price is high.",
            "Ціна зависока, тому не купую.",
            "The price is high, so I will not buy.",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.normalize(text)[0]["basis"], "reported_by_customer")

    def test_negative_and_positive_reasons_in_one_refusal_keep_their_polarity(self):
        text = "Не беру не через ціну, а через розмір. Ціна підходить."
        self.assertTrue(all(row["basis"] != "reported_by_customer" for row in self.normalize(text)))
        self.assertEqual(self.normalize(text, "size_fit")[0]["basis"], "reported_by_customer")

    def test_negating_other_reason_does_not_remove_explicit_price_cause(self):
        text = "Не беру не через розмір, а через ціну."
        self.assertEqual(self.normalize(text)[0]["basis"], "reported_by_customer")

    def test_counterevidence_still_prevents_unqualified_reported_cause(self):
        rows = self.normalize("Не беру, бо дорого.", counterevidence="Ціна тепер підходить.")
        self.assertEqual((rows[0]["basis"], rows[0]["confidence"]), ("hypothesis", "low"))
        self.assertEqual(rows[0]["counterevidence"][0]["message_id"], 2)

    def test_unknown_and_concern_without_refusal_remain_noncausal(self):
        unknown = self.normalize("Дякую.", "unknown")[0]
        self.assertEqual((unknown["basis"], unknown["confidence"]), ("unknown", "low"))
        concern = self.normalize("Ціна зависока, подумаю.")[0]
        self.assertEqual((concern["basis"], concern["confidence"]), ("hypothesis", "low"))
