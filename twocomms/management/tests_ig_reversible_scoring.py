from django.test import SimpleTestCase

from management.services.bot_sales_classifier import DEFER_RE, _resolve_readiness


class ReversibleReadinessTests(SimpleTestCase):
    def test_repeated_signal_does_not_accumulate(self):
        self.assertEqual(_resolve_readiness(32, 32), 32)
        self.assertEqual(_resolve_readiness(70, 32), 70)

    def test_negative_and_neutral_evidence_can_reduce_score(self):
        self.assertEqual(_resolve_readiness(80, 0, hard_zero=True), 0)
        self.assertEqual(_resolve_readiness(80, 25, soft_negative=True), 35)
        self.assertEqual(_resolve_readiness(40, 0), 30)

    def test_manager_reaction_and_verified_payment_have_separate_rules(self):
        self.assertEqual(_resolve_readiness(54, 0, preserve=True), 54)
        self.assertEqual(_resolve_readiness(0, 0, hard_zero=True, verified_payment=True), 100)

    def test_communication_opt_out_preserves_commercial_readiness(self):
        self.assertEqual(_resolve_readiness(82, 0, preserve=True), 82)
        self.assertEqual(_resolve_readiness(82, 0, hard_zero=True), 0)

    def test_common_ukrainian_and_russian_deferrals_are_detected(self):
        for phrase in (
            "Я подумаю",
            "Не зараз",
            "Куплю пізніше",
            "Не сейчас",
            "Позже решу",
            "Нет моего размера",
            "Немає мого кольору",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNotNone(DEFER_RE.search(phrase))
