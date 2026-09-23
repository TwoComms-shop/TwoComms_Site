import datetime as dt
import base64
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from management.models import (
    GeminiQuotaProfile,
    GeminiQuotaState,
    GeminiRequest,
    GeminiRequestAttempt,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.bot_access import OPERATE_IG_BOT_PERMISSION
from management.services import (
    gemini_accounting_contract,
    gemini_routing,
    gemini_v2_read_model,
)


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class GeminiV2ReadApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="gemini-v2-read-admin", password="secret", is_staff=True
        )
        self.admin.user_permissions.add(Permission.objects.get(
            content_type__app_label="management",
            codename=OPERATE_IG_BOT_PERMISSION.split(".", 1)[1],
        ))
        self.user = get_user_model().objects.create_user(
            username="gemini-v2-read-user", password="secret"
        )
        self.client.force_login(self.admin)
        self.now = timezone.now()
        local = self.now.astimezone(gemini_v2_read_model.PT)
        effective = dt.datetime.combine(
            local.date(), dt.time.min, tzinfo=gemini_v2_read_model.PT
        )
        self.groups = {
            alias: f"read-api-project-{index}"
            for index, alias in enumerate(
                gemini_v2_read_model.gemini_keys.ALL_KEYS, start=1
            )
        }
        self.secrets = {
            alias: f"secret-sentinel-{index}-never-public"
            for index, alias in enumerate(
                gemini_v2_read_model.gemini_keys.ALL_KEYS, start=1
            )
        }
        self.env = patch.dict(os.environ, self.secrets, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.runtime = override_settings(
            GEMINI_ACCOUNTING_V2_MODE="shadow",
            GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM=effective.isoformat(),
            GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY="read-api-hmac-secret",
            GEMINI_KEY_PROJECT_GROUPS=self.groups,
        )
        self.runtime.enable()
        self.addCleanup(self.runtime.disable)
        self.profiles = {}
        for model in gemini_v2_read_model.MODELS:
            self.profiles[model] = GeminiQuotaProfile.objects.create(
                profile_version="read-api-profile-v1",
                model=model,
                rpm_limit=15 if model.endswith("lite") else 5,
                input_tpm_limit=250_000,
                rpd_limit=500 if model.endswith("lite") else 20,
                permit_limit=2 if model.endswith("lite") else 1,
                estimator_version="test-v1",
                source=GeminiQuotaProfile.Source.OWNER_OBSERVED,
                observed_at=self.now - dt.timedelta(days=1),
                effective_from=self.now - dt.timedelta(days=1),
            )

    def _graph(self, suffix, *, model="gemini-3.7-flash", alias="GEMINI_API"):
        request_id = f"private-request-{suffix}"
        plan = [{
            "candidate_index": 1,
            "project_identity": self.groups[alias],
            "model": model,
            "identity_status": "known",
            "initial_skip_reason": "",
        }]
        return GeminiRequest.objects.create(
            request_id=request_id,
            lane="live",
            task_class="ordinary_live",
            reasoning_task="customer_chat",
            logical_turn_id=f"private-turn-{suffix}",
            client_id=1000 + int(suffix),
            routing_policy_version=gemini_routing.POLICY_VERSION,
            accounting_policy_version="test-accounting-v1",
            quota_profile_version="read-api-profile-v1",
            candidate_plan=plan,
            candidate_plan_digest=(
                gemini_accounting_contract.canonical_candidate_plan_digest(plan)
            ),
            deadline_ms=35_000,
            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
        )

    @staticmethod
    def _model_row(payload, model="gemini-3.7-flash"):
        return next(item for item in payload["models"] if item["model"] == model)

    def _attempt(
        self,
        graph,
        *,
        alias="GEMINI_API",
        model="gemini-3.7-flash",
        index=1,
        fsm="succeeded",
        winner=False,
        failure_kind="",
        http_code=200,
        provider_started_at=None,
    ):
        started = provider_started_at or self.now
        row = GeminiRequestAttempt.objects.create(
            request_id=graph.request_id,
            request_graph=graph,
            role="chat",
            key_name=alias,
            project_group=self.groups[alias],
            project_identity=self.groups[alias],
            model=model,
            outcome="succeeded" if fsm in {"succeeded", "succeeded_late"} else fsm,
            fsm_state=fsm,
            quota_profile=self.profiles[model],
            accounting_mode="shadow",
            failure_kind=failure_kind,
            http_code=http_code,
            latency_ms=100 * index,
            prompt_tokens=100 * index,
            reserved_prompt_tokens=120 * index,
            lane="live",
            logical_turn_id=graph.logical_turn_id,
            client_id=graph.client_id,
            attempt_index=index,
            candidate_index=index,
            provider_started_at=started,
            dispatch_pacific_day=started.astimezone(
                gemini_v2_read_model.PT
            ).date(),
            finished_at=started + dt.timedelta(milliseconds=100 * index),
            settled_at=started + dt.timedelta(milliseconds=100 * index),
            permit_released_at=started + dt.timedelta(milliseconds=100 * index),
            winner_claimed=winner,
        )
        return row

    def _quota_url(self):
        return reverse("management_bot_gemini_v2_quotas_api")

    def _routes_url(self):
        return reverse("management_bot_gemini_v2_routes_api")

    def _attempts_url(self):
        return reverse("management_bot_gemini_v2_attempts_api")

    def test_off_mode_returns_unknown_full_5_by_6_matrix_without_writes(self):
        with override_settings(
            GEMINI_ACCOUNTING_V2_MODE="off",
            GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="",
        ):
            before = (
                GeminiQuotaState.objects.count(),
                GeminiRequest.objects.count(),
                GeminiRequestAttempt.objects.count(),
            )
            with CaptureQueriesContext(connection) as queries:
                payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
            after = (
                GeminiQuotaState.objects.count(),
                GeminiRequest.objects.count(),
                GeminiRequestAttempt.objects.count(),
            )

        self.assertEqual(before, after)
        self.assertLessEqual(len(queries), 6)
        self.assertEqual(len(payload["models"]), 5)
        self.assertTrue(all(len(row["projects"]) == 6 for row in payload["models"]))
        for model in payload["models"]:
            self.assertIsNone(model["rpm"]["limit"])
            for project in model["projects"]:
                self.assertEqual(project["status"], "accounting_unknown")
                self.assertIsNone(project["rpd"]["used"])
                self.assertFalse(project["rpd"]["complete"])

    def test_single_real_row_updates_only_its_model_and_slot(self):
        graph = self._graph("1")
        self._attempt(graph, winner=True)
        GeminiQuotaState.objects.create(
            project_identity=self.groups["GEMINI_API"],
            model="gemini-3.7-flash",
            quota_profile=self.profiles["gemini-3.7-flash"],
            pacific_day=self.now.astimezone(gemini_v2_read_model.PT).date(),
            rpd_dispatched=1,
            accounting_status=GeminiQuotaState.AccountingStatus.AVAILABLE,
            last_success_at=self.now,
        )

        payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
        rows = {
            (model["model"], project["slot_id"]): project
            for model in payload["models"] for project in model["projects"]
        }
        target = rows[("gemini-3.7-flash", gemini_v2_read_model.SLOT_IDS[0])]
        neighbour = rows[("gemini-3.7-flash", gemini_v2_read_model.SLOT_IDS[1])]
        other_model = rows[("gemini-3.6-flash", gemini_v2_read_model.SLOT_IDS[0])]
        self.assertEqual(target["rpm"]["used"], 1)
        self.assertEqual(target["input_tpm"]["used"], 100)
        self.assertEqual(target["rpd"]["used"], 1)
        self.assertEqual(target["status"], "confirmed_recent_success")
        self.assertEqual(neighbour["rpm"]["used"], 0)
        self.assertEqual(other_model["rpm"]["used"], 0)

    def test_transient_degradation_expires_but_fresh_failure_stays_visible(self):
        state = GeminiQuotaState.objects.create(
            project_identity=self.groups["GEMINI_API"],
            model="gemini-3.7-flash",
            quota_profile=self.profiles["gemini-3.7-flash"],
            pacific_day=self.now.astimezone(gemini_v2_read_model.PT).date(),
            accounting_status=GeminiQuotaState.AccountingStatus.DEGRADED,
            last_failure_at=self.now - dt.timedelta(hours=25),
            last_failure_kind="http_5xx",
            last_http_code=503,
        )

        payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
        self.assertEqual(
            self._model_row(payload)["projects"][0]["status"],
            "available_assumed",
        )

        GeminiQuotaState.objects.filter(pk=state.pk).update(
            last_failure_at=self.now - dt.timedelta(minutes=2),
        )
        payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
        self.assertEqual(
            self._model_row(payload)["projects"][0]["status"],
            "provider_degraded",
        )

        GeminiQuotaState.objects.filter(pk=state.pk).update(
            last_success_at=self.now - dt.timedelta(minutes=1),
        )
        payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
        self.assertEqual(
            self._model_row(payload)["projects"][0]["status"],
            "confirmed_recent_success",
        )

    def test_provider_429_external_drift_and_dst_reset_are_explicit(self):
        fixed_now = dt.datetime(2026, 3, 8, 9, 0, tzinfo=dt.timezone.utc)
        reset = dt.datetime(2026, 3, 9, 7, 0, tzinfo=dt.timezone.utc)
        dst_profile = GeminiQuotaProfile.objects.create(
            profile_version="read-api-dst-profile-v1",
            model="gemini-3.7-flash",
            rpm_limit=5,
            input_tpm_limit=250_000,
            rpd_limit=20,
            permit_limit=1,
            estimator_version="test-v1",
            source=GeminiQuotaProfile.Source.OWNER_OBSERVED,
            observed_at=fixed_now - dt.timedelta(days=1),
            effective_from=fixed_now - dt.timedelta(days=1),
        )
        state = GeminiQuotaState.objects.create(
            project_identity=self.groups["GEMINI_API"],
            model="gemini-3.7-flash",
            quota_profile=dst_profile,
            pacific_day=dt.date(2026, 3, 8),
            rpd_dispatched=18,
            provider_blocks={
                "rpd": {
                    "quota_id": "GenerateRequestsPerDay-FreeTier",
                    "dimensions": {
                        "model": "gemini-3.7-flash",
                        "private": "must-not-be-public",
                    },
                    "retry_after_seconds": 3600,
                    "until": reset.isoformat(),
                }
            },
            external_usage_suspected=True,
            accounting_status=GeminiQuotaState.AccountingStatus.BLOCKED,
            last_failure_at=fixed_now,
            last_failure_kind="quota_429",
            last_http_code=429,
        )
        with override_settings(
            GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-03-08T00:00:00-08:00"
        ):
            payload = gemini_v2_read_model.build_quotas_payload(now=fixed_now)

        row = self._model_row(payload)["projects"][0]
        self.assertEqual(payload["pacific_reset_at"], reset.isoformat())
        self.assertEqual(row["status"], "rpd_exhausted_until_reset")
        self.assertTrue(row["external_usage_suspected"])
        self.assertEqual(row["rpd"]["used"], 18)
        self.assertEqual(
            row["provider_blocks"][0]["quota_id"],
            "GenerateRequestsPerDay-FreeTier",
        )
        self.assertEqual(
            row["provider_blocks"][0]["dimensions"],
            {"model": "gemini-3.7-flash"},
        )
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("must-not-be-public", serialized)
        self.assertNotIn(state.project_identity, serialized)

    def test_detail_less_429_and_unclassified_blocked_state_fail_closed(self):
        pacific_local = self.now.astimezone(gemini_v2_read_model.PT)
        reset = dt.datetime.combine(
            pacific_local.date() + dt.timedelta(days=1),
            dt.time.min,
            tzinfo=gemini_v2_read_model.PT,
        ).astimezone(dt.timezone.utc)
        state = GeminiQuotaState.objects.create(
            project_identity=self.groups["GEMINI_API"],
            model="gemini-3.7-flash",
            quota_profile=self.profiles["gemini-3.7-flash"],
            pacific_day=pacific_local.date(),
            provider_blocks={
                "unknown": {
                    "quota_id": "",
                    "dimensions": {},
                    "retry_after_seconds": 0,
                    "until": reset.isoformat(),
                }
            },
            external_usage_suspected=True,
            accounting_status=GeminiQuotaState.AccountingStatus.BLOCKED,
            last_failure_at=self.now,
            last_failure_kind="quota_429",
            last_http_code=429,
        )

        response = self.client.get(self._quota_url())
        self.assertEqual(response.status_code, 200)
        row = self._model_row(response.json())["projects"][0]
        self.assertEqual(row["status"], "accounting_unknown")
        self.assertNotEqual(row["status"], "available_assumed")
        self.assertEqual(row["provider_blocks"][0]["metric"], "unknown")
        self.assertEqual(row["provider_blocks"][0]["until"], reset.isoformat())

        GeminiQuotaState.objects.filter(pk=state.pk).update(provider_blocks={})
        response = self.client.get(self._quota_url())
        self.assertEqual(response.status_code, 200)
        row = self._model_row(response.json())["projects"][0]
        self.assertEqual(row["status"], "provider_degraded")
        self.assertNotEqual(row["status"], "available_assumed")

    def test_provider_block_state_table_expires_only_classified_quota_blocks(self):
        active_until = self.now + dt.timedelta(minutes=5)
        expired_until = self.now - dt.timedelta(seconds=1)
        state = GeminiQuotaState.objects.create(
            project_identity=self.groups["GEMINI_API"],
            model="gemini-3.7-flash",
            quota_profile=self.profiles["gemini-3.7-flash"],
            pacific_day=self.now.astimezone(gemini_v2_read_model.PT).date(),
            accounting_status=GeminiQuotaState.AccountingStatus.BLOCKED,
            last_failure_at=self.now,
            last_failure_kind="quota_429",
            last_http_code=429,
        )

        cases = (
            ("active_rpm", "rpm", active_until, "quota_429", 429, "rpm_limited"),
            ("active_tpm", "tpm", active_until, "quota_429", 429, "tpm_limited"),
            (
                "active_rpd",
                "rpd",
                active_until,
                "quota_429",
                429,
                "rpd_exhausted_until_reset",
            ),
            ("expired_rpm", "rpm", expired_until, "quota_429", 429, "available_assumed"),
            ("expired_tpm", "tpm", expired_until, "quota_429", 429, "available_assumed"),
            ("expired_rpd", "rpd", expired_until, "quota_429", 429, "available_assumed"),
            ("active_unknown", "unknown", active_until, "quota_429", 429, "accounting_unknown"),
            ("expired_unknown", "unknown", expired_until, "quota_429", 429, "accounting_unknown"),
            ("expired_then_auth", "rpm", expired_until, "invalid_key", 401, "auth_failed"),
            (
                "expired_then_model_failure",
                "tpm",
                expired_until,
                "model_not_found",
                404,
                "model_unavailable_for_project",
            ),
        )
        for name, metric, until, failure_kind, http_code, expected in cases:
            with self.subTest(name=name):
                GeminiQuotaState.objects.filter(pk=state.pk).update(
                    provider_blocks={
                        metric: {
                            "quota_id": "safe-test-quota",
                            "dimensions": {"model": "gemini-3.7-flash"},
                            "retry_after_seconds": 1,
                            "until": until.isoformat(),
                        }
                    },
                    accounting_status=GeminiQuotaState.AccountingStatus.BLOCKED,
                    last_failure_at=self.now,
                    last_failure_kind=failure_kind,
                    last_http_code=http_code,
                    last_success_at=None,
                )
                payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
                row = self._model_row(payload)["projects"][0]
                self.assertEqual(row["status"], expected)

        for ambiguous_until in ("", "not-an-iso-timestamp"):
            with self.subTest(ambiguous_until=ambiguous_until):
                GeminiQuotaState.objects.filter(pk=state.pk).update(
                    provider_blocks={
                        "rpm": {
                            "quota_id": "safe-test-quota",
                            "dimensions": {"model": "gemini-3.7-flash"},
                            "retry_after_seconds": 0,
                            "until": ambiguous_until,
                        }
                    },
                    accounting_status=GeminiQuotaState.AccountingStatus.BLOCKED,
                    last_failure_kind="quota_429",
                    last_http_code=429,
                )
                payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
                self.assertEqual(
                    self._model_row(payload)["projects"][0]["status"],
                    "provider_degraded",
                )

        GeminiQuotaState.objects.filter(pk=state.pk).update(
            provider_blocks={},
            accounting_status=GeminiQuotaState.AccountingStatus.BLOCKED,
            last_failure_kind="quota_429",
            last_http_code=429,
        )
        payload = gemini_v2_read_model.build_quotas_payload(now=self.now)
        self.assertEqual(
            self._model_row(payload)["projects"][0]["status"],
            "provider_degraded",
        )

    def test_routes_use_executable_policy_and_active_expiring_pin(self):
        pinned_until = self.now + dt.timedelta(minutes=10)
        InstagramBotSettings.objects.create(
            pk=1,
            gemini_routing_mode=InstagramBotSettings.GeminiRoutingMode.PINNED,
            pinned_chat_model="gemini-3.6-flash",
            pinned_until=pinned_until,
        )

        with CaptureQueriesContext(connection) as queries:
            payload = gemini_v2_read_model.build_routes_payload(now=self.now)

        self.assertLessEqual(len(queries), 2)
        self.assertEqual(payload["policy_version"], gemini_routing.POLICY_VERSION)
        self.assertTrue(payload["emergency_pin"]["active"])
        routes = {row["task_class"]: row for row in payload["routes"]}
        ordinary = routes["ordinary_live"]
        self.assertEqual(ordinary["base_chain"], list(gemini_routing.ORDINARY_CHAIN))
        self.assertEqual(ordinary["effective_chain"][0], "gemini-3.6-flash")
        self.assertEqual(ordinary["deadline_ms"], 35_000)
        self.assertEqual(routes["durable_analysis"]["base_chain"], list(gemini_routing.ANALYSIS_CHAIN))

    def test_attempts_cursor_caps_page_and_uses_only_opaque_references(self):
        for index in range(1, 28):
            self._graph(str(index))

        first = self.client.get(self._attempts_url())
        self.assertEqual(first.status_code, 200)
        first_payload = first.json()
        self.assertEqual(len(first_payload["items"]), 25)
        self.assertTrue(first_payload["next_cursor"])
        ciphertext = base64.urlsafe_b64decode(first_payload["next_cursor"])
        self.assertNotIn(b"private-request", ciphertext)
        self.assertNotIn(b'"id":', ciphertext)
        second = self.client.get(
            self._attempts_url(), {"cursor": first_payload["next_cursor"]}
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json()["items"]), 2)
        invalid = self.client.get(self._attempts_url(), {"cursor": "not-signed"})
        self.assertEqual(invalid.status_code, 400)
        capped = self.client.get(self._attempts_url(), {"limit": 999})
        self.assertEqual(capped.json()["limit"], 50)
        serialized = first.content.decode("utf-8")
        self.assertNotIn("private-request-", serialized)
        self.assertNotIn("private-turn-", serialized)
        self.assertNotIn("read-api-project-", serialized)

    def test_atomic_winner_survives_late_successful_loser_and_receipt_is_boolean(self):
        graph = self._graph("31")
        winner = self._attempt(graph, index=1, winner=True)
        loser = self._attempt(
            graph,
            alias="GEMINI_API2",
            index=2,
            fsm="succeeded_late",
            winner=False,
            provider_started_at=self.now + dt.timedelta(milliseconds=20),
        )
        reply = InstagramBotMessage.objects.create(
            sender_id="private-customer-id",
            role=InstagramBotMessage.Role.MODEL,
            text="private reply",
            status=InstagramBotMessage.Status.DONE,
            send_state="sent",
            provider_message_id="private-meta-receipt",
            delivery_provider_message_ids=["private-meta-receipt"],
            delivery_planned_chunk_count=1,
            delivery_delivered_chunk_count=1,
        )
        winner.reply_message_id = reply.pk
        winner.save(update_fields=["reply_message_id"])
        graph.winner_attempt = winner
        graph.reply_message_id = reply.pk
        graph.terminal_resolution = "succeeded"
        graph.terminal_reason = "provider_success"
        graph.resolved_at = self.now
        graph.save(update_fields=[
            "winner_attempt", "reply_message_id", "terminal_resolution",
            "terminal_reason", "resolved_at", "updated_at",
        ])

        payload = gemini_v2_read_model.build_attempts_payload(now=self.now)
        item = payload["items"][0]
        self.assertEqual(item["winner"]["attempt_index"], 1)
        self.assertTrue(item["winner"]["winner"])
        late = next(row for row in item["attempts"] if row["attempt_index"] == 2)
        self.assertEqual(late["outcome"], "succeeded_late")
        self.assertFalse(late["winner"])
        self.assertTrue(item["reply"]["provider_receipt_present"])
        serialized = json.dumps(item, sort_keys=True)
        self.assertNotIn("private-meta-receipt", serialized)
        self.assertNotIn(loser.project_identity, serialized)

    def test_winner_only_reply_link_is_visible_and_conflict_fails_closed(self):
        graph = self._graph("32")
        winner = self._attempt(graph, winner=True)
        winner_reply = InstagramBotMessage.objects.create(
            sender_id="private-customer-id",
            role=InstagramBotMessage.Role.MODEL,
            status=InstagramBotMessage.Status.DONE,
            send_state="sent",
            provider_message_id="winner-only-receipt",
        )
        winner.reply_message_id = winner_reply.pk
        winner.save(update_fields=["reply_message_id"])
        graph.winner_attempt = winner
        graph.terminal_resolution = "succeeded"
        graph.terminal_reason = "provider_success"
        graph.save(update_fields=[
            "winner_attempt", "terminal_resolution", "terminal_reason", "updated_at",
        ])

        item = gemini_v2_read_model.build_attempts_payload(now=self.now)["items"][0]
        self.assertEqual(item["reply"]["link_source"], "winner")
        self.assertTrue(item["reply"]["provider_receipt_present"])

        conflicting = InstagramBotMessage.objects.create(
            sender_id="private-customer-id",
            role=InstagramBotMessage.Role.MODEL,
            status=InstagramBotMessage.Status.DONE,
            send_state="sent",
            provider_message_id="conflicting-receipt",
        )
        GeminiRequest.objects.filter(pk=graph.pk).update(
            reply_message_id=conflicting.pk
        )
        item = gemini_v2_read_model.build_attempts_payload(now=self.now)["items"][0]
        self.assertEqual(item["reply"]["state"], "link_conflict")
        self.assertFalse(item["reply"]["provider_receipt_present"])
        serialized = json.dumps(item, sort_keys=True)
        self.assertNotIn("winner-only-receipt", serialized)
        self.assertNotIn("conflicting-receipt", serialized)

    def test_attempt_truncation_is_exact_and_quota_details_are_allowlisted(self):
        exact = self._graph("33")
        for index in range(1, gemini_v2_read_model.ATTEMPTS_PER_REQUEST_CAP + 1):
            self._attempt(exact, index=index, fsm="failed", http_code=503)
        item = gemini_v2_read_model.build_attempts_payload(now=self.now)["items"][0]
        self.assertFalse(item["attempts_truncated"])
        self.assertEqual(len(item["attempts"]), gemini_v2_read_model.ATTEMPTS_PER_REQUEST_CAP)

        self._attempt(
            exact,
            index=gemini_v2_read_model.ATTEMPTS_PER_REQUEST_CAP + 1,
            fsm="failed",
            failure_kind="quota_429",
            http_code=429,
        )
        last = GeminiRequestAttempt.objects.filter(request_graph=exact).order_by("-id").first()
        GeminiRequestAttempt.objects.filter(pk=last.pk).update(
            provider_quota_metric="rpm",
            provider_quota_id="GenerateRequestsPerMinute-FreeTier",
            provider_quota_dimensions={
                "model": "gemini-3.7-flash",
                "secret": "must-not-be-public",
            },
            provider_retry_after_seconds=60,
            provider_block_until=self.now + dt.timedelta(seconds=60),
        )
        item = gemini_v2_read_model.build_attempts_payload(now=self.now)["items"][0]
        self.assertTrue(item["attempts_truncated"])
        self.assertEqual(len(item["attempts"]), gemini_v2_read_model.ATTEMPTS_PER_REQUEST_CAP)

        quota_graph = self._graph("34")
        quota_attempt = self._attempt(
            quota_graph,
            fsm="failed",
            failure_kind="quota_429",
            http_code=429,
        )
        GeminiRequestAttempt.objects.filter(pk=quota_attempt.pk).update(
            provider_quota_metric="rpm",
            provider_quota_id="GenerateRequestsPerMinute-FreeTier",
            provider_quota_dimensions={
                "model": "gemini-3.7-flash",
                "secret": "must-not-be-public",
            },
            provider_retry_after_seconds=60,
            provider_block_until=self.now + dt.timedelta(seconds=60),
        )
        item = gemini_v2_read_model.build_attempts_payload(now=self.now)["items"][0]
        block = item["attempts"][0]["quota_block"]
        self.assertEqual(block["quota_id"], "GenerateRequestsPerMinute-FreeTier")
        self.assertEqual(block["dimensions"], {"model": "gemini-3.7-flash"})
        self.assertNotIn("must-not-be-public", json.dumps(item, sort_keys=True))

    def test_all_reads_have_bounded_queries_zero_writes_and_no_provider_calls(self):
        graph = self._graph("41")
        self._attempt(graph, winner=True)
        providers = [
            patch("management.services.gemini_probe.probe_key_metadata"),
            patch("management.services.call_ai_analysis.requests.post"),
            patch("management.services.gemini_metadata_health.urlopen"),
        ]
        provider_mocks = []
        for provider in providers:
            provider_mocks.append(provider.start())
            self.addCleanup(provider.stop)

        with CaptureQueriesContext(connection) as quota_queries:
            gemini_v2_read_model.build_quotas_payload(now=self.now)
        with CaptureQueriesContext(connection) as route_queries:
            gemini_v2_read_model.build_routes_payload(now=self.now)
        with CaptureQueriesContext(connection) as attempt_queries:
            gemini_v2_read_model.build_attempts_payload(now=self.now)

        self.assertLessEqual(len(quota_queries), 6)
        self.assertLessEqual(len(route_queries), 2)
        self.assertLessEqual(len(attempt_queries), 4)
        all_queries = [*quota_queries, *route_queries, *attempt_queries]
        self.assertFalse(any(
            query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            for query in all_queries
        ))
        for provider_mock in provider_mocks:
            provider_mock.assert_not_called()

    def test_public_boundaries_never_emit_alias_identity_secret_or_provider_body(self):
        graph = self._graph("51")
        attempt = self._attempt(
            graph,
            fsm="failed",
            failure_kind="provider_error",
            http_code=503,
        )
        GeminiRequestAttempt.objects.filter(pk=attempt.pk).update(
            provider_reason="private-provider-reason",
            error_detail="private-provider-body",
        )
        payloads = [
            self.client.get(self._quota_url()),
            self.client.get(self._routes_url()),
            self.client.get(self._attempts_url()),
        ]
        for response in payloads:
            self.assertEqual(response.status_code, 200)
            serialized = response.content.decode("utf-8")
            for alias in gemini_v2_read_model.gemini_keys.ALL_KEYS:
                self.assertNotIn(alias, serialized)
                self.assertNotIn(self.groups[alias], serialized)
                self.assertNotIn(self.secrets[alias], serialized)
            self.assertNotIn("private-provider-reason", serialized)
            self.assertNotIn("private-provider-body", serialized)
            self.assertNotIn(graph.request_id, serialized)
            self.assertNotIn(graph.logical_turn_id, serialized)

    def test_unknown_manual_candidate_is_not_a_seventh_slot(self):
        private_identity = "private-custom-project-identity"
        plan = [{
            "candidate_index": 1,
            "project_identity": private_identity,
            "model": "gemini-2.5-flash",
            "identity_status": "unknown",
            "initial_skip_reason": "",
        }]
        graph = GeminiRequest.objects.create(
            request_id="private-request-custom",
            lane="analysis",
            task_class="durable_analysis",
            reasoning_task="customer_intelligence",
            logical_turn_id="private-turn-custom",
            routing_policy_version=gemini_routing.POLICY_VERSION,
            candidate_plan=plan,
            candidate_plan_digest=(
                gemini_accounting_contract.canonical_candidate_plan_digest(plan)
            ),
            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
        )
        GeminiRequestAttempt.objects.create(
            request_id=graph.request_id,
            request_graph=graph,
            role="management",
            key_name="(manual)",
            project_group=private_identity,
            project_identity=private_identity,
            model="gemini-2.5-flash",
            outcome="failed",
            fsm_state=GeminiRequestAttempt.FsmState.FAILED,
            accounting_mode="shadow",
            failure_kind="provider_error",
            http_code=503,
            lane="analysis",
            attempt_index=1,
            candidate_index=1,
            provider_started_at=self.now,
            dispatch_pacific_day=self.now.astimezone(
                gemini_v2_read_model.PT
            ).date(),
            finished_at=self.now,
            settled_at=self.now,
            permit_released_at=self.now,
        )

        payload = gemini_v2_read_model.build_attempts_payload(now=self.now)
        item = payload["items"][0]
        self.assertIsNone(item["candidate_plan"][0]["slot_id"])
        self.assertEqual(item["candidate_plan"][0]["model"], "unknown")
        self.assertIsNone(item["attempts"][0]["slot_id"])
        self.assertEqual(item["attempts"][0]["model"], "unknown")
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(private_identity, serialized)
        self.assertNotIn("gslot_unknown", serialized)

    def test_quota_fallback_counts_use_atomic_graph_winner(self):
        plan = [
            {
                "candidate_index": 1,
                "project_identity": self.groups["GEMINI_API"],
                "model": "gemini-3.7-flash",
                "identity_status": "known",
                "initial_skip_reason": "",
            },
            {
                "candidate_index": 2,
                "project_identity": self.groups["GEMINI_API2"],
                "model": "gemini-3.6-flash",
                "identity_status": "known",
                "initial_skip_reason": "",
            },
        ]
        graph = GeminiRequest.objects.create(
            request_id="private-request-fallback",
            lane="live",
            task_class="complex_live",
            reasoning_task="product_decision",
            logical_turn_id="private-turn-fallback",
            routing_policy_version=gemini_routing.POLICY_VERSION,
            candidate_plan=plan,
            candidate_plan_digest=(
                gemini_accounting_contract.canonical_candidate_plan_digest(plan)
            ),
            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
        )
        self._attempt(
            graph,
            index=1,
            fsm="failed",
            failure_kind="provider_error",
            http_code=503,
        )
        winner = self._attempt(
            graph,
            alias="GEMINI_API2",
            model="gemini-3.6-flash",
            index=2,
            winner=True,
        )
        graph.winner_attempt = winner
        graph.terminal_resolution = "succeeded"
        graph.terminal_reason = "provider_success"
        graph.save(update_fields=[
            "winner_attempt", "terminal_resolution", "terminal_reason", "updated_at",
        ])

        models = {
            row["model"]: row
            for row in gemini_v2_read_model.build_quotas_payload(now=self.now)["models"]
        }
        self.assertEqual(models["gemini-3.7-flash"]["fallbacks_from"], 1)
        self.assertEqual(models["gemini-3.6-flash"]["fallbacks_to"], 1)
        self.assertEqual(models["gemini-3.7-flash"]["usage_by_lane"], {"live": 1})

    def test_read_model_has_static_provider_and_write_boundary(self):
        source = Path(gemini_v2_read_model.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "requests.post", "urlopen(", "probe_key", ".objects.create(",
            ".objects.update(", ".objects.get_or_create(", ".objects.delete(",
        ):
            self.assertNotIn(forbidden, source)

    def test_endpoints_are_admin_only(self):
        self.client.force_login(self.user)
        for url in (self._quota_url(), self._routes_url(), self._attempts_url()):
            self.assertEqual(self.client.get(url).status_code, 403)
