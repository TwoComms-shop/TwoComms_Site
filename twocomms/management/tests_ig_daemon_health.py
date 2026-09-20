import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from management.models import InstagramBotSettings
from management.services.ig_daemon_health import (
    MAIN_PROGRESS_KEY,
    PROCESS_PULSE_KEY,
    alert_daemon_runtime_health,
    daemon_runtime_health_snapshot,
)


class DaemonRuntimeHealthSnapshotTests(SimpleTestCase):
    def tearDown(self):
        cache.delete(PROCESS_PULSE_KEY)
        cache.delete(MAIN_PROGRESS_KEY)
        super().tearDown()

    def test_live_process_without_main_progress_is_stalled(self):
        cache.set(PROCESS_PULSE_KEY, {"at": 100.0, "pid": 42}, 600)

        with patch(
            "management.services.ig_daemon_health.time.time",
            return_value=110.0,
        ):
            snapshot = daemon_runtime_health_snapshot()

        self.assertTrue(snapshot["process_online"])
        self.assertTrue(snapshot["stalled"])
        self.assertFalse(snapshot["main_healthy"])
        self.assertEqual(snapshot["stalled_reason"], "main_progress_missing")

    def test_live_process_with_stale_main_progress_is_stalled(self):
        cache.set(PROCESS_PULSE_KEY, {"at": 300.0}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": 100.0, "state": "running"}, 600)

        with (
            patch(
                "management.services.ig_daemon_health.time.time",
                return_value=310.0,
            ),
            patch(
                "management.services.ig_daemon_health._alive_window_seconds",
                return_value=150,
            ),
        ):
            snapshot = daemon_runtime_health_snapshot()

        self.assertTrue(snapshot["process_online"])
        self.assertTrue(snapshot["stalled"])
        self.assertEqual(snapshot["stalled_reason"], "main_progress_stale")

    def test_fresh_idle_main_progress_is_healthy(self):
        cache.set(PROCESS_PULSE_KEY, {"at": 100.0}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": 101.0, "state": "idle", "cycle": 9}, 600)

        with patch(
            "management.services.ig_daemon_health.time.time",
            return_value=110.0,
        ):
            snapshot = daemon_runtime_health_snapshot()

        self.assertTrue(snapshot["process_online"])
        self.assertTrue(snapshot["main_healthy"])
        self.assertFalse(snapshot["stalled"])
        self.assertEqual(snapshot["main_cycle"], 9)

    @patch(
        "management.services.ig_daemon_health.technical_debt_snapshot",
        return_value={
            "observed_at": "2026-09-20T00:00:00+00:00",
            "fingerprint": "debt-fingerprint",
            "cases": [{
                "reason": "legacy_send_unknown",
                "scope": "legacy_message",
                "count": 3,
                "oldest_age_seconds": 91,
                "sample_ids": [1, 2],
                "sampled": True,
                "has_more": True,
            }],
            "case_count": 1,
            "coverage_complete": True,
            "errors": [],
            "sample_limit": 100,
        },
    )
    def test_runtime_snapshot_exposes_bounded_technical_debt(self, debt):
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)

        snapshot = daemon_runtime_health_snapshot()

        debt.assert_called_once_with(limit=100)
        self.assertTrue(snapshot["main_healthy"])
        self.assertEqual(snapshot["technical_debt"]["fingerprint"], "debt-fingerprint")
        self.assertEqual(snapshot["technical_debt"]["cases"][0]["count"], 3)


class TechnicalDebtFingerprintTests(TestCase):
    def test_fingerprint_is_stable_when_debt_age_and_count_change(self):
        from management.services.ig_technical_debt import technical_debt_snapshot

        first = {
            "reason": "canonical_delivery_unknown",
            "scope": "delivery_effect",
            "count": 1,
            "oldest_age_seconds": 10,
        }
        second = {**first, "count": 9, "oldest_age_seconds": 900}
        with patch(
            "management.services.ig_technical_debt._collect_db",
            side_effect=lambda _now, _limit: [[first], [second]][0],
        ):
            first_snapshot = technical_debt_snapshot()
        with patch(
            "management.services.ig_technical_debt._collect_db",
            side_effect=lambda _now, _limit: [second],
        ):
            second_snapshot = technical_debt_snapshot()
        self.assertEqual(first_snapshot["fingerprint"], second_snapshot["fingerprint"])

    def test_empty_private_media_root_scan_does_not_create_orphan_case(self):
        from management.services.ig_technical_debt import _collect_media
        from django.conf import settings
        from django.utils import timezone

        with patch("management.services.ig_technical_debt.os.walk", return_value=[]), \
             patch("management.services.ig_technical_debt.os.path.isdir", return_value=True), \
             patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", "/private/tmp/empty"):
            cases, complete = _collect_media(timezone.now(), 10)
        self.assertTrue(complete)
        self.assertFalse(any(case["reason"] == "orphan_private_media" for case in cases))

    def test_capped_reference_scan_does_not_falsely_classify_file_as_orphan(self):
        from django.conf import settings
        from django.utils import timezone
        from management.services.ig_technical_debt import _collect_media

        class Rows:
            def only(self, *fields):
                return self

            def iterator(self, **kwargs):
                for index in range(21):
                    yield type("Row", (), {"pk": index, "attachment_media": []})()

        with patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", "/private/tmp/media"), \
             patch("management.services.ig_technical_debt.os.path.isdir", return_value=True), \
             patch("management.services.ig_technical_debt.os.walk", return_value=[
                 ("/private/tmp/media", [], ["orphan.jpg"]),
             ]):
            # Patch the queryset constructor at the module import boundary.
            with patch("management.models.InstagramBotMessage.objects.filter", return_value=Rows()):
                cases, complete = _collect_media(timezone.now(), 1)
        self.assertFalse(complete)
        self.assertFalse(any(case["reason"] == "orphan_private_media" for case in cases))


class DaemonRuntimeHealthAlertTests(TestCase):
    def tearDown(self):
        cache.delete(PROCESS_PULSE_KEY)
        cache.delete(MAIN_PROGRESS_KEY)
        super().tearDown()

    @patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False})
    @patch("management.services.ig_alerts.alert_dedupe_key", return_value="daemon-stalled-hour")
    @patch("management.services.instagram_bot.notify_manager", return_value=True)
    def test_stalled_enabled_daemon_delivers_one_deduplicated_operator_alert(
        self, notify, dedupe, _maintenance
    ):
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.delete(MAIN_PROGRESS_KEY)

        snapshot = alert_daemon_runtime_health()

        self.assertTrue(snapshot["alerted"])
        dedupe.assert_called_once_with(
            "ig_daemon_stalled",
            window_minutes=60,
            text="main_progress_missing",
        )
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["event_type"], "ig_daemon_stalled")
        self.assertTrue(notify.call_args.kwargs["deliver_immediately"])
        self.assertNotIn("client", notify.call_args.kwargs)

    @patch("management.services.instagram_bot.notify_manager")
    def test_healthy_daemon_does_not_alert(self, notify):
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)

        snapshot = alert_daemon_runtime_health()

        self.assertFalse(snapshot["alerted"])
        notify.assert_not_called()

    @patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False})
    @patch("management.services.ig_alerts.alert_dedupe_key", return_value="worker-stalled-hour")
    @patch("management.services.instagram_bot.notify_manager", return_value=True)
    def test_stalled_background_worker_alerts_with_lane_scope(self, notify, dedupe, _maintenance):
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now, "worker_lanes": {
            "analysis": {"state": "stalled", "progress_at": now - 1000},
        }}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)
        snapshot = alert_daemon_runtime_health()
        self.assertTrue(snapshot["worker_stalled"])
        self.assertTrue(snapshot["alerted"])
        dedupe.assert_called_once_with("ig_worker_lane_stalled", window_minutes=60, text="worker_lane_stalled")
        self.assertIn("analysis", notify.call_args.kwargs["metadata"]["worker_lanes"])

    @patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False})
    @patch("management.services.ig_alerts.alert_dedupe_key", return_value="technical-debt-hour")
    @patch("management.services.instagram_bot.notify_manager", return_value=True)
    @patch(
        "management.services.ig_daemon_health.technical_debt_snapshot",
        return_value={
            "observed_at": "2026-09-20T00:00:00+00:00",
            "fingerprint": "debt-fingerprint",
            "cases": [{
                "reason": "canonical_delivery_unknown",
                "scope": "delivery_effect",
                "count": 2,
                "oldest_age_seconds": 120,
                "sample_ids": [99],
                "sampled": False,
                "has_more": False,
            }],
            "case_count": 1,
            "coverage_complete": True,
            "errors": [],
            "sample_limit": 100,
        },
    )
    def test_technical_debt_alert_is_bounded_and_fingerprint_deduplicated(
        self, debt, notify, dedupe, _maintenance
    ):
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)

        snapshot = alert_daemon_runtime_health()

        self.assertTrue(snapshot["alerted"])
        dedupe.assert_called_once_with(
            "ig_technical_debt", window_minutes=60, text="debt-fingerprint"
        )
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "ig_technical_debt")
        self.assertEqual(kwargs["metadata"]["technical_debt_fingerprint"], "debt-fingerprint")
        self.assertEqual(kwargs["metadata"]["technical_debt_cases"], [{
            "reason": "canonical_delivery_unknown",
            "scope": "delivery_effect",
            "count": 2,
            "oldest_age_seconds": 120,
        }])
        self.assertNotIn("sample_ids", kwargs["metadata"]["technical_debt_cases"][0])


class DaemonStatusUiContractTests(SimpleTestCase):
    def test_stalled_state_precedes_green_running_copy(self):
        template = (
            __import__("pathlib").Path(__file__).resolve().parent
            / "templates"
            / "management"
            / "bot.html"
        ).read_text(encoding="utf-8")
        stalled = template.index("st.state==='worker_stalled'")
        green = template.index("else if(st.is_enabled){ txt='Працює'")
        self.assertLess(stalled, green)
        self.assertIn("Обробник потребує уваги", template[stalled:green])
        self.assertIn("аналіз діалогів", template[stalled:green])
        self.assertIn("відповіді не підтверджені", template[stalled:green])
