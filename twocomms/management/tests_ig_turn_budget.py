"""Э2.10 — окно живости выведено из бюджета хода, а не задано независимо."""
from unittest.mock import patch

from django.test import TestCase

from management.management.commands import run_instagram_bot as runner
from management.services import ig_turn_budget


class TurnBudgetConsistencyTests(TestCase):
    """Тест согласованности: изменить один таймаут и не заметить — нельзя."""

    def test_declared_budget_is_the_sum_of_phase_maximums(self):
        phases = ig_turn_budget.turn_phases()
        self.assertEqual(
            ig_turn_budget.declared_turn_budget_seconds(),
            sum(phase.max_seconds for phase in phases),
        )

    def test_nonblocking_presence_is_not_a_synchronous_turn_phase(self):
        self.assertNotIn("typing_pause", {phase.name for phase in ig_turn_budget.turn_phases()})

    def test_every_phase_has_a_positive_maximum_and_a_note(self):
        for phase in ig_turn_budget.turn_phases():
            self.assertGreater(phase.max_seconds, 0, phase.name)
            self.assertTrue(phase.note, f"фаза {phase.name} без объяснения")

    def test_heartbeat_window_strictly_exceeds_the_turn_budget(self):
        """Ровно то соотношение, которое было нарушено: 45 с при бюджете ~116 с."""
        self.assertGreater(
            ig_turn_budget.heartbeat_alive_window_seconds(),
            ig_turn_budget.declared_turn_budget_seconds(),
            "штатный долгий ход не должен выглядеть смертью демона",
        )

    def test_heartbeat_window_includes_the_safety_margin(self):
        self.assertGreaterEqual(
            ig_turn_budget.heartbeat_alive_window_seconds()
            - ig_turn_budget.declared_turn_budget_seconds(),
            ig_turn_budget.HEARTBEAT_SAFETY_MARGIN_SECONDS - 1,
        )

    def test_generation_phase_uses_the_complex_deadline_not_the_ordinary_one(self):
        from management.services.call_ai_analysis import (
            CHAT_COMPLEX_DEADLINE_SECONDS,
        )

        generation = next(
            phase for phase in ig_turn_budget.turn_phases()
            if phase.name == "generation"
        )
        self.assertEqual(
            generation.max_seconds, float(CHAT_COMPLEX_DEADLINE_SECONDS),
            "бюджет должен считаться по худшему случаю, а не по обычному",
        )

    def test_delivery_phase_accounts_for_every_chunk(self):
        from management.services.ig_delivery_plan import DEFAULT_MAX_CHUNKS
        from management.services.instagram_bot import HTTP_TIMEOUT

        delivery = next(
            phase for phase in ig_turn_budget.turn_phases()
            if phase.name == "delivery"
        )
        self.assertEqual(
            delivery.max_seconds, float(DEFAULT_MAX_CHUNKS) * float(HTTP_TIMEOUT),
            "отправка из четырёх чанков не может считаться как один запрос",
        )

    def test_budget_report_is_operator_readable(self):
        report = ig_turn_budget.budget_report()
        self.assertIn("declared_budget_seconds", report)
        self.assertIn("heartbeat_alive_window_seconds", report)
        self.assertEqual(len(report["phases"]), len(ig_turn_budget.turn_phases()))


class HeartbeatWindowWiringTests(TestCase):
    """Демон обязан читать выведенное окно, а не своё число."""

    def test_daemon_window_matches_the_derived_value(self):
        self.assertEqual(
            runner.HB_ALIVE_WINDOW,
            ig_turn_budget.heartbeat_alive_window_seconds(),
        )

    def test_daemon_window_is_larger_than_the_old_hardcoded_45(self):
        self.assertGreater(
            runner.HB_ALIVE_WINDOW, 45,
            "45 с было причиной ложных restart посреди штатного хода",
        )

    def test_pulse_interval_stays_far_below_the_window(self):
        """Один пропущенный тик пульса не должен выглядеть смертью процесса."""
        self.assertLess(runner.HB_PULSE_INTERVAL * 3, runner.HB_ALIVE_WINDOW)

    def test_derivation_failure_falls_back_conservatively_not_to_45(self):
        with patch.object(
            ig_turn_budget, "heartbeat_alive_window_seconds",
            side_effect=RuntimeError("budget module broken"),
        ):
            self.assertGreater(runner._heartbeat_alive_window(), 45)


class DaemonLivenessTests(TestCase):
    """Живость процесса и зависшая работа — разные состояния."""

    def test_fresh_heartbeat_within_the_window_is_alive(self):
        import time

        with patch.object(runner, "cache") as cache:
            cache.get.return_value = {"at": time.time() - 30, "sentinel": 0}
            self.assertTrue(runner._daemon_alive())

    def test_long_running_turn_is_still_alive_within_the_budget(self):
        """Штатный долгий ход: 96 с работы, окно 136 с — restart не нужен."""
        import time

        with patch.object(runner, "cache") as cache:
            cache.get.return_value = {"at": time.time() - 96, "sentinel": 0}
            self.assertTrue(
                runner._daemon_alive(),
                "ход в пределах объявленного бюджета не является смертью",
            )

    def test_hang_beyond_the_window_is_not_alive(self):
        import time

        with patch.object(runner, "cache") as cache:
            cache.get.return_value = {
                "at": time.time() - (runner.HB_ALIVE_WINDOW + 10),
                "sentinel": 0,
            }
            self.assertFalse(runner._daemon_alive())

    def test_missing_heartbeat_is_not_alive(self):
        with patch.object(runner, "cache") as cache:
            cache.get.return_value = None
            self.assertFalse(runner._daemon_alive())

    def test_malformed_heartbeat_is_not_alive(self):
        with patch.object(runner, "cache") as cache:
            cache.get.return_value = {"at": "not-a-number"}
            self.assertFalse(runner._daemon_alive())
