import time
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

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

            def count(self):
                return 21

            def iterator(self, **kwargs):
                for index in range(21):
                    yield type("Row", (), {"pk": index, "attachment_media": []})()

        with patch.object(settings, "IG_TECHNICAL_DEBT_MEDIA_SCAN_CAP", 20, create=True), \
             patch.object(settings, "IG_PRIVATE_MEDIA_ROOT", "/private/tmp/media"), \
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
    def setUp(self):
        super().setUp()
        media_root = TemporaryDirectory(prefix="twc-health-test-")
        self.addCleanup(media_root.cleanup)
        media_settings = override_settings(IG_PRIVATE_MEDIA_ROOT=media_root.name)
        media_settings.enable()
        self.addCleanup(media_settings.disable)

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

    @patch("management.services.ig_worker_progress.worker_health_snapshot", return_value={"healthy": True, "lanes": {}})
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
        self, debt, notify, dedupe, _maintenance, _workers
    ):
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)

        snapshot = alert_daemon_runtime_health()

        self.assertTrue(snapshot["alerted"])
        dedupe.assert_not_called()
        from management.services.ig_technical_debt import technical_debt_fingerprint
        fingerprint = technical_debt_fingerprint(debt.return_value["cases"])
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "ig_technical_debt")
        self.assertEqual(kwargs["metadata"]["technical_debt_fingerprint"], fingerprint)
        self.assertEqual(kwargs["dedupe_key"], f"ig_technical_debt:{fingerprint}")
        self.assertEqual(kwargs["metadata"]["technical_debt_cases"], [{
            "reason": "canonical_delivery_unknown",
            "scope": "delivery_effect",
            "count": 2,
            "oldest_age_seconds": 120,
        }])
        self.assertNotIn("sample_ids", kwargs["metadata"]["technical_debt_cases"][0])
        self.assertTrue(kwargs["metadata"]["requires_human_review"])
        alert_text = notify.call_args.args[0]
        self.assertIn("CRM:", alert_text)
        self.assertNotIn("вважаються недоступними до відновлення progress", alert_text)

    @patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False})
    @patch("management.services.ig_alerts.alert_dedupe_key", return_value="technical-debt-hour")
    @patch("management.services.instagram_bot.notify_manager", return_value=True)
    @patch(
        "management.services.ig_daemon_health.technical_debt_snapshot",
        return_value={
            "fingerprint": "debt-fingerprint",
            "cases": [{
                "reason": "canonical_delivery_unknown", "scope": "delivery_effect",
                "count": 2, "oldest_age_seconds": 120, "sample_ids": [99],
                "sampled": False, "has_more": False,
            }],
            "case_count": 1, "coverage_complete": True, "errors": [], "sample_limit": 100,
        },
    )
    def test_acknowledged_matching_debt_is_not_realerted(self, debt, notify, dedupe, _maintenance):
        from django.utils import timezone
        from management.ig_bot_models import IgTechnicalDebtCase
        from management.services.ig_technical_debt import _reconciler_case

        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        current_case = debt.return_value["cases"][0]
        fingerprint = _reconciler_case(current_case, now=timezone.now())["case_fingerprint"]
        IgTechnicalDebtCase.objects.create(
            case_key="canonical_delivery_unknown:delivery_effect",
            reason="canonical_delivery_unknown", scope="delivery_effect",
            status=IgTechnicalDebtCase.Status.ACKNOWLEDGED,
            observation_fingerprint=fingerprint,
        )
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)

        from management.services.ig_daemon_health import _unhandled_technical_debt_cases

        self.assertEqual(_unhandled_technical_debt_cases([current_case]), [])
        with patch(
            "management.services.ig_worker_progress.worker_health_snapshot",
            return_value={"healthy": True, "lanes": {}},
        ):
            snapshot = alert_daemon_runtime_health()

        self.assertFalse(snapshot["alerted"])
        notify.assert_not_called()
        dedupe.assert_not_called()

    @patch("management.services.ig_alerts.alert_dedupe_key", return_value="terminal-debt-hour")
    @patch("management.services.instagram_bot.notify_manager", return_value=True)
    @patch("management.services.ig_daemon_health.technical_debt_snapshot")
    def test_resolved_and_dismissed_matching_debt_are_not_realerted(self, debt, notify, dedupe):
        from django.utils import timezone
        from management.ig_bot_models import IgTechnicalDebtCase
        from management.services.ig_technical_debt import _reconciler_case

        current_case = {
            "reason": "canonical_delivery_unknown", "scope": "delivery_effect",
            "count": 1, "oldest_age_seconds": 120, "sample_ids": [99],
            "sampled": False, "has_more": False,
        }
        debt.return_value = {
            "fingerprint": "terminal-debt-fingerprint", "cases": [current_case],
            "case_count": 1, "coverage_complete": True, "errors": [], "sample_limit": 100,
        }
        fingerprint = _reconciler_case(current_case, now=timezone.now())["case_fingerprint"]
        for status in (IgTechnicalDebtCase.Status.RESOLVED, IgTechnicalDebtCase.Status.DISMISSED):
            IgTechnicalDebtCase.objects.create(
                case_key="canonical_delivery_unknown:delivery_effect",
                reason="canonical_delivery_unknown", scope="delivery_effect",
                status=status, observation_fingerprint=fingerprint,
            )
            from management.services.ig_daemon_health import _unhandled_technical_debt_cases

            self.assertEqual(_unhandled_technical_debt_cases([current_case]), [])
            IgTechnicalDebtCase.objects.filter(
                case_key="canonical_delivery_unknown:delivery_effect",
            ).delete()

        notify.assert_not_called()
        dedupe.assert_not_called()

    @patch("management.services.ig_worker_progress.worker_health_snapshot", return_value={"healthy": True, "lanes": {}})
    @patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False})
    @patch("management.services.ig_alerts.alert_dedupe_key", return_value="changed-terminal-debt-hour")
    @patch("management.services.instagram_bot.notify_manager", return_value=True)
    @patch("management.services.ig_daemon_health.technical_debt_snapshot")
    def test_resolved_debt_with_changed_observation_is_realertable(self, debt, notify, dedupe, _maintenance, _workers):
        from management.ig_bot_models import IgTechnicalDebtCase

        current_case = {
            "reason": "canonical_delivery_unknown", "scope": "delivery_effect",
            "count": 1, "oldest_age_seconds": 120, "sample_ids": [100],
            "sampled": False, "has_more": False,
        }
        debt.return_value = {
            "fingerprint": "changed-terminal-debt", "cases": [current_case],
            "case_count": 1, "coverage_complete": True, "errors": [], "sample_limit": 100,
        }
        IgTechnicalDebtCase.objects.create(
            case_key="canonical_delivery_unknown:delivery_effect",
            reason="canonical_delivery_unknown", scope="delivery_effect",
            status=IgTechnicalDebtCase.Status.RESOLVED,
            observation_fingerprint="previous-observation",
        )
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)
        from management.services.ig_daemon_health import alert_daemon_runtime_health

        snapshot = alert_daemon_runtime_health()

        self.assertTrue(snapshot["alerted"])
        notify.assert_called_once()
        dedupe.assert_not_called()

    @patch("management.services.ig_worker_progress.worker_health_snapshot", return_value={"healthy": True, "lanes": {}})
    @patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False})
    @patch("management.services.ig_alerts.alert_dedupe_key", return_value="technical-debt-hour")
    @patch("management.services.instagram_bot.notify_manager", return_value=True)
    @patch(
        "management.services.ig_daemon_health.technical_debt_snapshot",
        return_value={
            "fingerprint": "new-debt-fingerprint",
            "cases": [{
                "reason": "canonical_delivery_unknown", "scope": "delivery_effect",
                "count": 2, "oldest_age_seconds": 120, "sample_ids": [100],
                "sampled": False, "has_more": False,
            }],
            "case_count": 1, "coverage_complete": True, "errors": [], "sample_limit": 100,
        },
    )
    def test_acknowledged_debt_with_changed_observation_is_alertable(self, debt, notify, dedupe, _maintenance, _workers):
        from management.ig_bot_models import IgTechnicalDebtCase

        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        IgTechnicalDebtCase.objects.create(
            case_key="canonical_delivery_unknown:delivery_effect",
            reason="canonical_delivery_unknown", scope="delivery_effect",
            status=IgTechnicalDebtCase.Status.CLAIMED,
            observation_fingerprint="old-observation-fingerprint",
        )
        now = time.time()
        cache.set(PROCESS_PULSE_KEY, {"at": now}, 600)
        cache.set(MAIN_PROGRESS_KEY, {"at": now, "state": "idle"}, 600)

        snapshot = alert_daemon_runtime_health()

        self.assertTrue(snapshot["alerted"])
        notify.assert_called_once()
        dedupe.assert_not_called()


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


class TechnicalDebtNotificationRecoveryTests(TestCase):
    def _snapshot(self, ids=(10,)):
        return {"coverage_complete": True, "cases": [{
            "reason": "canonical_delivery_unknown", "scope": "delivery_effect",
            "count": len(ids), "sample_ids": list(ids), "has_more": False,
            "oldest_age_seconds": 600,
        }]}

    def _notification(self, snapshot=None, status="pending"):
        from management.models import IgBotNotification
        from management.services.ig_daemon_health import technical_debt_alert_decision

        decision = technical_debt_alert_decision(snapshot or self._snapshot())
        return IgBotNotification.objects.create(dedupe_key=decision["dedupe_key"],
            event_type="ig_technical_debt", status=status,
            payload={**decision["metadata"], "text": decision["text"]})

    def test_same_observation_survives_hour_boundaries_without_new_outbox_rows(self):
        from datetime import timedelta
        from django.utils import timezone
        from management.models import IgBotNotification
        from management.services.ig_daemon_health import technical_debt_alert_decision
        from management.services.instagram_bot import notify_manager

        first = technical_debt_alert_decision(self._snapshot())
        older = self._snapshot()
        older["cases"][0]["oldest_age_seconds"] += 3600
        with patch("django.utils.timezone.now", return_value=timezone.now()+timedelta(hours=1)):
            second = technical_debt_alert_decision(older)
        self.assertEqual(first["dedupe_key"], second["dedupe_key"])
        for decision in (first, second):
            notify_manager(decision["text"], dedupe_key=decision["dedupe_key"],
                event_type="ig_technical_debt", metadata=decision["metadata"], deliver_immediately=False)
        self.assertEqual(IgBotNotification.objects.count(), 1)
        self.assertNotEqual(first["dedupe_key"], technical_debt_alert_decision(self._snapshot((11,)))["dedupe_key"])

    def test_recovered_alarm_is_retired_with_audit(self):
        from management.services.ig_daemon_health import revalidate_technical_debt_notification

        row = self._notification()
        result = revalidate_technical_debt_notification(row.pk, snapshot={"coverage_complete": True, "cases": []})
        self.assertFalse(result)
        row.refresh_from_db()
        self.assertEqual(row.status, "resolved")
        self.assertEqual(row.failure_kind, "debt_alert_recovered")
        self.assertEqual(row.audit_events.count(), 1)

    def test_changed_or_legacy_hourly_alarm_is_retired(self):
        from management.services.ig_daemon_health import revalidate_technical_debt_notification

        row = self._notification()
        self.assertFalse(revalidate_technical_debt_notification(row.pk, snapshot=self._snapshot((11,))))
        row.refresh_from_db()
        self.assertEqual(row.failure_kind, "debt_alert_obsolete")
        row = self._notification(self._snapshot((11,)))
        row.payload.pop("technical_debt_alert_policy_version")
        row.save(update_fields=["payload"])
        self.assertFalse(revalidate_technical_debt_notification(row.pk, snapshot=self._snapshot((11,))))
        row.refresh_from_db()
        self.assertEqual(row.status, "resolved")

    def test_incomplete_inventory_defers_without_claiming_recovery(self):
        from management.services.ig_daemon_health import revalidate_technical_debt_notification

        row = self._notification()
        self.assertFalse(revalidate_technical_debt_notification(row.pk, snapshot={"coverage_complete": False, "cases": []}))
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")
        self.assertIsNotNone(row.next_attempt_at)
        self.assertEqual(row.audit_events.count(), 0)

    def test_unknown_telegram_send_is_never_replayed(self):
        from management.services.ig_daemon_health import revalidate_technical_debt_notification

        row = self._notification(status="unknown")
        self.assertFalse(revalidate_technical_debt_notification(row.pk, snapshot=self._snapshot()))
        row.refresh_from_db()
        self.assertEqual(row.status, "unknown")

    def test_obsolete_alarm_is_blocked_at_actual_delivery_boundary(self):
        from management.services.instagram_bot import _deliver_manager_notification_unlocked

        row = self._notification()
        with (patch("management.services.ig_daemon_health.technical_debt_snapshot",
                    return_value={"coverage_complete": True, "cases": []}),
              patch("management.services.instagram_bot._http") as send):
            self.assertFalse(_deliver_manager_notification_unlocked(row.dedupe_key))
        send.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.attempts, 0)

    def test_runtime_stall_is_reported_even_with_unresolved_debt(self):
        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.save(update_fields=["is_enabled", "updated_at"])
        snapshot = {"stalled": True, "worker_stalled": False, "stalled_reason": "main_progress_stale",
                    "process_age_seconds": 1, "main_age_seconds": 1000, "worker_lanes": {},
                    "technical_debt": self._snapshot()}
        with (patch("management.services.ig_daemon_health.daemon_runtime_health_snapshot", return_value=snapshot),
              patch("management.services.ig_maintenance.maintenance_status", return_value={"active": False}),
              patch("management.services.instagram_bot.notify_manager", return_value=True) as notify):
            self.assertTrue(alert_daemon_runtime_health()["alerted"])
        self.assertEqual([call.kwargs["event_type"] for call in notify.call_args_list],
                         ["ig_daemon_stalled", "ig_technical_debt"])
