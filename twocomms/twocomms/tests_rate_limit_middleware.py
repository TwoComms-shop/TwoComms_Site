import tempfile
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from . import middleware as middleware_module
from .middleware import SimpleRateLimitMiddleware


TEST_LIMITS = {
    "auth": 1,
    "webhook": 1,
    "telemetry": 1,
    "staff_write": 1,
    "commerce_write": 2,
    "expensive": 1,
    "catalog": 3,
    "read": 1,
}


def _multiprocess_counter_worker(start_event, output, iterations):
    start_event.wait()
    values = []
    for _ in range(iterations):
        values.append(
            middleware_module._increment_rate_limit_counter(
                "ratelimit:test:multiprocess",
                timeout=120,
            )
        )
    output.put(values)


@override_settings(
    DEBUG=False,
    SIMPLE_RATE_LIMIT_ENABLED=True,
    SIMPLE_RATE_LIMIT_WINDOW=60,
    SIMPLE_RATE_LIMITS=TEST_LIMITS,
)
class SimpleRateLimitMiddlewareTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        middleware_module._rate_limit_warning_after = 0.0
        self.factory = RequestFactory()
        self.middleware = SimpleRateLimitMiddleware(lambda request: None)

    def request(self, path, *, method="get", ip="203.0.113.10", host="twocomms.shop"):
        request = getattr(self.factory, method)(path, HTTP_HOST=host)
        request.META["REMOTE_ADDR"] = ip
        return request

    def assert_rate_limited(self, response):
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 429)

    def test_route_classes_have_independent_budgets(self):
        self.assertIsNone(self.middleware.process_request(self.request("/")))
        self.assert_rate_limited(
            self.middleware.process_request(self.request("/contacts/"))
        )

        for _ in range(3):
            self.assertIsNone(
                self.middleware.process_request(self.request("/catalog/tshirts/"))
            )
        self.assert_rate_limited(
            self.middleware.process_request(self.request("/catalog/tshirts/"))
        )

    def test_locale_prefixed_catalog_uses_catalog_budget(self):
        for _ in range(3):
            self.assertIsNone(
                self.middleware.process_request(self.request("/ru/product/example/"))
            )
        self.assert_rate_limited(
            self.middleware.process_request(self.request("/ru/product/example/"))
        )

    def test_auth_budget_is_stricter_than_other_writes(self):
        self.assertIsNone(
            self.middleware.process_request(self.request("/login/"))
        )
        self.assertIsNone(
            self.middleware.process_request(
                self.request("/accounts/ajax/login/", method="post")
            )
        )
        self.assert_rate_limited(
            self.middleware.process_request(
                self.request("/accounts/ajax/login/", method="post")
            )
        )

        self.assertIsNone(
            self.middleware.process_request(self.request("/cart/add/", method="post"))
        )
        self.assertIsNone(
            self.middleware.process_request(self.request("/cart/add/", method="post"))
        )
        self.assert_rate_limited(
            self.middleware.process_request(
                self.request("/cart/add/", method="post")
            )
        )

    def test_webhook_telemetry_staff_and_commerce_budgets_are_independent(self):
        requests = (
            self.request("/cart/add/", method="post"),
            self.request("/api/track-event/", method="post"),
            self.request("/payments/monobank/webhook/", method="post"),
            self.request(
                "/api/orders/1/",
                method="post",
                host="management.twocomms.shop",
            ),
        )
        for request in requests:
            self.assertIsNone(self.middleware.process_request(request))

        self.assertIsNone(
            self.middleware.process_request(self.request("/cart/add/", method="post"))
        )
        self.assert_rate_limited(
            self.middleware.process_request(self.request("/cart/add/", method="post"))
        )
        self.assert_rate_limited(
            self.middleware.process_request(
                self.request("/api/track-event/", method="post")
            )
        )
        self.assert_rate_limited(
            self.middleware.process_request(
                self.request("/payments/monobank/webhook/", method="post")
            )
        )
        self.assert_rate_limited(
            self.middleware.process_request(
                self.request(
                    "/api/orders/1/",
                    method="post",
                    host="management.twocomms.shop",
                )
            )
        )

    def test_main_and_dtf_admin_writes_do_not_consume_commerce_budget(self):
        ip = "203.0.113.44"
        self.assertIsNone(
            self.middleware.process_request(
                self.request("/admin-panel/product/1/", method="post", ip=ip)
            )
        )
        self.assertIsNone(
            self.middleware.process_request(
                self.request(
                    "/admin-panel/orders/1/update/",
                    method="post",
                    ip=ip,
                    host="dtf.twocomms.shop",
                )
            )
        )
        self.assertIsNone(
            self.middleware.process_request(
                self.request("/cart/add/", method="post", ip=ip)
            )
        )
        self.assertIsNone(
            self.middleware.process_request(
                self.request("/cart/add/", method="post", ip=ip)
            )
        )
        self.assert_rate_limited(
            self.middleware.process_request(
                self.request("/cart/add/", method="post", ip=ip)
            )
        )

    def test_external_callback_routes_use_webhook_and_telemetry_buckets(self):
        cases = (
            ("/orders/dropshipper/monobank/callback/", "webhook"),
            ("/data-deletion/request/", "webhook"),
            ("/bot/data-deletion/request/", "webhook"),
            ("/push/events/", "telemetry"),
            ("/binotel/api/webhook-events/", "read"),
        )
        for path, expected in cases:
            request = self.request(path, method="post" if expected != "read" else "get")
            self.assertEqual(
                middleware_module._route_rate_limit_name(request, "twocomms.shop"),
                expected,
            )

    def test_dtf_get_that_writes_is_not_exempt(self):
        request = lambda: self.request(
            "/api/quote/?length_m=1",
            host="dtf.twocomms.shop",
        )
        self.assertIsNone(self.middleware.process_request(request()))
        self.assert_rate_limited(self.middleware.process_request(request()))

    def test_same_ip_has_independent_host_budgets(self):
        self.assertIsNone(self.middleware.process_request(self.request("/contacts/")))
        self.assertIsNone(
            self.middleware.process_request(
                self.request("/clients/", host="management.twocomms.shop")
            )
        )

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_unknown_hosts_share_one_finite_budget(self):
        self.assertIsNone(
            self.middleware.process_request(
                self.request("/", host="one.example.invalid")
            )
        )
        self.assert_rate_limited(
            self.middleware.process_request(
                self.request("/", host="two.example.invalid")
            )
        )

    def test_static_and_media_requests_are_exempt(self):
        with patch.object(
            middleware_module,
            "_increment_rate_limit_counter",
            create=True,
        ) as increment:
            self.assertIsNone(
                self.middleware.process_request(self.request("/static/app.css"))
            )
            self.assertIsNone(
                self.middleware.process_request(self.request("/media/image.jpg"))
            )

        increment.assert_not_called()

    def test_dynamic_prom_media_feed_is_expensive_not_exempt(self):
        self.assertIsNone(
            self.middleware.process_request(self.request("/media/prom-feed.xml"))
        )
        self.assert_rate_limited(
            self.middleware.process_request(self.request("/media/prom-feed.xml"))
        )

    @override_settings(
        SIMPLE_RATE_LIMIT_TRUSTED_PROXY_CIDRS=("127.0.0.0/8", "::1/128"),
    )
    def test_client_ip_trusts_xff_only_from_configured_proxy_ranges(self):
        self.assertTrue(hasattr(middleware_module, "_client_rate_limit_ip"))

        direct = self.request("/", ip="172.15.1.2")
        direct.META["HTTP_X_FORWARDED_FOR"] = "198.51.100.10"
        self.assertEqual(middleware_module._client_rate_limit_ip(direct), "172.15.1.2")

        proxied = self.request("/", ip="::1")
        proxied.META["HTTP_X_FORWARDED_FOR"] = "198.51.100.10, 127.0.0.2"
        self.assertEqual(
            middleware_module._client_rate_limit_ip(proxied),
            "198.51.100.10",
        )

    @override_settings(
        SIMPLE_RATE_LIMITS={**TEST_LIMITS, "catalog": 600},
    )
    def test_catalog_budget_covers_original_489_url_crawl(self):
        for index in range(600):
            self.assertIsNone(
                self.middleware.process_request(
                    self.request(f"/product/example-{index}/")
                )
            )
        self.assert_rate_limited(
            self.middleware.process_request(self.request("/product/overflow/"))
        )

    def test_retry_after_reports_remaining_fixed_window_seconds(self):
        with patch.object(middleware_module.time, "time", return_value=121.2):
            self.assertIsNone(self.middleware.process_request(self.request("/")))
            response = self.middleware.process_request(self.request("/contacts/"))

        self.assert_rate_limited(response)
        self.assertEqual(response["Retry-After"], "59")

    def test_cache_failure_fails_open_and_logs_warning(self):
        with patch.object(
            middleware_module,
            "_increment_rate_limit_counter",
            side_effect=OSError("cache unavailable"),
            create=True,
        ), self.assertLogs("twocomms.ratelimit", level="WARNING") as logs:
            response = self.middleware.process_request(self.request("/"))

        self.assertIsNone(response)
        self.assertIn("failing open", logs.output[0])

    def test_cache_failure_warning_is_throttled(self):
        with patch.object(
            middleware_module,
            "_increment_rate_limit_counter",
            side_effect=OSError("cache unavailable"),
        ), patch.object(
            middleware_module,
            "_RATE_LIMIT_LOGGER",
            create=True,
        ) as logger:
            self.assertIsNone(self.middleware.process_request(self.request("/")))
            self.assertIsNone(self.middleware.process_request(self.request("/contacts/")))

        logger.warning.assert_called_once()


class AtomicFileRateLimitCounterTests(SimpleTestCase):
    def test_counter_uses_configured_cache_alias(self):
        caches = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "rate-limit-default-test",
            },
            "ratelimit": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "rate-limit-dedicated-test",
            },
        }
        with self.settings(
            CACHES=caches,
            SIMPLE_RATE_LIMIT_CACHE_ALIAS="ratelimit",
        ):
            middleware_module._increment_rate_limit_counter(
                "ratelimit:test:alias",
                timeout=120,
            )

            from django.core.cache import caches as django_caches

            self.assertIsNone(django_caches["default"].get("ratelimit:test:alias"))
            self.assertEqual(
                django_caches["ratelimit"].get("ratelimit:test:alias"),
                1,
            )

    def test_ignored_backend_error_is_rejected_as_fail_open_signal(self):
        class IgnoredErrorBackend:
            def add(self, *args, **kwargs):
                return False

            def incr(self, *args, **kwargs):
                return None

        with patch.object(
            middleware_module,
            "caches",
            {"default": IgnoredErrorBackend()},
        ):
            with self.assertRaises(RuntimeError):
                middleware_module._increment_rate_limit_counter(
                    "ratelimit:test:ignored-error",
                    timeout=120,
                )

    def test_file_cache_counter_does_not_lose_concurrent_increments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"
            lock_dir = Path(temp_dir) / "locks"
            caches = {
                "default": {
                    "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                    "LOCATION": str(cache_dir),
                }
            }
            with self.settings(
                CACHES=caches,
                SIMPLE_RATE_LIMIT_LOCK_DIR=str(lock_dir),
            ):
                self.assertTrue(
                    hasattr(middleware_module, "_increment_rate_limit_counter")
                )
                cache.clear()
                with ThreadPoolExecutor(max_workers=16) as executor:
                    counts = list(
                        executor.map(
                            lambda _: middleware_module._increment_rate_limit_counter(
                                "ratelimit:test:atomic",
                                timeout=120,
                            ),
                            range(80),
                        )
                    )

                self.assertEqual(sorted(counts), list(range(1, 81)))
                self.assertEqual(cache.get("ratelimit:test:atomic"), 80)
                self.assertTrue(lock_dir.is_dir())
                self.assertLessEqual(len(list(lock_dir.glob("*.lock"))), 64)

    def test_file_cache_counter_is_atomic_across_processes(self):
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("fork is required for the production-style cache test")

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"
            lock_dir = Path(temp_dir) / "locks"
            caches = {
                "default": {
                    "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                    "LOCATION": str(cache_dir),
                }
            }
            with self.settings(
                CACHES=caches,
                SIMPLE_RATE_LIMIT_LOCK_DIR=str(lock_dir),
            ):
                cache.clear()
                context = multiprocessing.get_context("fork")
                start_event = context.Event()
                output = context.Queue()
                processes = [
                    context.Process(
                        target=_multiprocess_counter_worker,
                        args=(start_event, output, 25),
                    )
                    for _ in range(8)
                ]
                for process in processes:
                    process.start()
                start_event.set()
                for process in processes:
                    process.join(timeout=15)
                    self.assertEqual(process.exitcode, 0)

                counts = []
                for _ in processes:
                    counts.extend(output.get(timeout=2))

                self.assertEqual(sorted(counts), list(range(1, 201)))
                self.assertEqual(cache.get("ratelimit:test:multiprocess"), 200)
