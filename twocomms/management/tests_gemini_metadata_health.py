import datetime
import json
import threading
import time
from concurrent.futures import Future
from io import StringIO
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from management.models import GeminiKeyState, GeminiRequestAttempt
from management.services import gemini_metadata_health


UTC = datetime.timezone.utc


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class GeminiMetadataHealthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = datetime.datetime(2026, 8, 18, 12, 5, tzinfo=UTC)

    @patch.dict("os.environ", {"GEMINI_API": "secret-value"}, clear=False)
    @patch("management.services.gemini_metadata_health.urlopen")
    def test_primary_metadata_success_skips_secondary_without_post_or_body(self, urlopen):
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps({
            "supportedGenerationMethods": ["generateContent"],
        }).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.return_value = response

        result = gemini_metadata_health.check_alias("GEMINI_API", now=self.now)

        self.assertEqual([row["status"] for row in result], ["metadata_ok", "not_needed"])
        self.assertEqual(urlopen.call_count, 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("secret-value", request.full_url)
        self.assertEqual(request.headers["X-goog-api-key"], "secret-value")
        attempts = list(GeminiRequestAttempt.objects.order_by("id"))
        self.assertEqual([row.role for row in attempts], ["health_metadata", "health_metadata"])
        self.assertEqual([row.outcome for row in attempts], ["succeeded", "skipped"])
        self.assertEqual(attempts[1].failure_kind, "not_needed")
        self.assertEqual(attempts[1].decision, "skipped_primary_ok")

    @patch.dict("os.environ", {"GEMINI_API": "secret-value"}, clear=False)
    @patch("management.services.gemini_metadata_health.urlopen")
    def test_primary_failure_checks_secondary_and_redacts_provider_response(self, urlopen):
        from urllib.error import HTTPError

        error = HTTPError("https://provider.invalid", 429, "secret provider text", {}, None)
        response = Mock()
        response.status = 200
        response.read.return_value = json.dumps({
            "supportedGenerationMethods": ["generateContent"],
        }).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        urlopen.side_effect = [error, response]

        result = gemini_metadata_health.check_alias("GEMINI_API", now=self.now)

        self.assertEqual([row["status"] for row in result], ["rate_limited", "metadata_ok"])
        self.assertEqual(urlopen.call_count, 2)
        serialized = str(result) + str(list(GeminiRequestAttempt.objects.values()))
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("secret provider text", serialized)

    @patch.dict("os.environ", {"GEMINI_API": "secret-value"}, clear=False)
    @patch("management.services.gemini_metadata_health.urlopen")
    def test_http_200_without_generate_content_checks_fallback_model(self, urlopen):
        unsupported = Mock()
        unsupported.status = 200
        unsupported.read.return_value = json.dumps({
            "supportedGenerationMethods": ["countTokens"],
        }).encode()
        unsupported.__enter__ = Mock(return_value=unsupported)
        unsupported.__exit__ = Mock(return_value=False)
        supported = Mock()
        supported.status = 200
        supported.read.return_value = json.dumps({
            "supportedGenerationMethods": ["generateContent"],
        }).encode()
        supported.__enter__ = Mock(return_value=supported)
        supported.__exit__ = Mock(return_value=False)
        urlopen.side_effect = [unsupported, supported]

        result = gemini_metadata_health.check_alias("GEMINI_API", now=self.now)

        self.assertEqual(
            [row["status"] for row in result],
            ["unsupported_generation", "metadata_ok"],
        )
        self.assertEqual(urlopen.call_count, 2)

    @patch("management.services.gemini_metadata_health.urlopen")
    def test_provider_http_503_is_distinct_from_transport_failure(self, urlopen):
        import socket
        from urllib.error import HTTPError, URLError

        urlopen.side_effect = HTTPError(
            "https://provider.invalid", 503, "provider unavailable", {}, None
        )
        provider = gemini_metadata_health._check_model(
            "GEMINI_API",
            "secret-value",
            "gemini-3.7-flash",
            "metadata-provider-503",
        )
        urlopen.side_effect = URLError("dns unavailable")
        transport = gemini_metadata_health._check_model(
            "GEMINI_API",
            "secret-value",
            "gemini-3.7-flash",
            "metadata-transport",
        )

        self.assertEqual(provider["status"], "http_5xx")
        self.assertEqual(transport["status"], "transport_error")

        urlopen.side_effect = HTTPError(
            "https://provider.invalid", 408, "request timeout", {}, None
        )
        http_timeout = gemini_metadata_health._check_model(
            "GEMINI_API",
            "secret-value",
            "gemini-3.7-flash",
            "metadata-http-408",
        )
        self.assertEqual(http_timeout["status"], "http_408")

        urlopen.side_effect = URLError(socket.timeout("read timed out"))
        wrapped_timeout = gemini_metadata_health._check_model(
            "GEMINI_API",
            "secret-value",
            "gemini-3.7-flash",
            "metadata-wrapped-timeout",
        )
        self.assertEqual(wrapped_timeout["status"], "timeout")
        self.assertEqual(
            list(
                GeminiRequestAttempt.objects.order_by("id").values_list(
                    "failure_kind", "http_code"
                )
            ),
            [
                ("http_5xx", 503),
                ("transport_error", None),
                ("http_408", 408),
                ("timeout", None),
            ],
        )

    @patch.dict("os.environ", {"GEMINI_API": ""}, clear=True)
    @patch("management.services.gemini_metadata_health.urlopen")
    def test_unconfigured_alias_does_not_call_provider(self, urlopen):
        result = gemini_metadata_health.check_alias("GEMINI_API", now=self.now)
        self.assertEqual([row["status"] for row in result], ["unconfigured", "unconfigured"])
        urlopen.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    @patch("management.management.commands.check_ig_gemini_metadata_health.gemini_metadata_health.run_hour")
    def test_metadata_command_is_explicit_manual_diagnostic(self, run_hour):
        run_hour.return_value = {
            "request_id": "metadata-20260818T12",
            "checked_aliases": 6,
            "configured_aliases": 6,
            "provider_requests": 6,
            "deadline_skipped_models": 0,
        }
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command("check_ig_gemini_metadata_health", stdout=output)
        call_command("check_ig_gemini_metadata_health", manual=True, stdout=output)
        self.assertEqual(run_hour.call_count, 1)
        self.assertIn("6 metadata requests", output.getvalue())
        self.assertIn("0 deadline skips", output.getvalue())

    def test_read_only_pool_status_does_not_create_state_rows(self):
        from management.services import gemini_keys

        self.assertEqual(GeminiKeyState.objects.count(), 0)
        rows = gemini_keys.pool_status(now=self.now, read_only=True)
        self.assertEqual(len(rows), 6)
        self.assertEqual(GeminiKeyState.objects.count(), 0)

    @patch("management.services.gemini_metadata_health.urlopen")
    def test_deadline_skip_is_not_recorded_as_provider_failure(self, urlopen):
        result = gemini_metadata_health._check_model(
            "GEMINI_API",
            "secret-value",
            "gemini-3.7-flash",
            "metadata-deadline",
            deadline=time.monotonic() - 1,
        )

        self.assertEqual(result["status"], "deadline_skipped")
        urlopen.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    @patch("management.services.gemini_metadata_health.check_alias")
    def test_hourly_aliases_use_distinct_request_ids_and_shared_deadline(self, check_alias):
        check_alias.side_effect = [
            [{"status": "metadata_ok"}, {"status": "not_needed"}],
            [{"status": "deadline_skipped"}, {"status": "deadline_skipped"}],
            [{"status": "unconfigured"}, {"status": "unconfigured"}],
            [{"status": "metadata_ok"}, {"status": "not_needed"}],
            [{"status": "metadata_ok"}, {"status": "not_needed"}],
            [{"status": "metadata_ok"}, {"status": "not_needed"}],
        ]
        result = gemini_metadata_health.run_hour(now=self.now)

        self.assertEqual(result["checked_aliases"], 6)
        self.assertEqual(result["configured_aliases"], 5)
        self.assertEqual(result["provider_requests"], 4)
        self.assertEqual(result["deadline_skipped_models"], 2)
        self.assertEqual(check_alias.call_count, 6)
        request_ids = [call.kwargs["request_id"] for call in check_alias.call_args_list]
        self.assertEqual(len(set(request_ids)), 6)
        self.assertTrue(all(call.kwargs["deadline"] > time.monotonic() for call in check_alias.call_args_list))

    def test_hourly_timeout_budget_reserves_headroom_for_every_model_route(self):
        # Alias checks run concurrently; the shared deadline is bounded by
        # the slowest model probe, not serial alias x model multiplication.
        worst_case_seconds = gemini_metadata_health.TIMEOUT_SECONDS

        self.assertLessEqual(
            worst_case_seconds,
            gemini_metadata_health.CHECK_DEADLINE_SECONDS - 5,
        )

    @patch.dict("os.environ", {alias: f"secret-{alias.lower()}" for alias in gemini_metadata_health.gemini_keys.ALL_KEYS}, clear=False)
    @patch("management.services.gemini_metadata_health._record")
    @patch("management.services.gemini_metadata_health.check_alias")
    def test_hourly_alias_checks_overlap_without_worker_ledger_writes(self, check_alias, record):
        active = 0
        peak_active = 0
        lock = threading.Lock()
        coordinator_thread_id = threading.get_ident()
        record_thread_ids = []

        def fake_check_alias(*args, **kwargs):
            self.assertFalse(kwargs["record"])
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return [
                {"model": gemini_metadata_health.MODELS[0], "status": "metadata_ok"},
                {"model": gemini_metadata_health.MODELS[1], "status": "not_needed"},
            ]

        check_alias.side_effect = fake_check_alias
        record.side_effect = lambda **_kwargs: record_thread_ids.append(threading.get_ident())

        result = gemini_metadata_health.run_hour(now=self.now)

        self.assertEqual(check_alias.call_count, len(gemini_metadata_health.gemini_keys.ALL_KEYS))
        self.assertGreaterEqual(peak_active, 2)
        self.assertEqual(record.call_count, 2 * len(gemini_metadata_health.gemini_keys.ALL_KEYS))
        self.assertEqual(record_thread_ids, [coordinator_thread_id] * record.call_count)
        self.assertEqual(result["checked_aliases"], len(gemini_metadata_health.gemini_keys.ALL_KEYS))
        self.assertEqual(result["provider_requests"], len(gemini_metadata_health.gemini_keys.ALL_KEYS))

    @patch("management.services.gemini_metadata_health._record")
    @patch("management.services.gemini_metadata_health.check_alias")
    def test_fast_later_aliases_survive_a_slow_first_alias_deadline(
        self,
        check_alias,
        record,
    ):
        aliases = list(gemini_metadata_health.gemini_keys.ALL_KEYS)
        fast_aliases_finished = threading.Event()
        finished_fast_aliases = set()
        lock = threading.Lock()

        def fake_check_alias(alias, **kwargs):
            self.assertFalse(kwargs["record"])
            if alias == aliases[0]:
                self.assertTrue(fast_aliases_finished.wait(timeout=1))
                time.sleep(0.25)
            else:
                with lock:
                    finished_fast_aliases.add(alias)
                    if len(finished_fast_aliases) == len(aliases) - 1:
                        fast_aliases_finished.set()
            return [
                {
                    "model": gemini_metadata_health.MODELS[0],
                    "status": "metadata_ok",
                },
                {
                    "model": gemini_metadata_health.MODELS[1],
                    "status": "not_needed",
                },
            ]

        check_alias.side_effect = fake_check_alias

        results = gemini_metadata_health._run_alias_checks(
            batch_id="metadata-deadline-order",
            now=self.now,
            deadline=time.monotonic() + 0.15,
        )

        self.assertEqual(
            [row["status"] for row in results[0]],
            ["deadline_skipped"] * len(gemini_metadata_health.MODELS),
        )
        self.assertEqual(
            [[row["status"] for row in result] for result in results[1:]],
            [["metadata_ok", "not_needed"]] * (len(aliases) - 1),
        )
        self.assertEqual(
            {call.kwargs["alias"] for call in record.call_args_list},
            set(aliases[1:]),
        )

    @patch("management.services.gemini_metadata_health._record")
    @patch("management.services.gemini_metadata_health.check_alias", side_effect=RuntimeError("unexpected worker failure"))
    def test_hourly_surfaces_a_worker_failure_before_writing_partial_ledger(self, _check_alias, record):
        with self.assertRaisesRegex(RuntimeError, r"Gemini metadata health worker failed for aliases: GEMINI_API"):
            gemini_metadata_health.run_hour(now=self.now)

        record.assert_not_called()

    @patch("management.services.gemini_metadata_health.check_alias")
    def test_hourly_batch_rolls_back_all_ledger_rows_when_a_write_fails(self, check_alias):
        check_alias.return_value = [
            {"model": gemini_metadata_health.MODELS[0], "status": "metadata_ok"},
            {"model": gemini_metadata_health.MODELS[1], "status": "not_needed"},
        ]
        original_record = gemini_metadata_health._record
        calls = 0

        def fail_second_record(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("ledger unavailable")
            return original_record(**kwargs)

        with patch(
            "management.services.gemini_metadata_health._record",
            side_effect=fail_second_record,
        ):
            with self.assertRaisesRegex(RuntimeError, "ledger unavailable"):
                gemini_metadata_health.run_hour(now=self.now)

        self.assertFalse(GeminiRequestAttempt.objects.exists())

    @patch("management.services.gemini_metadata_health.ThreadPoolExecutor")
    def test_hourly_joins_provider_workers_before_returning(self, executor_type):
        class TrackingExecutor:
            def __init__(self, *args, **kwargs):
                self.shutdown_calls = []

            def submit(self, function, alias, **kwargs):
                future = Future()
                future.set_result((
                    [
                        {"model": gemini_metadata_health.MODELS[0], "status": "metadata_ok"},
                        {"model": gemini_metadata_health.MODELS[1], "status": "not_needed"},
                    ],
                    time.monotonic(),
                ))
                return future

            def shutdown(self, **kwargs):
                self.shutdown_calls.append(kwargs)

        executor = TrackingExecutor()
        executor_type.return_value = executor
        with patch("management.services.gemini_metadata_health._record"):
            gemini_metadata_health.run_hour(now=self.now)

        self.assertEqual(executor.shutdown_calls, [{"wait": True, "cancel_futures": True}])

    @patch("management.services.gemini_metadata_health.ThreadPoolExecutor")
    def test_hourly_joins_submitted_workers_when_later_submission_fails(self, executor_type):
        class TrackingExecutor:
            def __init__(self, *args, **kwargs):
                self.submit_count = 0
                self.shutdown_calls = []

            def submit(self, *args, **kwargs):
                self.submit_count += 1
                if self.submit_count == 2:
                    raise RuntimeError("executor unavailable")
                return Mock()

            def shutdown(self, **kwargs):
                self.shutdown_calls.append(kwargs)

        executor = TrackingExecutor()
        executor_type.return_value = executor

        with self.assertRaisesRegex(RuntimeError, "executor unavailable"):
            gemini_metadata_health.run_hour(now=self.now)

        self.assertEqual(executor.shutdown_calls, [{"wait": True, "cancel_futures": True}])
