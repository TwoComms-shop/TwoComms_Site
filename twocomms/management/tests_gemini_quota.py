"""ЭБ.4 — квота free-tier принадлежит паре (проект, модель), и её надо считать.

Замер из консоли квот (один ключ, 29.08.2026), с которого начался этап:

    3.6 Flash        RPM 5/5    TPM 13.03K/250K   RPD 21/20   ← превышено
    3.7 Flash        RPM 3/5    TPM 20.86K/250K   RPD 27/20   ← превышено
    3.5 Flash        RPM 1/5    TPM 5/250K        RPD 1/20
    3.5 Flash Lite   RPM 1/15   TPM 5/250K        RPD 1/500

Шесть ключей — шесть отдельных проектов, то есть на пул за сутки: 120 запросов
на 3.7, 120 на 3.6, 120 на 3.5-flash и 3000 на lite. Прежняя цепочка чата
начинала с 3.7 на КАЖДОМ ходе, поэтому самый скудный бюджет тратился на самую
частую операцию и выгорал до обеда.
"""
import datetime
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from management.models import GeminiModelQuotaUsage
from management.services import gemini_keys as gk
from management.services import gemini_quota as quota


class BudgetTableTests(SimpleTestCase):
    """Числа берутся из наблюдаемой консоли, а не из документации."""

    def test_strong_models_have_the_scarce_daily_budget(self):
        for model in ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"):
            budget = quota.budget_for(model)
            self.assertEqual(budget["rpd"], 20, model)
            self.assertEqual(budget["rpm"], 5, model)

    def test_lite_is_the_only_model_with_room_for_every_reply(self):
        budget = quota.budget_for("gemini-3.5-flash-lite")
        self.assertEqual(budget["rpd"], 500)
        self.assertEqual(budget["rpm"], 15)
        self.assertGreater(
            budget["rpd"] * 6,
            25 * 8,
            "25 клиентов по 8 реплик обязаны укладываться в дневной бюджет",
        )

    def test_unknown_model_is_not_silently_limited(self):
        self.assertEqual(quota.budget_for("gemini-unknown"), {})

    @override_settings(GEMINI_MODEL_BUDGETS={"gemini-3.7-flash": {"rpd": 999}})
    def test_settings_override_wins_without_losing_the_rest(self):
        budget = quota.budget_for("gemini-3.7-flash")
        self.assertEqual(budget["rpd"], 999)
        self.assertEqual(budget["rpm"], 5, "неупомянутые поля остаются")


class TaskRoutingTests(SimpleTestCase):
    """Выбор модели — средство изоляции потребителей друг от друга."""

    @override_settings(GEMINI_TASK_TIERS={})
    def test_journey_trace_keeps_existing_analysis_tier_and_fallback_chain(self):
        self.assertEqual(quota.DEFAULT_TASK_TIERS["journey_trace_reconstruction"], quota.TIER_ANALYSIS)
        self.assertEqual(
            gk.task_model_chain("management", "journey_trace_reconstruction"),
            gk.task_model_chain("management", "conversation_reanalysis"),
        )

    def test_ordinary_customer_reply_runs_on_the_loose_quota_model(self):
        chain = quota.chain_for_task("customer_chat", role="chat")
        self.assertEqual(chain[0], "gemini-3.5-flash-lite")

    def test_decisions_get_the_best_model(self):
        for task in (
            "product_decision", "size_fit_decision", "catalog_match",
            "payment_decision", "order_decision", "media_analysis",
        ):
            self.assertEqual(
                quota.chain_for_task(task, role="chat")[0], "gemini-3.7-flash", task
            )

    def test_analysis_has_its_own_tier_and_cannot_starve_replies(self):
        analysis = quota.chain_for_task("customer_intelligence", role="management")
        replies = quota.chain_for_task("customer_chat", role="chat")
        self.assertEqual(analysis[0], "gemini-3.6-flash")
        self.assertNotEqual(
            analysis[0], replies[0], "тиры обязаны опираться на разные квоты"
        )

    def test_unknown_task_never_lands_on_the_scarcest_model(self):
        """Цена ошибки в имени задачи — выгоревший за полдня бюджет 3.7."""
        self.assertEqual(
            quota.chain_for_task("brand_new_task", role="chat")[0],
            "gemini-3.5-flash-lite",
        )
        self.assertEqual(
            quota.chain_for_task("brand_new_task", role="management")[0],
            "gemini-3.6-flash",
        )

    def test_grounded_checker_chain_stays_on_25(self):
        chain = quota.chain_for_task("lead_grounding", role="checker")
        self.assertEqual(chain, ["gemini-2.5-flash", "gemini-2.5-flash-lite"])

    def test_operator_override_is_respected(self):
        chain = gk.task_model_chain("chat", "customer_chat", "gemini-3.7-flash")
        self.assertEqual(chain[0], "gemini-3.7-flash")

    @override_settings(GEMINI_TASK_TIERS={"customer_chat": "strong"})
    def test_tier_map_is_data_so_a_correction_is_one_setting(self):
        self.assertEqual(
            quota.chain_for_task("customer_chat", role="chat")[0], "gemini-3.7-flash"
        )


class LedgerTests(TestCase):
    """Учёт дорадчий, но обязан быть точным и атомарным."""

    def setUp(self):
        self.now = timezone.now()
        self.model = "gemini-3.7-flash"

    def _spend(self, count, *, key="GEMINI_API", model=None, now=None):
        model = model or self.model
        return [
            quota.try_reserve(key, model, now=now or self.now) for _ in range(count)
        ]

    def _spend_across_minutes(self, count, *, key="GEMINI_API", model=None):
        """Израсходовать `count` запросов так, чтобы ограничивал только RPD.

        Каждый запрос — в своей минуте: иначе первым упирается RPM (5 для сильных
        моделей), и тест про суточный бюджет проверял бы минутный.
        """
        model = model or self.model
        return [
            quota.try_reserve(
                key, model, now=self.now + datetime.timedelta(seconds=61 * index)
            )
            for index in range(count)
        ]

    def test_reservation_stops_exactly_at_the_daily_budget(self):
        granted = self._spend_across_minutes(21)

        self.assertEqual(granted.count(True), 20, "ровно RPD, не больше")
        self.assertFalse(granted[-1], "двадцать первый запрос не выдаём")
        self.assertFalse(
            quota.has_capacity(
                "GEMINI_API", self.model,
                now=self.now + datetime.timedelta(seconds=61 * 21),
            )
        )

    def test_one_key_running_out_does_not_touch_the_others(self):
        self._spend_across_minutes(20)
        later = self.now + datetime.timedelta(seconds=61 * 20)

        self.assertFalse(quota.has_capacity("GEMINI_API", self.model, now=later))
        self.assertTrue(quota.has_capacity("GEMINI_API2", self.model, now=later))

    def test_one_model_running_out_does_not_touch_the_others(self):
        """Та же ошибка, что и в ЭБ.2, но на уровне учёта, а не кулдауна."""
        self._spend_across_minutes(20)
        later = self.now + datetime.timedelta(seconds=61 * 20)

        self.assertFalse(quota.has_capacity("GEMINI_API", self.model, now=later))
        self.assertTrue(
            quota.has_capacity("GEMINI_API", "gemini-3.5-flash-lite", now=later)
        )

    def test_minute_budget_reopens_after_the_window(self):
        granted = self._spend(6)
        self.assertEqual(granted.count(True), 5, "RPM=5 для сильной модели")

        later = self.now + datetime.timedelta(seconds=61)
        self.assertTrue(quota.try_reserve("GEMINI_API", self.model, now=later))

    def test_daily_budget_survives_the_minute_window_reset(self):
        """Минута обновилась — сутки нет: RPD считается отдельно."""
        for minute in range(4):
            moment = self.now + datetime.timedelta(seconds=61 * minute)
            self._spend(5, now=moment)

        later = self.now + datetime.timedelta(seconds=61 * 4)
        self.assertFalse(
            quota.try_reserve("GEMINI_API", self.model, now=later),
            "двадцать запросов за четыре минуты — это исчерпанные сутки",
        )

    def test_day_rolls_over_at_pacific_midnight(self):
        self._spend_across_minutes(20)
        tomorrow = self.now + datetime.timedelta(days=1)

        self.assertTrue(quota.try_reserve("GEMINI_API", self.model, now=tomorrow))

    def test_tokens_are_settled_against_the_reserved_request(self):
        quota.try_reserve("GEMINI_API", self.model, now=self.now)
        quota.settle("GEMINI_API", self.model, 12_000, now=self.now)

        row = GeminiModelQuotaUsage.objects.get(
            key_name="GEMINI_API", model=self.model, day_date=quota.pacific_day(self.now)
        )
        self.assertEqual(row.requests, 1)
        self.assertEqual(row.tokens, 12_000)

    def test_pre_dispatch_cancellation_returns_request_and_minute_reservation(self):
        self.assertTrue(
            quota.try_reserve("GEMINI_API", self.model, now=self.now)
        )
        quota.cancel_reservation(
            "GEMINI_API",
            self.model,
            dispatch_at=self.now,
        )

        row = GeminiModelQuotaUsage.objects.get(
            key_name="GEMINI_API",
            model=self.model,
            day_date=quota.pacific_day(self.now),
        )
        self.assertEqual(row.requests, 0)
        self.assertEqual(row.minute_requests, 0)
        self.assertTrue(quota.has_capacity("GEMINI_API", self.model, now=self.now))

    def test_cross_midnight_settlement_uses_original_dispatch_day(self):
        dispatch_at = datetime.datetime(
            2026, 8, 31, 6, 59, 50, tzinfo=datetime.timezone.utc
        )
        completed_at = dispatch_at + datetime.timedelta(seconds=20)
        self.assertNotEqual(
            quota.pacific_day(dispatch_at),
            quota.pacific_day(completed_at),
        )
        self.assertTrue(
            quota.try_reserve("GEMINI_API", self.model, now=dispatch_at)
        )

        quota.settle(
            "GEMINI_API",
            self.model,
            321,
            now=completed_at,
            dispatch_at=dispatch_at,
        )

        original = GeminiModelQuotaUsage.objects.get(
            key_name="GEMINI_API",
            model=self.model,
            day_date=quota.pacific_day(dispatch_at),
        )
        self.assertEqual(original.tokens, 321)
        self.assertFalse(
            GeminiModelQuotaUsage.objects.filter(
                key_name="GEMINI_API",
                model=self.model,
                day_date=quota.pacific_day(completed_at),
            ).exists()
        )

    def test_unknown_model_is_never_blocked_by_the_ledger(self):
        self.assertTrue(
            all(
                quota.try_reserve("GEMINI_API", "gemini-unknown", now=self.now)
                for _ in range(50)
            )
        )

    def test_keys_are_offered_in_order_of_remaining_daily_quota(self):
        """При 20 запросах в сутки бюджет надо растягивать по проектам."""
        self._spend_across_minutes(18, key="GEMINI_API")
        self._spend_across_minutes(5, key="GEMINI_API2")
        later = self.now + datetime.timedelta(seconds=61 * 20)

        order = quota.order_keys_by_remaining(
            ["GEMINI_API", "GEMINI_API2", "GEMINI_API3"], self.model, now=later
        )

        self.assertEqual(order[0], "GEMINI_API3", "самый свободный ключ — первым")
        self.assertEqual(order[-1], "GEMINI_API", "самый израсходованный — последним")

    def test_exhausted_pair_is_skipped_before_the_provider_says_429(self):
        self._spend_across_minutes(20)
        env = {
            f"GEMINI_API{suffix}": f"key-val-{suffix or '1'}"
            for suffix in ("", "2", "3", "4", "5", "6")
        }

        with patch.dict("os.environ", env, clear=False):
            candidates = [
                (key, model)
                for key, _value, model in gk.iter_live_chat_attempts(
                    model_chain_override=[self.model]
                )
            ]

        self.assertTrue(candidates, "остальные ключи обязаны остаться кандидатами")
        self.assertNotIn(
            ("GEMINI_API", self.model),
            candidates,
            "исчерпанная пара не должна тратить ход клиента на предсказуемый 429",
        )
        self.assertIn(("GEMINI_API2", self.model), candidates)

    def test_snapshot_reports_usage_without_touching_the_provider(self):
        self._spend(3)
        quota.settle("GEMINI_API", self.model, 900, now=self.now)

        rows = [
            row for row in quota.usage_snapshot(now=self.now)
            if row["key_name"] == "GEMINI_API" and row["model"] == self.model
        ]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requests"], 3)
        self.assertEqual(rows[0]["rpd"], 20)
        self.assertEqual(rows[0]["tokens"], 900)


class HedgeAffordabilityTests(SimpleTestCase):
    """Волна оправдана только там, где запросы дешёвые."""

    def test_strong_models_are_below_the_hedging_threshold(self):
        for model in ("gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"):
            self.assertLess(
                quota.budget_for(model)["rpd"],
                quota.HEDGE_MIN_RPD,
                f"{model}: волна из трёх ключей — это 15% дневного бюджета",
            )

    def test_lite_can_afford_a_wave(self):
        self.assertGreaterEqual(
            quota.budget_for("gemini-3.5-flash-lite")["rpd"], quota.HEDGE_MIN_RPD
        )
