"""Source requirement extraction never supplies fulfilment authority."""
from datetime import datetime, timezone
import json

from django.test import SimpleTestCase

from management.services.ig_fulfillment_requirements import extract_requirements


class FulfillmentRequirementExtractionTests(SimpleTestCase):
    def extract(self, text, **kwargs):
        return extract_requirements(text, provider_timestamp=datetime(2026, 10, 12, 9, tzinfo=timezone.utc), **kwargs)

    def test_full_dates_and_gift_recipient_are_customer_hints(self):
        for text in ("Нужен подарок для друга к 20.10.2026", "Потрібен подарунок для друга до 20 жовтня 2026", "I need a gift for my friend by October 20, 2026", "Delivery by 2026-10-20"):
            with self.subTest(text=text):
                result = self.extract(text)
                self.assertEqual(result["deadline_date"], "2026-10-20")
                self.assertEqual(result["date_status"], "resolved")
                self.assertTrue(result["request_detected"])
                self.assertNotIn("arrival_guaranteed", result)
                json.dumps(result)
        self.assertEqual(self.extract("Подарунок для мами до 20.10.2026")["recipient_hint"], "mother")

    def test_incomplete_ambiguous_and_invalid_dates_are_not_rolled_forward(self):
        for text in ("Нужен подарок 10 числа", "Успеем к 10/11?", "Доставка до 10 ноября", "Gift by 31.02.2026", "Gift by 10/11/2026", "Подарунок до 20.10.2026 або 21.10.2026"):
            with self.subTest(text=text):
                result = self.extract(text)
                self.assertEqual(result["date_status"], "needs_clarification")
                self.assertIsNone(result["deadline_date"])
        result = self.extract("Delivery by 01.10.2026")
        self.assertEqual(result["date_status"], "past")
        self.assertEqual(result["deadline_date"], "2026-10-01")

    def test_relative_day_uses_provider_kyiv_day_despite_delayed_ingestion(self):
        provider = datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)
        now = datetime(2026, 10, 16, 9, tzinfo=timezone.utc)
        result = extract_requirements("Нужен подарок завтра", provider_timestamp=provider, now=now)
        self.assertEqual(result["deadline_date"], "2026-10-14")
        self.assertEqual(result["date_status"], "past")
        self.assertEqual(result["timezone"], "Europe/Kyiv")
        self.assertEqual(result["time_basis"], "provider")
        for word in ("післязавтра", "послезавтра", "day after tomorrow"):
            self.assertEqual(extract_requirements("Delivery " + word, provider_timestamp=provider)["deadline_date"], "2026-10-15")

    def test_relative_day_without_aware_provider_never_borrows_now(self):
        for provider in (None, datetime(2026, 10, 12)):
            result = extract_requirements("Delivery tomorrow", provider_timestamp=provider,
                now=datetime(2026, 10, 12, tzinfo=timezone.utc))
            self.assertIsNone(result["deadline_date"])
            self.assertEqual(result["date_status"], "needs_clarification")

    def test_urgency_and_mixed_arrival_dispatch_are_separate(self):
        for text in ("Срочно", "Терміново", "Urgent, please"):
            self.assertIs(self.extract(text)["urgency"], True)
            self.assertTrue(self.extract(text)["request_detected"])
        for text in ("Не срочно", "Не терміново", "Not urgent"):
            self.assertIs(self.extract(text)["urgency"], False)
        self.assertEqual(self.extract("Відправка до 20.10.2026")["intent"], "dispatch")
        self.assertEqual(self.extract("Will it arrive by 2026-10-20?")["intent"], "arrival")
        self.assertEqual(self.extract("When will you dispatch and will it arrive by 2026-10-20?")["intent"], "mixed")

    def test_quotes_negation_and_hypothetical_do_not_create_requests(self):
        for text in ('Он сказал "Нужен подарок до 20.10.2026, срочно"', "Якщо буде подарунок до 20.10.2026", "If I needed delivery tomorrow", "Не потрібен подарунок до 20.10.2026", "Не срочно, не доставка до 20.10.2026"):
            with self.subTest(text=text):
                self.assertFalse(self.extract(text)["request_detected"])
        actual = self.extract('"Срочно завтра" написано в рекламе. Нужен подарок к 21.10.2026')
        self.assertEqual(actual["deadline_date"], "2026-10-21")

    def test_changed_date_and_withdrawal_are_supersession_hints(self):
        result = self.extract("Не 20.10.2026, а подарок к 23.10.2026")
        self.assertEqual(result["supersession_hint"], "changed")
        self.assertEqual(result["deadline_date"], "2026-10-23")
        result = self.extract("Тепер доставка до 24.10.2026")
        self.assertEqual(result["supersession_hint"], "changed")
        for text in ("Дата неважна", "Скасуйте дедлайн", "Deadline no longer matters", "Не нужно к 10.11.2026"):
            self.assertEqual(self.extract(text)["supersession_hint"], "withdrawn")

    def test_together_split_are_only_preferences_and_conflicts_abstain(self):
        self.assertEqual(self.extract("Всё вместе позже")["delivery_preference"], "together")
        self.assertEqual(self.extract("S та M окремо, L пізніше")["delivery_preference"], "split")
        self.assertEqual(self.extract("Split or all together?")["delivery_preference"], "unknown")
        self.assertEqual(self.extract("Не отдельно")["delivery_preference"], "unknown")
        self.assertEqual(self.extract("For my mother and for my friend")["recipient_hint"], "")

    def test_detached_finite_schema_and_input_bound(self):
        first = self.extract("Urgent")
        first["reasons"].append("changed")
        self.assertNotIn("changed", self.extract("Urgent")["reasons"])
        bounded = self.extract("x" * 8001 + " срочно")
        self.assertFalse(bounded["request_detected"])
        self.assertEqual(len(bounded["raw_quote"]), 500)
        self.assertEqual(bounded["reasons"], ["input_bound"])

    def test_custom_print_and_date_are_distinct_unconfirmed_hints(self):
        result = self.extract("My own print, delivery by 2026-10-20")
        self.assertTrue(result["custom_print_hint"])
        self.assertEqual(result["intent"], "arrival")
        self.assertNotIn("production_days", result)
        self.assertFalse(self.extract("Not custom print")["custom_print_hint"])

    def test_future_provider_event_cannot_supply_relative_date(self):
        result = extract_requirements("Delivery tomorrow",
            provider_timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
            now=datetime(2026, 10, 12, tzinfo=timezone.utc))
        self.assertEqual(result["date_status"], "needs_clarification")
        self.assertIsNone(result["deadline_date"])

    def test_polite_english_deadline_question_is_a_real_requirement(self):
        for text in ("Would it arrive by 20 October 2026?", "Would you dispatch it by 2026-10-20?"):
            with self.subTest(text=text):
                result = self.extract(text)
                self.assertTrue(result["request_detected"], result)
                self.assertEqual(result["deadline_date"], "2026-10-20")

    def test_polite_question_does_not_promote_if_or_quoted_hypotheses(self):
        for text in ('If I ordered, would it arrive by 20 October 2026?',
                'He said "Would it arrive by 20 October 2026?"'):
            with self.subTest(text=text):
                result = self.extract(text)
                self.assertFalse(result["request_detected"], result)
                self.assertIsNone(result["deadline_date"])
