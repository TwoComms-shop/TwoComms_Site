import datetime
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from management.models import GeminiKeyState, LeadCheckerSettings


class GeminiKeyStateModelTests(TestCase):
    def test_get_creates_row(self):
        st = GeminiKeyState.get("GEMINI_API")
        self.assertEqual(st.key_name, "GEMINI_API")
        self.assertIsNone(st.cooldown_until)
        self.assertEqual(st.requests_today, 0)

    def test_get_is_idempotent(self):
        a = GeminiKeyState.get("GEMINI_API2")
        a.requests_today = 5
        a.save()
        b = GeminiKeyState.get("GEMINI_API2")
        self.assertEqual(b.requests_today, 5)
        self.assertEqual(GeminiKeyState.objects.filter(key_name="GEMINI_API2").count(), 1)


class LeadCheckerSettingsAutoRecheckTests(TestCase):
    def test_auto_recheck_defaults(self):
        s = LeadCheckerSettings.load()
        self.assertFalse(s.auto_recheck)
        self.assertEqual(s.auto_recheck_batch, 25)


ENV6 = {f"GEMINI_API{n}": f"key-val-{n}" for n in ("", "2", "3", "4", "5", "6")}


class NextMidnightPTTests(SimpleTestCase):
    def test_returns_future_utc_midnight_pt(self):
        from management.services import gemini_keys as gk
        now = timezone.now()
        nm = gk.next_midnight_pt(now)
        self.assertGreater(nm, now)
        nm_pt = nm.astimezone(gk.PT)
        self.assertEqual((nm_pt.hour, nm_pt.minute, nm_pt.second), (0, 0, 0))


class Parse429Tests(SimpleTestCase):
    def test_topup(self):
        from management.services import gemini_keys as gk
        scope, secs = gk.parse_429('{"error":{"message":"Your prepayment credits are depleted."}}')
        self.assertEqual(scope, "topup")
        self.assertGreater(secs, 0)

    def test_retry_delay_wins_over_perday(self):
        """Реальне free-tier тіло: quotaId PerDay + retryDelay 48s → беремо retryDelay
        (короткий кулдаун), а НЕ до півночі."""
        from management.services import gemini_keys as gk
        body = ('{"error":{"code":429,"message":"You exceeded your current quota... limit: 20",'
                '"details":[{"@type":"...QuotaFailure","violations":[{"quotaId":'
                '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},'
                '{"@type":"...RetryInfo","retryDelay":"48s"}]}}')
        scope, secs = gk.parse_429(body)
        self.assertEqual(scope, "minute")
        self.assertGreaterEqual(secs, 48)
        self.assertLessEqual(secs, 60)

    def test_long_retry_delay_is_day(self):
        from management.services import gemini_keys as gk
        scope, secs = gk.parse_429('{"error":{"details":[{"@type":"RetryInfo","retryDelay":"40000s"}]}}')
        self.assertEqual(scope, "day")
        self.assertGreater(secs, 3600)

    def test_perday_without_retry_delay_is_midnight(self):
        from management.services import gemini_keys as gk
        body = '{"error":{"message":"quota","details":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}'
        scope, secs = gk.parse_429(body)
        self.assertEqual(scope, "day")
        self.assertEqual(secs, 0)

    def test_ambiguous_defaults_to_minute_not_day(self):
        from management.services import gemini_keys as gk
        scope, secs = gk.parse_429('{"error":{"message":"check your plan and billing details"}}')
        self.assertEqual(scope, "minute")


class MarkAndAvailabilityTests(TestCase):
    def test_mark_429_day_cooldown_until_midnight_pt(self):
        from management.services import gemini_keys as gk
        now = timezone.now()
        st = gk.mark_429("GEMINI_API", "day", 0, now=now, error="quota")
        self.assertEqual(st.cooldown_scope, "day")
        self.assertEqual(st.cooldown_until, gk.next_midnight_pt(now))
        self.assertFalse(gk.is_available("GEMINI_API", now))

    def test_mark_429_minute_short_cooldown(self):
        from management.services import gemini_keys as gk
        now = timezone.now()
        gk.mark_429("GEMINI_API2", "minute", 40, now=now)
        self.assertFalse(gk.is_available("GEMINI_API2", now))
        self.assertTrue(gk.is_available("GEMINI_API2", now + datetime.timedelta(seconds=41)))

    def test_mark_success_clears_and_counts(self):
        from management.services import gemini_keys as gk
        now = timezone.now()
        gk.mark_429("GEMINI_API3", "day", 0, now=now)
        st = gk.mark_success("GEMINI_API3", now=now)
        self.assertIsNone(st.cooldown_until)
        self.assertEqual(st.requests_today, 1)
        self.assertTrue(gk.is_available("GEMINI_API3", now))


class ModelOverloadTests(SimpleTestCase):
    def test_overload_cache(self):
        from management.services import gemini_keys as gk
        gk.clear_model_overload()
        now = timezone.now()
        self.assertFalse(gk.is_model_overloaded("gemini-3.5-flash", now))
        gk.mark_model_overloaded("gemini-3.5-flash", seconds=300, now=now)
        self.assertTrue(gk.is_model_overloaded("gemini-3.5-flash", now))
        self.assertFalse(gk.is_model_overloaded("gemini-3.5-flash", now + datetime.timedelta(seconds=301)))
        gk.clear_model_overload()


class IterAttemptsTests(TestCase):
    def setUp(self):
        from management.services import gemini_keys as gk
        gk.clear_model_overload()

    def test_chat_starts_with_primary_key_and_newest_model(self):
        from management.services import gemini_keys as gk
        with patch.dict("os.environ", ENV6, clear=False):
            first = next(gk.iter_attempts("chat"))
        self.assertEqual(first[0], "GEMINI_API")
        self.assertEqual(first[2], "gemini-3.6-flash")

    def test_chat_falls_to_borrow_when_own_in_cooldown(self):
        from management.services import gemini_keys as gk
        now = timezone.now()
        gk.mark_429("GEMINI_API", "day", 0, now=now)
        gk.mark_429("GEMINI_API2", "day", 0, now=now)
        with patch.dict("os.environ", ENV6, clear=False):
            keys = [a[0] for a in gk.iter_attempts("chat")]
        self.assertNotIn("GEMINI_API", keys)
        self.assertNotIn("GEMINI_API2", keys)
        self.assertEqual(keys[0], "GEMINI_API5")

    def test_overloaded_model_skipped(self):
        from management.services import gemini_keys as gk
        gk.mark_model_overloaded("gemini-3.5-flash", seconds=300)
        with patch.dict("os.environ", ENV6, clear=False):
            models_for_first_key = [a[2] for a in gk.iter_attempts("chat") if a[0] == "GEMINI_API"]
        self.assertNotIn("gemini-3.5-flash", models_for_first_key)
        self.assertIn("gemini-3.1-flash-lite", models_for_first_key)
        gk.clear_model_overload()

    def test_checker_chain_uses_25_flash(self):
        from management.services import gemini_keys as gk
        with patch.dict("os.environ", ENV6, clear=False):
            combos = list(gk.iter_attempts("checker"))
        self.assertTrue(all(k in ("GEMINI_API5", "GEMINI_API6") for k, _, _ in combos))
        self.assertIn("gemini-2.5-flash", [m for _, _, m in combos])

    def test_primary_model_tried_on_all_keys_before_lower(self):
        """Model-major: gemini-3.6-flash перебирається на ВСІХ ключах раніше за
        будь-яку нижчу модель. «Нижче 3.6 — лише крайній випадок»."""
        from management.services import gemini_keys as gk
        with patch.dict("os.environ", ENV6, clear=False):
            combos = list(gk.iter_attempts("chat"))
        models_seq = [m for _, _, m in combos]
        last_primary = max(i for i, m in enumerate(models_seq) if m == "gemini-3.6-flash")
        first_lower = min(i for i, m in enumerate(models_seq) if m != "gemini-3.6-flash")
        self.assertLess(last_primary, first_lower)
        keys_with_primary = {k for k, _, m in combos if m == "gemini-3.6-flash"}
        self.assertEqual(
            keys_with_primary,
            {"GEMINI_API", "GEMINI_API2", "GEMINI_API5", "GEMINI_API6"},
        )

    def test_primary_kept_for_other_keys_after_midpass_overload(self):
        """Frozen snapshot: 503 на 3.6 під час проходу не виключає 3.6 з решти
        ключів — спершу вичерпуємо пріоритетну модель на всіх ключах."""
        from management.services import gemini_keys as gk
        gk.clear_model_overload()
        with patch.dict("os.environ", ENV6, clear=False):
            gen = gk.iter_attempts("chat")
            first = next(gen)
            gk.mark_model_overloaded("gemini-3.6-flash", seconds=300)
            rest = list(gen)
        combos = [first] + rest
        keys_with_primary = {k for k, _, m in combos if m == "gemini-3.6-flash"}
        self.assertEqual(
            keys_with_primary,
            {"GEMINI_API", "GEMINI_API2", "GEMINI_API5", "GEMINI_API6"},
        )
        gk.clear_model_overload()


class PoolStatusTests(TestCase):
    def test_pool_status_shape(self):
        from management.services import gemini_keys as gk
        now = timezone.now()
        gk.mark_429("GEMINI_API", "day", 0, now=now)
        with patch.dict("os.environ", ENV6, clear=False):
            rows = gk.pool_status(now=now)
        by_name = {r["key_name"]: r for r in rows}
        self.assertEqual(len(rows), 6)
        self.assertFalse(by_name["GEMINI_API"]["available"])
        self.assertGreater(by_name["GEMINI_API"]["seconds_remaining"], 0)
        self.assertTrue(by_name["GEMINI_API2"]["available"])
        self.assertEqual(by_name["GEMINI_API"]["role"], "chat")


class KeyLevel429Tests(SimpleTestCase):
    def test_free_model_429_is_key_level(self):
        from management.services import gemini_keys as gk
        self.assertTrue(gk.is_key_level_429("gemini-3.5-flash", grounded=False))
        self.assertTrue(gk.is_key_level_429("gemini-3.1-flash-lite", grounded=False))

    def test_paid_model_429_is_model_level(self):
        from management.services import gemini_keys as gk
        self.assertFalse(gk.is_key_level_429("gemini-3.1-pro-preview", grounded=False))

    def test_grounding_429_key_level_only_on_25(self):
        from management.services import gemini_keys as gk
        self.assertTrue(gk.is_key_level_429("gemini-2.5-flash", grounded=True))
        self.assertFalse(gk.is_key_level_429("gemini-3.5-flash", grounded=True))


class ModelChainDegradationTests(SimpleTestCase):
    """Фаза 1: цепочки моделей — лише безкоштовні, з деградацією до меншої моделі.
    Платні моделі (pro) НЕ в цепочці (free-tier ключі не можуть їх юзати → марна трата)."""

    def test_management_degrades_to_smaller_free_models_without_paid(self):
        from management.services import gemini_keys as gk
        chain = gk.role_model_chains()["management"]
        self.assertEqual(chain[0], "gemini-3.5-flash")
        self.assertIn("gemini-2.5-flash", chain)
        self.assertIn("gemini-2.5-flash-lite", chain)
        self.assertNotIn("gemini-3.1-pro-preview", chain)
        self.assertNotIn("gemini-3-pro-preview", chain)
        self.assertNotIn("gemini-2.5-pro", chain)
        for m in chain:
            self.assertIn(m, gk.FREE_QUOTA_MODELS)

    def test_chat_chain_is_free_and_degrading(self):
        from management.services import gemini_keys as gk
        chain = gk.role_model_chains()["chat"]
        self.assertEqual(chain[0], "gemini-3.6-flash")
        self.assertIn("gemini-2.5-flash-lite", chain)
        for m in chain:
            self.assertIn(m, gk.FREE_QUOTA_MODELS)

    def test_chat_model_allowlist_normalizes_legacy_or_arbitrary_values(self):
        from management.services import gemini_keys as gk

        self.assertTrue(gk.is_allowed_chat_model("gemini-3.6-flash"))
        self.assertFalse(gk.is_allowed_chat_model("https://attacker.invalid/model"))
        self.assertEqual(gk.normalize_chat_model("gemini-3-flash-preview"), "gemini-3.6-flash")
        self.assertEqual(
            gk.model_chain("chat", "gemini-2.5-flash")[0],
            "gemini-2.5-flash",
        )

    def test_checker_chain_uses_only_free_grounding_models(self):
        from management.services import gemini_keys as gk
        chain = gk.role_model_chains()["checker"]
        self.assertTrue(chain)
        for m in chain:
            self.assertIn(m, gk.FREE_GROUNDING_MODELS)
        self.assertNotIn("gemini-3.5-flash", chain)


class ModelUnavailableSkipTests(TestCase):
    """Фаза 1: модель, що повернула 429-платно / 404 — позначається недоступною й
    одразу пропускається (не б'ємо її на решті ключів / у наступних викликах)."""

    def setUp(self):
        from management.services import gemini_keys as gk
        gk.clear_model_unavailable()

    def tearDown(self):
        from management.services import gemini_keys as gk
        gk.clear_model_unavailable()

    def test_mark_and_is_unavailable_with_ttl(self):
        from management.services import gemini_keys as gk
        now = timezone.now()
        self.assertFalse(gk.is_model_unavailable("gemini-3.1-pro-preview", now))
        gk.mark_model_unavailable("gemini-3.1-pro-preview", seconds=600, now=now)
        self.assertTrue(gk.is_model_unavailable("gemini-3.1-pro-preview", now))
        self.assertFalse(
            gk.is_model_unavailable("gemini-3.1-pro-preview", now + datetime.timedelta(seconds=601))
        )

    def test_iter_attempts_skips_unavailable_model_entirely(self):
        from management.services import gemini_keys as gk
        gk.mark_model_unavailable("gemini-3.5-flash", seconds=600)
        with patch.dict("os.environ", ENV6, clear=False):
            combos = list(gk.iter_attempts("chat"))
        self.assertNotIn("gemini-3.5-flash", [m for _, _, m in combos])
        # нижча модель усе ще доступна
        self.assertIn("gemini-2.5-flash", [m for _, _, m in combos])

    def test_iter_attempts_stops_model_when_marked_unavailable_midpass(self):
        from management.services import gemini_keys as gk
        with patch.dict("os.environ", ENV6, clear=False):
            gen = gk.iter_attempts("chat")
            first = next(gen)
            self.assertEqual(first[2], "gemini-3.6-flash")
            # імітуємо: на 1-му ключі модель виявилась платною → позначили
            gk.mark_model_unavailable("gemini-3.6-flash", seconds=600)
            rest = list(gen)
        # після позначення 3.5-flash більше не пробується на інших ключах
        self.assertEqual([(k, m) for k, _, m in rest if m == "gemini-3.6-flash"], [])
