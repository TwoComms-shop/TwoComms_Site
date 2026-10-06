import datetime
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from management.bot_access import (
    META_REVIEWER_GROUP_NAME,
    OPERATE_IG_BOT_PERMISSION,
    VIEW_IG_CONVERSATION_PII_PERMISSION,
)
from management.models import (
    GeminiKeyState,
    GeminiRequestAttempt,
    InstagramBotLog,
    InstagramBotSettings,
)
from management import bot_views
from management.services import gemini_health, gemini_keys, instagram_bot


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class GeminiHealthApiTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="gemini-health-admin", password="secret", is_staff=True
        )
        self.staff.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="management",
            codename__in={
                OPERATE_IG_BOT_PERMISSION.split(".", 1)[1],
                VIEW_IG_CONVERSATION_PII_PERMISSION.split(".", 1)[1],
            },
        ))
        self.user = get_user_model().objects.create_user(
            username="gemini-health-user", password="secret"
        )
        self.reviewer = get_user_model().objects.create_user(
            username="gemini-health-reviewer", password="secret"
        )
        reviewer_group, _created = Group.objects.get_or_create(
            name=META_REVIEWER_GROUP_NAME
        )
        self.reviewer.groups.add(reviewer_group)
        self.client.force_login(self.staff)

        self.secret_value = "test-gemini-provider-key-never-return"
        self.env_patcher = patch.dict(
            os.environ,
            {key_name: "" for key_name in gemini_keys.ALL_KEYS},
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        cache.clear()
        self.addCleanup(cache.clear)

    def _health_url(self):
        return reverse("management_bot_gemini_health_api")

    def _probe_url(self):
        return reverse("management_bot_gemini_health_probe_api")

    def _configure(self, key_name="GEMINI_API"):
        os.environ[key_name] = self.secret_value
        return GeminiKeyState.get(key_name)

    def _post_probe(self, *, key_name="GEMINI_API", model="gemini-3.7-flash"):
        return self.client.post(
            self._probe_url(),
            {"slot_id": gemini_health.SLOT_BY_ALIAS.get(key_name, key_name), "model": model},
        )

    def test_get_returns_snapshot_and_never_calls_provider(self):
        snapshot = {
            "schema_version": 5,
            "generated_at": "2026-08-18T12:00:00+00:00",
            "window": {"hours": 24},
            "summary": {"configured": 0},
            "fallback": None,
            "keys": [],
        }
        with (
            patch(
                "management.services.gemini_health.build_snapshot",
                return_value=snapshot,
            ) as build_snapshot,
            patch("management.services.gemini_probe.probe_key_metadata") as probe_key,
        ):
            response = self.client.get(self._health_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), snapshot)
        build_snapshot.assert_called_once_with()
        probe_key.assert_not_called()

    def test_rendered_dashboard_contains_no_environment_aliases(self):
        response = self.client.get(reverse("management_bot"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        for alias in gemini_keys.ALL_KEYS:
            self.assertNotIn(alias, html)

    def test_get_uses_redacted_snapshot_without_configured_key(self):
        self._configure()
        with patch("management.services.gemini_probe.probe_key_metadata") as probe_key:
            response = self.client.get(self._health_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["keys"]), 6)
        serialized = response.content.decode()
        self.assertNotIn(self.secret_value, serialized)
        for env_alias in gemini_keys.ALL_KEYS:
            self.assertNotIn(env_alias, serialized)
        self.assertEqual(response.json()["keys"][0]["display_label"], "API key 1")
        probe_key.assert_not_called()

    def test_manual_attempt_is_ignored_without_seventh_project_or_api_error(self):
        GeminiRequestAttempt.objects.create(
            request_id="manual-custom-route",
            role="chat",
            key_name="(manual)",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
        )

        response = self.client.get(self._health_url())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["keys"]), 6)
        self.assertEqual(
            {row["slot_id"] for row in payload["keys"]},
            set(gemini_health.SLOT_IDS),
        )
        self.assertIsNone(payload["latest_route"])
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("(manual)", serialized)
        self.assertNotIn("gslot_unknown", serialized)

    def test_status_endpoint_projects_internal_key_alias_to_opaque_slot(self):
        settings_obj = InstagramBotSettings.load()
        settings_obj.last_gemini_key = "GEMINI_API2"
        settings_obj.save(update_fields=["last_gemini_key", "updated_at"])

        response = self.client.get(reverse("management_bot_status_api"))

        self.assertEqual(response.status_code, 200)
        status = response.json()["status"]
        self.assertNotIn("last_gemini_key", status)
        self.assertEqual(
            status["last_gemini_project_slot"],
            gemini_health.SLOT_BY_ALIAS["GEMINI_API2"],
        )
        self.assertEqual(status["last_gemini_project_label"], "API key 2")
        serialized = json.dumps(response.json(), sort_keys=True)
        for env_alias in gemini_keys.ALL_KEYS:
            self.assertNotIn(env_alias, serialized)

        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.last_gemini_key, "GEMINI_API2")

    def test_status_last_error_is_recursively_redacted_for_json_and_dashboard(self):
        settings_obj = InstagramBotSettings.load()
        settings_obj.last_error = " | ".join(
            f"failure={env_alias}" for env_alias in gemini_keys.ALL_KEYS
        )
        settings_obj.save(update_fields=["last_error", "updated_at"])

        status_response = self.client.get(reverse("management_bot_status_api"))
        dashboard_response = self.client.get(reverse("management_bot"))

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(dashboard_response.status_code, 200)
        status_serialized = json.dumps(status_response.json(), sort_keys=True)
        dashboard_html = dashboard_response.content.decode("utf-8")
        for env_alias in gemini_keys.ALL_KEYS:
            self.assertNotIn(env_alias, status_serialized)
            self.assertNotIn(env_alias, dashboard_html)
            self.assertIn(
                gemini_health.public_key_reference(env_alias),
                status_serialized,
            )

        settings_obj.refresh_from_db()
        for env_alias in gemini_keys.ALL_KEYS:
            self.assertIn(env_alias, settings_obj.last_error)

    def test_log_write_boundary_persists_finite_metadata_without_raw_body(self):
        for index, env_alias in enumerate(gemini_keys.ALL_KEYS):
            instagram_bot.log(
                "info",
                "gemini_try" if index % 2 == 0 else "gemini_ok",
                f"{env_alias}/gemini-3.5-flash-lite: ok customer=private-customer-body",
            )

        rows = list(InstagramBotLog.objects.order_by("id"))
        self.assertEqual(len(rows), len(gemini_keys.ALL_KEYS))
        serialized = json.dumps(
            [{"event": row.event, "detail": row.detail} for row in rows],
            sort_keys=True,
        )
        for env_alias in gemini_keys.ALL_KEYS:
            self.assertNotIn(env_alias, serialized)
        self.assertNotIn("private-customer-body", serialized)
        for row in rows:
            metadata = json.loads(row.detail)
            self.assertEqual(metadata["schema_version"], 1)
            self.assertEqual(metadata["kind"], "provider_attempt")
            self.assertEqual(metadata["scope"], {})
            self.assertIn(metadata["reason"], {"processing", "completed"})

    def test_status_and_dashboard_read_boundaries_redact_legacy_log_rows(self):
        for index, env_alias in enumerate(gemini_keys.ALL_KEYS):
            InstagramBotLog.objects.create(
                level="info",
                event="gemini_try" if index % 2 == 0 else "gemini_ok",
                detail=f"winner={env_alias}/gemini-3.5-flash-lite customer=private-legacy-body",
            )

        # The default feed hides harmless legacy rows while advancing its scan
        # cursor. Explicit unknown history still exposes their safe projection.
        default_response = self.client.get(reverse("management_bot_status_api"))
        self.assertEqual(default_response.status_code, 200)
        default_console = default_response.json()["console"]
        self.assertEqual(default_console["items"], [])
        self.assertEqual(default_console["scanned_rows"], len(gemini_keys.ALL_KEYS))
        self.assertEqual(default_console["next_after_id"], InstagramBotLog.objects.latest("id").pk)
        status_response = self.client.get(reverse("management_bot_status_api"), {"category": "unknown"})
        dashboard_items = bot_views._log_items()

        self.assertEqual(status_response.status_code, 200)
        status_serialized = json.dumps(status_response.json(), sort_keys=True)
        dashboard_serialized = json.dumps(dashboard_items, sort_keys=True)
        for env_alias in gemini_keys.ALL_KEYS:
            self.assertNotIn(env_alias, status_serialized)
            self.assertNotIn(env_alias, dashboard_serialized)
        self.assertNotIn("private-legacy-body", status_serialized)
        self.assertNotIn("private-legacy-body", dashboard_serialized)
        self.assertEqual(status_response.json()["log"], [])
        self.assertEqual(len(status_response.json()["console"]["items"]), len(gemini_keys.ALL_KEYS))
        for item in [*status_response.json()["console"]["items"], *dashboard_items]:
            self.assertEqual(item["kind"], "legacy_event")
            self.assertEqual(item["reason"], "legacy_unstructured")
            self.assertEqual(item["detail"], "")
            self.assertEqual(item["scope"], {})

    def test_non_admin_and_meta_reviewer_cannot_read_or_probe(self):
        self._configure()
        with (
            patch("management.services.gemini_health.build_snapshot") as build_snapshot,
            patch("management.services.gemini_probe.probe_key_metadata") as probe_key,
        ):
            for user in (self.user, self.reviewer):
                with self.subTest(username=user.username):
                    self.client.force_login(user)
                    self.assertEqual(self.client.get(self._health_url()).status_code, 403)
                    self.assertEqual(self._post_probe().status_code, 403)
        build_snapshot.assert_not_called()
        probe_key.assert_not_called()

    def test_endpoint_http_methods_are_enforced(self):
        self.assertEqual(self.client.post(self._health_url()).status_code, 405)
        self.assertEqual(self.client.get(self._probe_url()).status_code, 405)

    def test_probe_rejects_unknown_values_before_provider_io(self):
        self._configure()
        invalid_requests = (
            {"slot_id": "", "model": "gemini-3.7-flash"},
            {"slot_id": "gslot_unknown", "model": "gemini-3.7-flash"},
            {"slot_id": "GEMINI_API", "model": "gemini-3.7-flash"},
            {"slot_id": gemini_health.SLOT_IDS[0], "model": ""},
            {"slot_id": gemini_health.SLOT_IDS[0], "model": "gemini-2.5-flash"},
            {"slot_id": gemini_health.SLOT_IDS[0], "model": "gemini-3.7-flash "},
        )
        with patch("management.services.gemini_probe.probe_key_metadata") as probe_key:
            for payload in invalid_requests:
                with self.subTest(payload=payload):
                    self.assertEqual(self.client.post(self._probe_url(), payload).status_code, 400)
        probe_key.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    def test_probe_rejects_unconfigured_cooldown_and_busy_key(self):
        with patch("management.services.gemini_probe.probe_key_metadata") as probe_key:
            self.assertEqual(self._post_probe().status_code, 409)
            state = self._configure()
            state.cooldown_until = timezone.now() + datetime.timedelta(minutes=5)
            state.cooldown_scope = "minute"
            state.save(update_fields=["cooldown_until", "cooldown_scope", "updated_at"])
            self.assertEqual(self._post_probe().status_code, 409)
            state.cooldown_until = None
            state.cooldown_scope = ""
            state.lease_token = "active-customer-lease"
            state.lease_until = timezone.now() + datetime.timedelta(minutes=1)
            state.lease_role = "chat"
            state.save(update_fields=[
                "cooldown_until", "cooldown_scope", "lease_token",
                "lease_until", "lease_role", "updated_at",
            ])
            self.assertEqual(self._post_probe().status_code, 409)
        probe_key.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    def test_probe_rejects_cache_lock_contention_before_provider_io(self):
        self._configure()
        with (
            patch("management.bot_views.cache.add", return_value=False),
            patch("management.services.gemini_probe.probe_key_metadata") as probe_key,
        ):
            response = self._post_probe()
        self.assertEqual(response.status_code, 409)
        probe_key.assert_not_called()
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    def test_probe_rejects_lease_race_without_provider_io(self):
        self._configure()
        with (
            patch("management.services.gemini_keys.acquire_key_lease", return_value=None),
            patch("management.services.gemini_probe.probe_key_metadata") as probe_key,
        ):
            response = self._post_probe()
        self.assertEqual(response.status_code, 409)
        probe_key.assert_not_called()

    def test_probe_rate_limit_is_scoped_to_alias_and_model(self):
        self._configure()
        result = {
            "status": "ok", "http_code": 200, "finish_reason": "STOP",
            "latency_ms": 12, "model": "gemini-3.7-flash",
        }
        with patch("management.services.gemini_probe.probe_key_metadata", return_value=result) as probe_key:
            first = self._post_probe()
            repeated = self._post_probe()
            other_model = self._post_probe(model="gemini-3.6-flash")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(repeated.status_code, 429)
        self.assertEqual(other_model.status_code, 200)
        self.assertEqual(probe_key.call_count, 2)

    def test_successful_probe_is_single_bounded_redacted_audit(self):
        state = self._configure()
        state.last_status = "existing-status"
        state.last_error = "existing-error"
        state.requests_today = 7
        state.save(update_fields=["last_status", "last_error", "requests_today", "updated_at"])
        provider_result = {
            "status": "ok", "http_code": 200, "finish_reason": "STOP",
            "thoughts_tokens": 987, "candidates_tokens": 3, "latency_ms": 27,
            "model": "provider-controlled-model", "provider_body": self.secret_value,
            "error": self.secret_value,
        }
        with patch(
            "management.services.gemini_probe.probe_key_metadata", return_value=provider_result
        ) as probe_key:
            response = self._post_probe()
        self.assertEqual(response.status_code, 200)
        probe_key.assert_called_once_with("gemini-3.7-flash", self.secret_value, timeout=(5, 20))
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["probe"]["alias"], "API key 1")
        self.assertEqual(payload["probe"]["model"], "gemini-3.7-flash")
        self.assertEqual(payload["probe"]["status"], "ok")
        self.assertEqual(payload["probe"]["http_code"], 200)
        self.assertEqual(payload["probe"]["finish_reason"], "STOP")
        self.assertEqual(payload["probe"]["latency_ms"], 27)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(self.secret_value, serialized)
        self.assertNotIn("provider_body", serialized)
        self.assertNotIn("tokens", serialized)

        attempt = GeminiRequestAttempt.objects.get()
        self.assertTrue(attempt.request_id.startswith("health-probe-"))
        self.assertEqual(
            {
                "role": attempt.role, "key_name": attempt.key_name,
                "model": attempt.model, "outcome": attempt.outcome,
                "failure_kind": attempt.failure_kind, "http_code": attempt.http_code,
                "decision": attempt.decision, "error_detail": attempt.error_detail,
                "prompt_tokens": attempt.prompt_tokens,
                "thoughts_tokens": attempt.thoughts_tokens,
                "candidates_tokens": attempt.candidates_tokens,
            },
            {
                "role": "health_metadata", "key_name": "GEMINI_API",
                "model": "gemini-3.7-flash", "outcome": "succeeded",
                "failure_kind": "", "http_code": 200,
                "decision": "manual_metadata", "error_detail": "",
                "prompt_tokens": 0, "thoughts_tokens": 0, "candidates_tokens": 0,
            },
        )
        state.refresh_from_db()
        self.assertIsNotNone(state.last_probe_at)
        self.assertEqual(state.last_probe_status, "ok")
        self.assertEqual(state.last_probe_model, "gemini-3.7-flash")
        self.assertEqual(state.last_probe_latency_ms, 27)
        self.assertEqual(state.last_probe_finish_reason, "STOP")
        self.assertEqual(state.last_probe_http_code, 200)
        self.assertEqual(state.last_probe_error, "")
        self.assertEqual(state.last_status, "existing-status")
        self.assertEqual(state.last_error, "existing-error")
        self.assertEqual(state.requests_today, 7)
        self.assertEqual(state.lease_token, "")
        self.assertIsNone(state.lease_until)
        self.assertEqual(state.lease_role, "")

    def test_metadata_ok_is_capability_success_never_generation_evidence(self):
        self._configure()
        with patch(
            "management.services.gemini_probe.probe_key_metadata",
            return_value={
                "status": "metadata_ok",
                "http_code": 200,
                "finish_reason": "",
                "latency_ms": 11,
            },
        ):
            response = self._post_probe()

        self.assertEqual(response.status_code, 200)
        attempt = GeminiRequestAttempt.objects.get()
        self.assertEqual(attempt.role, "health_metadata")
        self.assertEqual(attempt.outcome, "succeeded")
        snapshot = self.client.get(self._health_url()).json()
        key = snapshot["keys"][0]
        self.assertEqual(key["live_state"], "READY")
        self.assertEqual(key["source"], "manual_metadata")
        self.assertFalse(key["generation_quota_proven"])

    def test_manual_metadata_ready_supports_every_display_model(self):
        self._configure()
        for model in gemini_health.DISPLAY_MODELS:
            with self.subTest(model=model):
                cache.clear()
                GeminiRequestAttempt.objects.all().delete()
                with patch(
                    "management.services.gemini_probe.probe_key_metadata",
                    return_value={
                        "status": "metadata_ok",
                        "http_code": 200,
                        "finish_reason": "",
                        "latency_ms": 9,
                    },
                ):
                    response = self._post_probe(model=model)

                self.assertEqual(response.status_code, 200)
                snapshot = self.client.get(self._health_url()).json()
                key = snapshot["keys"][0]
                self.assertEqual(key["live_state"], "READY")
                self.assertEqual(key["active_model"], model)
                self.assertEqual(key["source"], "manual_metadata")
                self.assertFalse(key["generation_quota_proven"])

    def test_failed_probe_normalizes_untrusted_result_and_does_not_cooldown(self):
        state = self._configure()
        provider_result = {
            "status": self.secret_value, "http_code": 99999,
            "finish_reason": self.secret_value, "latency_ms": 999999999,
            "thoughts_tokens": 123, "raw_error": self.secret_value,
        }
        with patch("management.services.gemini_probe.probe_key_metadata", return_value=provider_result):
            response = self._post_probe()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["probe"]["status"], "provider_error")
        self.assertEqual(payload["probe"]["http_code"], 0)
        self.assertEqual(payload["probe"]["finish_reason"], "")
        self.assertEqual(payload["probe"]["latency_ms"], 120000)
        self.assertNotIn(self.secret_value, json.dumps(payload, sort_keys=True))
        attempt = GeminiRequestAttempt.objects.get()
        self.assertEqual(attempt.outcome, "failed")
        self.assertEqual(attempt.failure_kind, "provider_error")
        self.assertEqual(attempt.error_detail, "provider_error")
        self.assertIsNone(attempt.http_code)
        self.assertEqual(attempt.prompt_tokens, 0)
        self.assertEqual(attempt.thoughts_tokens, 0)
        self.assertEqual(attempt.candidates_tokens, 0)
        state.refresh_from_db()
        self.assertEqual(state.last_probe_status, "provider_error")
        self.assertEqual(state.last_probe_error, "provider_error")
        self.assertEqual(state.last_probe_finish_reason, "")
        self.assertEqual(state.last_probe_latency_ms, 120000)
        self.assertIsNone(state.cooldown_until)
        self.assertEqual(state.requests_today, 0)

    def test_unexpected_probe_exception_is_redacted_and_rate_limited(self):
        self._configure()
        with patch(
            "management.services.gemini_probe.probe_key_metadata",
            side_effect=RuntimeError(self.secret_value),
        ) as probe_key:
            first = self._post_probe()
            repeated = self._post_probe()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["probe"]["status"], "provider_error")
        self.assertNotIn(self.secret_value, first.content.decode())
        self.assertEqual(repeated.status_code, 429)
        probe_key.assert_called_once()
        self.assertEqual(GeminiRequestAttempt.objects.get().failure_kind, "provider_error")
        state = GeminiKeyState.get("GEMINI_API")
        self.assertEqual(state.lease_token, "")
        self.assertIsNone(state.lease_until)
