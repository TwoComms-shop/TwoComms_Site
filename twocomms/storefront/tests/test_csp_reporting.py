import json
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from twocomms import settings as base_settings
from twocomms.log_handlers import redact_pii


@override_settings(MIDDLEWARE=[])
class CSPReportReceiverTests(SimpleTestCase):
    endpoint = "csp_report"

    def post(self, payload, content_type="application/reports+json", **extra):
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return self.client.post(
            reverse(self.endpoint),
            data=data,
            content_type=content_type,
            **extra,
        )

    def logged_records(self, get_logger):
        logger = get_logger.return_value
        records = []
        for call in logger.warning.call_args_list:
            self.assertEqual(len(call.args), 1)
            self.assertFalse(call.kwargs)
            records.append(json.loads(call.args[0]))
        return records

    @staticmethod
    def without_reported_at(record):
        return {key: value for key, value in record.items() if key != "reported_at"}

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_reporting_api_array_logs_each_csp_body_with_envelope_url_fallback(self, get_logger):
        payload = [
            {
                "type": "csp-violation",
                "url": "https://twocomms.shop/catalog/?campaign=secret#products",
                "body": {
                    "blockedURL": "https://cdn.example.test/first.js?token=secret#x",
                    "effectiveDirective": "script-src-elem",
                },
            },
            {
                "type": "csp-violation",
                "url": "https://twocomms.shop/product/hoodie/?email=buyer@example.com",
                "body": {
                    "blockedURL": "https://img.example.test/pixel.png",
                    "documentURL": "https://twocomms.shop/checkout/?phone=0501112233#pay",
                    "effectiveDirective": "img-src",
                },
            },
        ]

        response = self.post(payload)

        self.assertEqual(response.status_code, 204)
        records = self.logged_records(get_logger)
        self.assertEqual(len(records), 2)
        self.assertEqual(
            self.without_reported_at(records[0]),
            {
                "blocked_uri": "https://cdn.example.test/first.js",
                "document_uri": "https://twocomms.shop/catalog/",
                "event": "csp_violation",
                "referrer": "",
                "user_agent": "",
                "violated_directive": "script-src-elem",
            },
        )
        self.assertEqual(records[1]["event"], "csp_violation")
        self.assertEqual(records[1]["blocked_uri"], "https://img.example.test/pixel.png")
        self.assertEqual(records[1]["document_uri"], "https://twocomms.shop/checkout/")
        self.assertEqual(records[1]["violated_directive"], "img-src")
        self.assertEqual(
            get_logger.return_value.warning.call_args_list[0].args[0],
            json.dumps(records[0], sort_keys=True, ensure_ascii=False),
        )

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_legacy_report_logs_one_structured_bounded_record(self, get_logger):
        payload = {
            "csp-report": {
                "blocked-uri": "https://legacy.example.test/script.js",
                "document-uri": "https://twocomms.shop/legacy/",
                "violated-directive": "script-src",
                "referrer": "https://referrer.example.test/path",
            }
        }

        user_agent_prefix = "Браузер/1.0 "
        response = self.post(
            payload,
            content_type="application/csp-report",
            HTTP_USER_AGENT=user_agent_prefix + ("u" * 500),
        )

        self.assertEqual(response.status_code, 204)
        records = self.logged_records(get_logger)
        self.assertEqual(len(records), 1)
        self.assertEqual(
            self.without_reported_at(records[0]),
            {
                "blocked_uri": "https://legacy.example.test/script.js",
                "document_uri": "https://twocomms.shop/legacy/",
                "event": "csp_violation",
                "referrer": "https://referrer.example.test/path",
                "user_agent": user_agent_prefix + ("u" * (200 - len(user_agent_prefix))),
                "violated_directive": "script-src",
            },
        )
        self.assertIn("Браузер", get_logger.return_value.warning.call_args.args[0])
        self.assertNotIn("\\u0411", get_logger.return_value.warning.call_args.args[0])

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_url_credentials_are_removed_and_encoded_path_pii_is_redacted(self, get_logger):
        payload = [
            {
                "type": "csp-violation",
                "body": {
                    "blockedURL": "https://user:password@assets.example.test/script.js",
                    "effectiveDirective": "script-src",
                },
            },
            {
                "type": "csp-violation",
                "body": {
                    "blockedURL": "https://assets.example.test/path/buyer%40example.com/file.js",
                    "effectiveDirective": "script-src",
                },
            },
            {
                "type": "csp-violation",
                "body": {
                    "blockedURL": "https://assets.example.test/%2B%33%38%30%35%30%31%31%31%32%32%33%33/app.js",
                    "effectiveDirective": "script-src",
                },
            },
            {
                "type": "csp-violation",
                "body": {
                    "blockedURL": "https://assets.example.test/%34%34%34%34%33%33%33%33%32%32%32%32%31%31%31%31/file.js",
                    "effectiveDirective": "script-src",
                },
            },
        ]

        response = self.post(payload)

        self.assertEqual(response.status_code, 204)
        records = self.logged_records(get_logger)
        self.assertEqual(len(records), 4)
        self.assertEqual(
            records[0]["blocked_uri"],
            "https://assets.example.test/script.js",
        )
        self.assertEqual(
            records[1]["blocked_uri"],
            "https://assets.example.test/path/[email]/file.js",
        )
        self.assertEqual(
            records[2]["blocked_uri"],
            "https://assets.example.test/[phone]/app.js",
        )
        self.assertEqual(
            records[3]["blocked_uri"],
            "https://assets.example.test/[number]/file.js",
        )

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_direct_report_object_remains_supported(self, get_logger):
        response = self.post(
            {
                "blockedURL": "https://direct.example.test/app.js",
                "documentURL": "https://twocomms.shop/direct/",
                "effectiveDirective": "script-src-elem",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 204)
        records = self.logged_records(get_logger)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["blocked_uri"], "https://direct.example.test/app.js")
        self.assertEqual(records[0]["document_uri"], "https://twocomms.shop/direct/")

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_fields_are_redacted_cleaned_query_free_and_truncated(self, get_logger):
        payload = {
            "csp-report": {
                "blocked-uri": "https://assets.example.test/050-111-22-33/" + ("a" * 3000) + "?email=raw@example.com#card-4444333322221111",
                "document-uri": "https://twocomms.shop/path/buyer@example.com?phone=0501112233#private",
                "violated-directive": "script-src\u0000 buyer@example.com " + ("d" * 400),
                "referrer": "https://ref.example.test/4444333322221111\npath?secret=buyer@example.com#fragment",
            }
        }

        response = self.post(
            payload,
            content_type="application/csp-report",
            HTTP_USER_AGENT="Agent\u0007 buyer@example.com " + ("x" * 500),
        )

        self.assertEqual(response.status_code, 204)
        record = self.logged_records(get_logger)[0]
        serialized = json.dumps(record)
        self.assertNotIn("raw@example.com", serialized)
        self.assertNotIn("buyer@example.com", serialized)
        self.assertNotIn("050-111-22-33", serialized)
        self.assertNotIn("4444333322221111", serialized)
        self.assertNotIn("secret=", serialized)
        self.assertNotIn("fragment", serialized)
        self.assertNotIn("\u0000", serialized)
        self.assertNotIn("\u0007", serialized)
        self.assertNotIn("\n", record["referrer"])
        self.assertIn("[phone]", record["blocked_uri"])
        self.assertIn("[email]", record["document_uri"])
        self.assertIn("[number]", record["referrer"])
        self.assertLessEqual(len(record["blocked_uri"]), 2048)
        self.assertLessEqual(len(record["document_uri"]), 2048)
        self.assertLessEqual(len(record["referrer"]), 2048)
        self.assertLessEqual(len(record["violated_directive"]), 256)
        self.assertLessEqual(len(record["user_agent"]), 200)

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_json_escaped_lone_surrogates_are_replaced_before_logging(self, get_logger):
        response = self.post(
            {
                "csp-report": {
                    "blocked-uri": "https://assets.example.test/\ud800/script.js",
                    "violated-directive": "script-src \udfff",
                }
            },
            content_type="application/csp-report",
        )

        self.assertEqual(response.status_code, 204)
        raw_record = get_logger.return_value.warning.call_args.args[0]
        raw_record.encode("utf-8")
        record = json.loads(raw_record)
        self.assertEqual(
            record["blocked_uri"],
            "https://assets.example.test/%EF%BF%BD/script.js",
        )
        self.assertEqual(record["violated_directive"], "script-src \ufffd")

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_extension_and_non_actionable_blocked_values_are_ignored(self, get_logger):
        blocked_values = [
            "",
            "self",
            "'self'",
            "inline",
            "'unsafe-inline'",
            "eval",
            "'unsafe-eval'",
            "data",
            "data:text/javascript,alert(1)",
            "chrome-extension://abc/script.js",
            "moz-extension://abc/script.js",
            "safari-extension://abc/script.js",
        ]

        for blocked_uri in blocked_values:
            with self.subTest(blocked_uri=blocked_uri):
                get_logger.return_value.warning.reset_mock()
                response = self.post(
                    {"csp-report": {"blocked-uri": blocked_uri}},
                    content_type="application/csp-report",
                )
                self.assertEqual(response.status_code, 204)
                get_logger.return_value.warning.assert_not_called()

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_unrelated_reporting_api_types_are_ignored(self, get_logger):
        response = self.post(
            [
                {
                    "type": "deprecation",
                    "url": "https://twocomms.shop/",
                    "body": {"blockedURL": "https://example.test/deprecated.js"},
                },
                {
                    "type": "intervention",
                    "url": "https://twocomms.shop/",
                    "body": {"blockedURL": "https://example.test/intervention.js"},
                },
            ]
        )

        self.assertEqual(response.status_code, 204)
        get_logger.return_value.warning.assert_not_called()

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_request_logs_at_most_twenty_reports(self, get_logger):
        payload = [
            {
                "type": "csp-violation",
                "url": "https://twocomms.shop/",
                "body": {
                    "blockedURL": f"https://cdn{i}.example.test/script.js",
                    "effectiveDirective": "script-src-elem",
                },
            }
            for i in range(25)
        ]

        response = self.post(payload)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(get_logger.return_value.warning.call_count, 20)
        records = self.logged_records(get_logger)
        self.assertEqual(records[-1]["blocked_uri"], "https://cdn19.example.test/script.js")

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_reporting_array_does_not_scan_past_first_twenty_entries(self, get_logger):
        payload = [None] * 20 + [
            {
                "type": "csp-violation",
                "url": "https://twocomms.shop/",
                "body": {
                    "blockedURL": "https://entry21.example.test/script.js",
                    "effectiveDirective": "script-src",
                },
            }
        ]

        response = self.post(payload)

        self.assertEqual(response.status_code, 204)
        get_logger.return_value.warning.assert_not_called()

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_body_larger_than_64_kib_is_rejected_before_parsing(self, get_logger):
        oversized_payload = json.dumps(
            {"blockedURL": "https://assets.example.test/" + ("x" * (64 * 1024))}
        )

        response = self.post(oversized_payload, content_type="application/json")

        self.assertEqual(response.status_code, 204)
        get_logger.return_value.warning.assert_not_called()

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=64)
    @patch("storefront.views.static_pages.logging.getLogger")
    def test_request_data_too_big_is_caught(self, get_logger):
        response = self.post(
            {
                "blockedURL": "https://assets.example.test/script.js",
                "effectiveDirective": "script-src",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 204)
        get_logger.return_value.warning.assert_not_called()

    @patch("storefront.views.static_pages.redact_pii", wraps=redact_pii)
    @patch("storefront.views.static_pages.logging.getLogger")
    def test_huge_field_is_sliced_before_redaction_and_output_is_bounded(
        self,
        get_logger,
        redact,
    ):
        response = self.post(
            {
                "blockedURL": "https://assets.example.test/" + ("x" * 60_000),
                "effectiveDirective": "script-src",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 204)
        record = self.logged_records(get_logger)[0]
        self.assertLessEqual(len(record["blocked_uri"]), 2048)
        self.assertTrue(redact.call_args_list)
        self.assertLessEqual(
            max(len(call.args[0]) for call in redact.call_args_list),
            2048,
        )

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_every_record_contains_an_aware_utc_reported_at(self, get_logger):
        response = self.post(
            [
                {
                    "type": "csp-violation",
                    "body": {
                        "blockedURL": f"https://cdn{i}.example.test/script.js",
                        "effectiveDirective": "script-src",
                    },
                }
                for i in range(2)
            ]
        )

        self.assertEqual(response.status_code, 204)
        records = self.logged_records(get_logger)
        self.assertEqual(len(records), 2)
        for record in records:
            reported_at = datetime.fromisoformat(record["reported_at"])
            self.assertIsNotNone(reported_at.tzinfo)
            self.assertEqual(reported_at.utcoffset(), timedelta(0))

    @patch("storefront.views.static_pages.logging.getLogger")
    def test_malformed_and_unsupported_payloads_fail_closed(self, get_logger):
        cases = [
            ("{not-json", "application/reports+json"),
            ({"csp-report": {"blocked-uri": "https://example.test/a.js"}}, "text/plain"),
            (None, "application/json"),
            (42, "application/json"),
            ('"scalar"', "application/json"),
            ([None, 42, "scalar"], "application/reports+json"),
            ([{"type": "csp-violation", "body": None}], "application/reports+json"),
            ([{"type": "csp-violation", "body": "scalar"}], "application/reports+json"),
        ]

        for payload, content_type in cases:
            with self.subTest(payload=payload, content_type=content_type):
                get_logger.return_value.warning.reset_mock()
                response = self.post(payload, content_type=content_type)
                self.assertEqual(response.status_code, 204)
                get_logger.return_value.warning.assert_not_called()


class CSPLoggingConfigurationTests(SimpleTestCase):
    def test_csp_logger_uses_only_dedicated_delayed_rotating_file(self):
        handler = base_settings.LOGGING["handlers"]["csp_file"]
        self.assertEqual(handler["class"], "logging.handlers.RotatingFileHandler")
        self.assertEqual(handler["level"], "WARNING")
        self.assertEqual(handler["filename"], str(base_settings.BASE_DIR / "csp.log"))
        self.assertEqual(handler["maxBytes"], 5 * 1024 * 1024)
        self.assertEqual(handler["backupCount"], 5)
        self.assertEqual(handler["encoding"], "utf-8")
        self.assertIs(handler["delay"], True)
        self.assertEqual(handler["filters"], ["pii_redaction"])
        self.assertEqual(handler["formatter"], "message_only")
        self.assertEqual(
            base_settings.LOGGING["formatters"]["message_only"]["format"],
            "%(message)s",
        )

        logger = base_settings.LOGGING["loggers"]["csp"]
        self.assertEqual(logger["handlers"], ["csp_file"])
        self.assertEqual(logger["level"], "WARNING")
        self.assertIs(logger["propagate"], False)
