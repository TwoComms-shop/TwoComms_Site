from unittest.mock import patch

from django.test import TestCase

from management.services import call_ai_analysis as caa
from management.services import gemini_keys as gk

ENV6 = {f"GEMINI_API{n}": f"key-val-{n or '1'}" for n in ("", "2", "3", "4", "5", "6")}


class GeminiGroundedPoolTests(TestCase):
    def setUp(self):
        gk.clear_model_overload()

    def test_grounded_skips_gen3_and_uses_25_flash(self):
        """grounded на gen-3 → 429 (не free) → model_skip, успіх на 2.5-flash, ключ НЕ в кулдауні."""
        def fake(model, payload, key, *, parse=True, timeout=None):
            if model == "gemini-2.5-flash":
                return ({"overall_score": 80}, {"totalTokenCount": 50})
            raise caa._Gemini429("quota plan and billing")

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            out = caa.gemini_generate_grounded("SYS", "USER")

        self.assertEqual(out["parsed"], {"overall_score": 80})
        self.assertEqual(out["model"], "gemini-2.5-flash")
        # Ключ checker-пулу НЕ повинен піти в кулдаун через grounding-429 на gen-3.
        self.assertTrue(gk.is_available("GEMINI_API5"))

    def test_grounded_payload_has_google_search_no_json_mime(self):
        captured = {}

        def fake(model, payload, key, *, parse=True, timeout=None):
            captured["payload"] = payload
            return ({"ok": True}, {})

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            caa.gemini_generate_grounded("SYS", "USER")

        self.assertEqual(captured["payload"]["tools"], [{"google_search": {}}])
        self.assertNotIn("responseMimeType", captured["payload"]["generationConfig"])

    def test_manual_key_tried_first(self):
        seen_keys = []

        def fake(model, payload, key, *, parse=True, timeout=None):
            seen_keys.append(key)
            if model == "gemini-2.5-flash":
                return ({"x": 1}, {})
            raise caa._Gemini429("billing")

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            out = caa.gemini_generate_grounded("S", "U", api_key="manual-key")

        self.assertEqual(out["parsed"], {"x": 1})
        self.assertEqual(seen_keys[0], "manual-key")
        self.assertEqual(out["meta"]["key"], "(manual)")

    def test_400_raises(self):
        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=caa._GeminiFatal("HTTP 400")):
            with self.assertRaises(caa.CallAIAnalysisError):
                caa.gemini_generate_grounded("S", "U")


class GeminiJsonPoolTests(TestCase):
    def setUp(self):
        gk.clear_model_overload()

    def test_free_model_429_cools_key_and_moves_to_next(self):
        """429 на free-моделі (3.5-flash, non-grounded) = вичерпана квота ПРОЕКТУ →
        кулдаун ключа, перехід на наступний ключ пулу."""
        def fake(model, payload, key, *, parse=True, timeout=None):
            if key == "key-val-3":
                raise caa._Gemini429("PerDay quota exceeded, check your plan and billing")
            return ({"ok": True}, {})

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            out = caa.gemini_generate_json("SYS", "USER", role="management")

        self.assertEqual(out["parsed"], {"ok": True})
        self.assertEqual(out["meta"]["key"], "GEMINI_API4")
        self.assertFalse(gk.is_available("GEMINI_API3"))  # ключ у кулдауні
        self.assertTrue(gk.is_available("GEMINI_API4"))

    def test_503_falls_back_to_next_model_same_key(self):
        def fake(model, payload, key, *, parse=True, timeout=None):
            if model == "gemini-3.5-flash":
                raise caa._GeminiTransient("HTTP 503")
            return ({"ok": True}, {})

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake), \
             patch("management.services.call_ai_analysis.time.sleep", return_value=None):
            out = caa.gemini_generate_json("S", "U", role="management")

        self.assertEqual(out["parsed"], {"ok": True})
        self.assertNotEqual(out["model"], "gemini-3.5-flash")
        self.assertTrue(gk.is_model_overloaded("gemini-3.5-flash"))
        gk.clear_model_overload()

    def test_all_exhausted_raises(self):
        def fake(model, payload, key, *, parse=True, timeout=None):
            raise caa._Gemini429("PerDay quota plan and billing")

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            with self.assertRaises(caa.CallAIAnalysisError):
                caa.gemini_generate_json("S", "U", role="management")


class GeminiTextPoolTests(TestCase):
    def setUp(self):
        gk.clear_model_overload()

    def test_text_mode_returns_raw_text(self):
        def fake(model, payload, key, *, parse=True, timeout=None):
            assert parse is False
            return ("Привіт! Чим допомогти?", {"totalTokenCount": 12})

        payload = {"contents": [{"role": "user", "parts": [{"text": "хай"}]}],
                   "generationConfig": {"temperature": 0.6, "maxOutputTokens": 700}}
        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            out = caa.gemini_generate_text(payload, role="chat")
        self.assertEqual(out["parsed"], "Привіт! Чим допомогти?")

    def test_chat_role_starts_with_chat_keys(self):
        seen = []

        def fake(model, payload, key, *, parse=True, timeout=None):
            seen.append((key, model))
            return ("ok-text", {})

        payload = {"contents": [{"role": "user", "parts": [{"text": "хай"}]}]}
        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            caa.gemini_generate_text(payload, role="chat")
        # перший ключ chat-пулу = GEMINI_API, перша модель = gemini-3.6-flash
        self.assertEqual(seen[0], ("key-val-1", "gemini-3.6-flash"))

    def test_chat_manual_key_first(self):
        seen = []

        def fake(model, payload, key, *, parse=True, timeout=None):
            seen.append(key)
            return ("ok", {})

        payload = {"contents": [{"role": "user", "parts": [{"text": "хай"}]}]}
        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            caa.gemini_generate_text(payload, role="chat", manual_key="bot-custom")
        self.assertEqual(seen[0], "bot-custom")

    def test_chat_borrows_reserve_on_36_when_own_exhausted(self):
        """Коли own-ключі чату (API, API2) у денному кулдауні — чат бере резерв
        усіх доступних ключів (починаючи з API3) на тій самій моделі 3.6-flash. Це пріоритет спілкування."""
        from django.utils import timezone
        now = timezone.now()
        gk.mark_429("GEMINI_API", "day", 0, now=now)
        gk.mark_429("GEMINI_API2", "day", 0, now=now)
        seen = []

        def fake(model, payload, key, *, parse=True, timeout=None):
            seen.append((key, model))
            return ("ok-text", {})

        payload = {"contents": [{"role": "user", "parts": [{"text": "хай"}]}]}
        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake):
            out = caa.gemini_generate_text(payload, role="chat")
        self.assertEqual(out["parsed"], "ok-text")
        # перший доступний — позичений management-ключ, модель найновіша gen-3
        self.assertEqual(seen[0], ("key-val-3", "gemini-3.6-flash"))


class GeminiEmptyResponseTests(TestCase):
    def setUp(self):
        gk.clear_model_overload()

    def test_empty_retries_and_does_not_mark_overloaded(self):
        """Порожня відповідь ретраїться, але НЕ метить модель глобально overloaded."""
        calls = {"n": 0}

        def fake(model, payload, key, *, parse=True, timeout=None):
            calls["n"] += 1
            raise caa._GeminiEmpty("порожня відповідь (finishReason=STOP)")

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake), \
             patch("management.services.call_ai_analysis.time.sleep", return_value=None):
            with self.assertRaises(caa.CallAIAnalysisError):
                caa.gemini_generate_grounded("S", "U")
        # checker attempts=2 → кожна 2.5-flash комбінація пробується двічі.
        self.assertFalse(gk.is_model_overloaded("gemini-2.5-flash"))
        self.assertGreater(calls["n"], 2)  # було кілька ретраїв

    def test_empty_then_success_on_retry(self):
        seq = iter([caa._GeminiEmpty("empty"), ({"overall_score": 70}, {})])

        def fake(model, payload, key, *, parse=True, timeout=None):
            if model != "gemini-2.5-flash":
                raise caa._Gemini429("billing")  # gen-3 grounded не free
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake), \
             patch("management.services.call_ai_analysis.time.sleep", return_value=None):
            out = caa.gemini_generate_grounded("S", "U")
        self.assertEqual(out["parsed"], {"overall_score": 70})
        self.assertEqual(out["model"], "gemini-2.5-flash")


class ParseModelJsonTests(TestCase):
    def test_handles_fences_and_trailing_commas(self):
        txt = '```json\n{"a": 1, "b": [1, 2,], "c": {"x": 3,},}\n```'
        out = caa._parse_model_json(txt)
        self.assertEqual(out["a"], 1)
        self.assertEqual(out["b"], [1, 2])
        self.assertEqual(out["c"], {"x": 3})

    def test_extracts_json_with_surrounding_text(self):
        txt = 'Ось результат:\n{"overall_score": 70}\nДжерела: [1] example.com'
        out = caa._parse_model_json(txt)
        self.assertEqual(out["overall_score"], 70)

    def test_unparseable_raises(self):
        with self.assertRaises(caa.CallAIAnalysisError):
            caa._parse_model_json("зовсім не json")


class ChatTimeoutTests(TestCase):
    def setUp(self):
        gk.clear_model_overload()

    def test_chat_uses_short_timeout(self):
        """Чат не повинен висіти на завислій моделі — короткий read-таймаут."""
        captured = {}

        class FakeResp:
            status_code = 200
            text = "ok"

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "привіт"}]}}]}

        def fake_post(url, **kw):
            captured["timeout"] = kw.get("timeout")
            return FakeResp()

        with patch.dict("os.environ", ENV6, clear=False), \
             patch("management.services.call_ai_analysis.requests.post", side_effect=fake_post):
            out = caa.gemini_generate_text({"contents": []}, role="chat")
        self.assertEqual(out["parsed"], "привіт")
        self.assertEqual(captured["timeout"], caa.CHAT_TIMEOUT)
        gk.clear_model_overload()

    def test_management_keeps_long_timeout(self):
        """Аудіо-аналіз (management) лишає довгий таймаут — генерація може бути довгою."""
        captured = {}

        class FakeResp:
            status_code = 200
            text = "ok"

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": '{"a":1}'}]}}]}

        def fake_post(url, **kw):
            captured["timeout"] = kw.get("timeout")
            return FakeResp()

        with patch.dict("os.environ", ENV6, clear=False), \
             patch("management.services.call_ai_analysis.requests.post", side_effect=fake_post):
            caa.gemini_generate_json("S", "U", role="management")
        self.assertEqual(captured["timeout"], caa.GEMINI_TIMEOUT)
        gk.clear_model_overload()

    @patch.object(caa, "_run_with_pool")
    def test_management_text_calls_are_bounded_for_bot_automation(self, mock_pool):
        mock_pool.return_value = {"parsed": "ok"}

        caa.gemini_generate_text({"contents": []}, role="management")

        self.assertEqual(
            mock_pool.call_args.kwargs.get("timeout"),
            caa.MANAGEMENT_TEXT_TIMEOUT,
        )
        self.assertEqual(
            mock_pool.call_args.kwargs.get("deadline_seconds"),
            caa.MANAGEMENT_TEXT_DEADLINE_SECONDS,
        )


class ChatRoundsRetryTests(TestCase):
    def setUp(self):
        gk.clear_model_overload()

    def test_chat_cycles_three_rounds_before_error(self):
        """503 на всіх моделях → чат робить 3 круги (з backoff між ними), тільки потім помилка.

        Model-major: кожен круг — повний свип пулу (усі моделі цепочки × 6 ключів),
        бо пріоритетну модель пробуємо на ВСІХ ключах. Кількість викликів =
        len(chain) × 6 ключів × max_rounds. Між кругами clear_model_overload() + backoff 2с, 4с."""
        calls = {"n": 0}

        def fake(model, payload, key, *, parse=True, timeout=None):
            calls["n"] += 1
            raise caa._GeminiTransient("HTTP 503")

        sleeps = []
        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake), \
             patch("management.services.call_ai_analysis.time.sleep", side_effect=lambda s: sleeps.append(s)):
            with self.assertRaises(caa.CallAIAnalysisError):
                caa.gemini_generate_text({"contents": []}, role="chat")
        # усі моделі цепочки × шість ключів (own API/API2 + усі borrow) × 3 круги
        n_keys = 6
        expected = len(gk.role_model_chains()["chat"]) * n_keys * gk.max_rounds("chat")
        self.assertEqual(calls["n"], expected)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(sleeps, [2.0, 4.0])
        gk.clear_model_overload()

    def test_chat_succeeds_on_second_round(self):
        state = {"round1_done": False, "n": 0}

        def fake(model, payload, key, *, parse=True, timeout=None):
            state["n"] += 1
            if state["n"] <= 3:  # перший круг — усі 503
                raise caa._GeminiTransient("HTTP 503")
            return ("відповідь користувачу", {})

        with patch.dict("os.environ", ENV6, clear=False), \
             patch.object(caa, "_gemini_call_once", side_effect=fake), \
             patch("management.services.call_ai_analysis.time.sleep", return_value=None):
            out = caa.gemini_generate_text({"contents": []}, role="chat")
        self.assertEqual(out["parsed"], "відповідь користувачу")
        gk.clear_model_overload()

    def test_chat_one_attempt_per_combo(self):
        """attempts_per_model['chat']==1 — на 503 НЕ ретраїть ту саму комбінацію всередині круга."""
        self.assertEqual(gk.attempts_per_model("chat"), 1)
        self.assertEqual(gk.max_rounds("chat"), 3)


class StickyKeyOrderTests(TestCase):
    def test_recent_ok_key_goes_first_within_tier(self):
        from django.utils import timezone
        # API2 успішно відповів нещодавно → у тирі own він має йти першим
        gk.mark_success("GEMINI_API2", now=timezone.now())
        with patch.dict("os.environ", ENV6, clear=False):
            order = [k for k, _, _ in gk.iter_attempts("chat")]
        # перший own-ключ — GEMINI_API2 (sticky), borrow-ключі — після own
        own_keys = [k for k in order if k in ("GEMINI_API", "GEMINI_API2")]
        self.assertEqual(own_keys[0], "GEMINI_API2")
