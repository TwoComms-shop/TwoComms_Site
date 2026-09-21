import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import requests
from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    AdminAuditLog,
    GeminiQuotaProfile,
    GeminiQuotaState,
    GeminiKeyState,
    GeminiModelQuotaUsage,
    GeminiModelState,
    GeminiRequest,
    GeminiRequestAttempt,
)
from management.services import call_ai_analysis as ai
from management.services import gemini_accounting_runtime as runtime
from management.services import gemini_health
from management.services import gemini_probe
from management.services.gemini_routing import TurnFacts, classify_live_turn
from management.services.ig_turn_lineage import Lane, turn_lineage


SHADOW_FROM = "2026-08-29T00:00:00-07:00"
SHADOW = {
    "GEMINI_ACCOUNTING_V2_MODE": "shadow",
    "GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM": SHADOW_FROM,
    "GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY": "shadow-test-hmac-key",
    "GEMINI_KEY_PROJECT_GROUPS": {
        "GEMINI_API": "gemini-project-1",
        "GEMINI_API2": "gemini-project-2",
        "GEMINI_API3": "gemini-project-3",
        "GEMINI_API4": "gemini-project-4",
        "GEMINI_API5": "gemini-project-5",
        "GEMINI_API6": "gemini-project-6",
    },
}
KEY_ENV = {
    "GEMINI_API": "shadow-key-1",
    "GEMINI_API2": "",
    "GEMINI_API3": "",
    "GEMINI_API4": "",
    "GEMINI_API5": "",
    "GEMINI_API6": "shadow-key-6",
}


def seed_shadow_profiles():
    observed_at = dt.datetime(2026, 8, 29, 17, 18, 56, tzinfo=dt.timezone.utc)
    rows = (
        ("gemini-3.7-flash", 5, 250_000, 20, 1),
        ("gemini-3.6-flash", 5, 250_000, 20, 1),
        ("gemini-3.5-flash", 5, 250_000, 20, 1),
        ("gemini-3.5-flash-lite", 15, 250_000, 500, 2),
    )
    for model, rpm, tpm, rpd, permits in rows:
        GeminiQuotaProfile.objects.get_or_create(
            profile_version=runtime.OWNER_PROFILE_VERSION,
            model=model,
            defaults={
                "rpm_limit": rpm,
                "input_tpm_limit": tpm,
                "rpd_limit": rpd,
                "permit_limit": permits,
                "estimator_version": "shadow-calibration-required",
                "source": GeminiQuotaProfile.Source.OWNER_OBSERVED,
                "source_reference": "test_fixture",
                "observed_at": observed_at,
                "effective_from": observed_at,
                "effective_until": None,
            },
        )


class _Response:
    def __init__(self, code=200, payload=None):
        self.status_code = code
        self._payload = payload if payload is not None else {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "ok"}]},
            }],
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 3,
                "totalTokenCount": 14,
            },
        }
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def _raw_plan(model="gemini-3.7-flash", *, identity="gemini-project-1"):
    return [{
        "candidate_index": 1,
        "key_name": "GEMINI_API",
        "key_value": "must-never-be-persisted",
        "project_identity": identity,
        "identity_status": "known" if identity else "unknown",
        "model": model,
        "skip_reason": "",
        "prompt": "customer-sentinel-must-not-be-persisted",
    }]


class GeminiShadowPureContractTests(SimpleTestCase):
    def test_effective_gate_requires_aware_pacific_midnight(self):
        valid = runtime.parse_effective_from(SHADOW_FROM)
        self.assertTrue(runtime.is_pacific_midnight(valid))
        self.assertFalse(runtime.is_pacific_midnight(runtime.parse_effective_from("2026-08-29T01:00:00-07:00")))
        self.assertIsNone(runtime.parse_effective_from("2026-08-29T00:00:00"))

    def test_candidate_plan_has_an_exact_privacy_allowlist(self):
        safe = runtime.sanitize_candidate_plan(_raw_plan())
        self.assertEqual(set(safe[0]), runtime._PLAN_FIELDS)
        encoded = json.dumps(safe, sort_keys=True)
        self.assertNotIn("GEMINI_API", encoded)
        self.assertNotIn("must-never", encoded)
        self.assertNotIn("customer-sentinel", encoded)

    @patch("management.services.call_ai_analysis.gemini_scoreboard.order_candidates")
    def test_live_plan_freezes_scoreboard_order_before_assigning_indexes(self, order):
        order.side_effect = lambda candidates, **_kwargs: list(reversed(candidates))
        raw = [
            {
                "candidate_index": index,
                "key_name": alias,
                "key_value": f"private-{index}",
                "project_identity": f"project-{index}",
                "identity_status": "known",
                "model": "gemini-3.7-flash",
                "skip_reason": "" if index < 3 else "quota_cooldown",
            }
            for index, alias in enumerate(
                ("GEMINI_API", "GEMINI_API2", "GEMINI_API3"), start=1
            )
        ]

        frozen = ai._freeze_live_candidate_plan(
            raw,
            models=["gemini-3.7-flash"],
        )

        self.assertEqual(
            [row["key_name"] for row in frozen],
            ["GEMINI_API2", "GEMINI_API", "GEMINI_API3"],
        )
        self.assertEqual(
            [row["candidate_index"] for row in frozen],
            [1, 2, 3],
        )
        self.assertEqual(frozen[-1]["skip_reason"], "quota_cooldown")
        encoded = json.dumps(runtime.sanitize_candidate_plan(frozen), sort_keys=True)
        self.assertNotIn("GEMINI_API", encoded)
        self.assertNotIn("private-", encoded)

    @override_settings(**SHADOW)
    @patch.dict(
        os.environ,
        {
            "GEMINI_API3": "same-private-credential",
            "GEMINI_API4": "same-private-credential",
            "GEMINI_API5": "",
            "GEMINI_API6": "",
        },
        clear=False,
    )
    def test_hmac_detects_env_and_manual_duplicates_without_persisting_digest(self):
        from management.services import gemini_keys

        plan = runtime.generic_candidate_plan(
            role="management",
            models=["gemini-3.6-flash"],
            manual_key="same-private-credential",
        )
        duplicate_rows = [
            row for row in plan if row.get("identity_status") == "duplicate"
        ]
        self.assertGreaterEqual(len(duplicate_rows), 2)
        safe = runtime.sanitize_candidate_plan(plan)
        encoded = json.dumps(safe, sort_keys=True)
        fingerprint = gemini_keys.credential_fingerprint("same-private-credential")
        self.assertTrue(fingerprint)
        self.assertNotIn("same-private-credential", encoded)
        self.assertNotIn(fingerprint.hex(), encoded)
        self.assertFalse(any("key_name" in row for row in safe))

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T01:00:00-07:00",
    )
    def test_invalid_midnight_gate_fails_closed(self):
        self.assertFalse(runtime.shadow_runtime_active())

    def test_only_registered_python_files_contain_generation_boundary(self):
        root = Path(__file__).resolve().parents[1]
        found = set()
        for path in root.rglob("*.py"):
            if "tests" in path.name or "migrations" in path.parts:
                continue
            if ":generateContent" in path.read_text(encoding="utf-8"):
                found.add(path.relative_to(root).as_posix())
        self.assertEqual(found, {
            "management/services/call_ai_analysis.py",
            "management/services/gemini_probe.py",
        })

    @override_settings(GEMINI_ACCOUNTING_V2_MODE="enforced")
    def test_system_check_rejects_non_s3b_mode(self):
        from management.checks import gemini_accounting_shadow_check

        self.assertEqual(
            [item.id for item in gemini_accounting_shadow_check()],
            ["management.E910"],
        )

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="2026-08-29T02:00:00-07:00",
    )
    def test_system_check_rejects_non_midnight_shadow_gate(self):
        from management.checks import gemini_accounting_shadow_check

        self.assertEqual(
            [item.id for item in gemini_accounting_shadow_check()],
            ["management.E911"],
        )

    @override_settings(**{
        **SHADOW,
        "GEMINI_KEY_PROJECT_GROUPS": {
            "GEMINI_API": "same-project",
            "GEMINI_API2": "same-project",
        },
    })
    @patch.dict(os.environ, {"GEMINI_API": "one", "GEMINI_API2": "two"}, clear=False)
    def test_system_check_rejects_duplicate_project_identity(self):
        from management.checks import gemini_accounting_shadow_check

        self.assertIn(
            "management.E913",
            [item.id for item in gemini_accounting_shadow_check()],
        )

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM=SHADOW_FROM,
        GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY="shadow-test-hmac-key",
        GEMINI_KEY_PROJECT_GROUPS={},
    )
    @patch.dict(os.environ, {key: "" for key in KEY_ENV} | {"GEMINI_API": "one"}, clear=False)
    def test_system_check_does_not_treat_default_labels_as_explicit_mapping(self):
        from management.checks import gemini_accounting_shadow_check

        self.assertIn(
            "management.E916",
            [item.id for item in gemini_accounting_shadow_check()],
        )

    @override_settings(**{
        **SHADOW,
        "IG_GEMINI_ANALYSIS_SINGLE_BOUNDARY_ROTATION": False,
    })
    @patch.dict(os.environ, {key: "" for key in KEY_ENV}, clear=False)
    def test_system_check_rejects_legacy_retry_with_active_shadow(self):
        from management.checks import gemini_accounting_shadow_check

        self.assertIn(
            "management.E917",
            [item.id for item in gemini_accounting_shadow_check()],
        )

    @override_settings(**{**SHADOW, "GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY": ""})
    @patch.dict(os.environ, {key: "" for key in KEY_ENV}, clear=False)
    def test_system_check_labels_secret_key_hmac_as_shadow_only_fallback(self):
        from management.checks import gemini_accounting_shadow_check

        self.assertIn(
            "management.W915",
            [item.id for item in gemini_accounting_shadow_check()],
        )


class GeminiShadowRuntimeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_shadow_profiles()

    def _observer(self, model="gemini-3.7-flash", *, identity="gemini-project-1", lane="analysis"):
        return runtime.begin_request(
            request_id=uuid_for_test(),
            role="management",
            reasoning_task="customer_intelligence",
            candidate_plan=_raw_plan(model, identity=identity),
            lane=lane,
        )

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis._gemini_call_once")
    @patch("management.services.call_ai_analysis.requests.post")
    def test_no_model_reason_table_creates_terminal_graphs_with_zero_provider_calls(
        self,
        post,
        provider,
    ):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("no-model-graph-client")
        reasons = (
            "provider_native_ugc",
            "reaction_only",
            "repeat_guard",
            "duplicate_reply",
            "postback",
            "opt_out",
            "manager_takeover",
            "authoritative_reply",
        )
        for index, reason in enumerate(reasons, start=1):
            with self.subTest(reason=reason):
                message = InstagramBotMessage.objects.create(
                    sender_id=client.igsid,
                    client=client,
                    role=InstagramBotMessage.Role.USER,
                    text=f"bounded-{index}",
                    mid=f"no-model-mid-{index}",
                    status=InstagramBotMessage.Status.DONE,
                )
                decision = classify_live_turn(
                    TurnFacts(deterministic_action=reason)
                )
                persist_decision(message, decision)
                # A duplicate instrumentation call is idempotent at the
                # immutable message-route boundary.
                persist_decision(message, decision)

                graph = GeminiRequest.objects.get(source_message_id=message.pk)
                self.assertEqual(graph.task_class, "no_model")
                self.assertEqual(graph.reasoning_task, "no_model")
                self.assertEqual(graph.candidate_plan, [])
                self.assertEqual(graph.candidate_outcomes, {})
                self.assertEqual(graph.terminal_resolution, "succeeded")
                self.assertEqual(graph.terminal_reason, "no_model")
                self.assertIsNone(graph.provider_phase_started_at)
                self.assertIsNotNone(graph.resolved_at)
                self.assertFalse(
                    GeminiRequestAttempt.objects.filter(request_graph=graph).exists()
                )
                message.refresh_from_db()
                self.assertEqual(message.gemini_task_class, "no_model")
                self.assertEqual(message.gemini_routing_reason_codes, [reason])

        self.assertEqual(GeminiRequest.objects.count(), len(reasons))
        provider.assert_not_called()
        post.assert_not_called()

    @override_settings(**SHADOW)
    @patch("management.services.instagram_bot.gemini_generate")
    @patch("management.services.call_ai_analysis.requests.post")
    def test_existing_ingress_no_reply_guards_record_graph_without_provider(
        self,
        post,
        generate,
    ):
        from management.models import IgClient, InstagramBotMessage, InstagramBotSettings
        from management.services import instagram_bot

        settings_obj = InstagramBotSettings.load()
        settings_obj.is_enabled = True
        settings_obj.ai_enabled = True
        settings_obj.allowed_senders = ""
        settings_obj.save(update_fields=["is_enabled", "ai_enabled", "allowed_senders"])

        cases = (
            ("reaction", "ingress-reaction", "🔥", "reaction_only"),
            ("opt_out", "ingress-opt-out", "STOP", "opt_out"),
        )
        for name, sender, text_value, reason in cases:
            with self.subTest(name=name):
                self.assertTrue(instagram_bot.enqueue_inbound(
                    settings_obj,
                    sender_id=sender,
                    text=text_value,
                    mid=f"{sender}-mid",
                    source="webhook",
                ))
                message = InstagramBotMessage.objects.get(mid=f"{sender}-mid")
                self.assertEqual(message.status, InstagramBotMessage.Status.DONE)
                self.assertEqual(message.gemini_task_class, "no_model")
                self.assertEqual(message.gemini_routing_reason_codes, [reason])
                graph = GeminiRequest.objects.get(source_message_id=message.pk)
                self.assertEqual(graph.terminal_reason, "no_model")
                self.assertIsNone(graph.provider_phase_started_at)

        takeover_client = IgClient.get_or_create_for_sender("ingress-takeover")
        takeover_client.manager_takeover = True
        takeover_client.bot_paused = True
        takeover_client.paused_reason = "manager_takeover"
        takeover_client.paused_at = timezone.now()
        takeover_client.last_manager_message_at = timezone.now()
        takeover_client.save(update_fields=[
            "manager_takeover",
            "bot_paused",
            "paused_reason",
            "paused_at",
            "last_manager_message_at",
            "updated_at",
        ])
        self.assertTrue(instagram_bot.enqueue_inbound(
            settings_obj,
            sender_id=takeover_client.igsid,
            text="Ще одне повідомлення",
            mid="ingress-takeover-mid",
            source="webhook",
        ))
        takeover_message = InstagramBotMessage.objects.get(mid="ingress-takeover-mid")
        self.assertEqual(takeover_message.status, InstagramBotMessage.Status.DONE)
        self.assertEqual(takeover_message.gemini_task_class, "no_model")
        self.assertEqual(
            takeover_message.gemini_routing_reason_codes,
            ["manager_takeover"],
        )
        self.assertEqual(
            GeminiRequest.objects.get(
                source_message_id=takeover_message.pk
            ).terminal_reason,
            "no_model",
        )
        generate.assert_not_called()
        post.assert_not_called()
        self.assertEqual(GeminiRequestAttempt.objects.count(), 0)

    @override_settings(**SHADOW)
    def test_no_model_cannot_replace_provider_owned_blank_message_route(self):
        from management.models import IgClient, InstagramBotMessage
        from management.services import instagram_bot

        client = IgClient.get_or_create_for_sender("provider-owned-route")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="provider-owned-route-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        ordinary = classify_live_turn(TurnFacts())
        plan = _raw_plan("gemini-3.5-flash-lite")
        with turn_lineage(
            lane=Lane.LIVE,
            client_id=client.pk,
            source_message_id=message.pk,
            logical_turn_id=f"t{client.pk}:{message.pk}",
        ):
            observer = runtime.begin_request(
                request_id="provider-owned-route-request",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=plan,
                deadline_seconds=35,
                routing_decision=ordinary,
            )
        boundary = observer.attempt(
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            candidate_index=1,
        )
        boundary.before_provider(serialized_bytes=128)

        # The duplicate barrier runs after reply generation.  A crash may have
        # left the message route blank, but provider graph ownership is already
        # durable and must win the atomic source/lane decision.
        instagram_bot._persist_no_model_route(
            message,
            action="duplicate_reply",
        )

        message.refresh_from_db()
        graph = GeminiRequest.objects.get()
        attempt = GeminiRequestAttempt.objects.get(request_graph=graph)
        self.assertEqual(message.gemini_task_class, "ordinary_live")
        self.assertEqual(message.gemini_routing_lane, Lane.LIVE)
        self.assertEqual(
            message.gemini_routing_model_chain,
            ["gemini-3.5-flash-lite"],
        )
        self.assertEqual(graph.task_class, "ordinary_live")
        self.assertEqual(attempt.fsm_state, GeminiRequestAttempt.FsmState.PROVIDER_STARTED)
        self.assertIsNotNone(graph.provider_phase_started_at)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertFalse(GeminiRequest.objects.filter(task_class="no_model").exists())

    @override_settings(**SHADOW)
    def test_nonempty_corrupt_no_model_plan_reserves_source_before_provider_start(self):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("corrupt-provider-reservation")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="corrupt-provider-reservation-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        corrupt_no_model = classify_live_turn(
            TurnFacts(deterministic_action="duplicate_reply")
        )
        with turn_lineage(
            lane=Lane.LIVE,
            client_id=client.pk,
            source_message_id=message.pk,
            logical_turn_id=f"t{client.pk}:{message.pk}",
        ):
            provider_observer = runtime.begin_request(
                request_id="corrupt-provider-reservation-request",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=_raw_plan("gemini-3.5-flash-lite"),
                deadline_seconds=35,
                routing_decision=corrupt_no_model,
            )

        # This is the historical race: the graph exists and a boundary object
        # is ready, but provider_started is not durable yet.  The non-empty
        # immutable plan itself must reserve source/lane ownership.
        persist_decision(message, corrupt_no_model)
        message.refresh_from_db()
        self.assertEqual(message.gemini_task_class, "")
        self.assertEqual(GeminiRequest.objects.count(), 1)

        boundary = provider_observer.attempt(
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            candidate_index=1,
        )
        with patch("management.services.call_ai_analysis.requests.post") as post:
            with self.assertRaises(ai._GeminiAdmissionRejected):
                ai._gemini_call_once(
                    "gemini-3.5-flash-lite",
                    {"contents": [{"parts": [{"text": "bounded"}]}]},
                    "private-key",
                    parse=False,
                    attempt_boundary=boundary,
                )
        post.assert_not_called()

        graph = GeminiRequest.objects.get()
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertFalse(GeminiRequestAttempt.objects.filter(request_graph=graph).exists())
        self.assertIsNone(graph.provider_phase_started_at)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**SHADOW)
    def test_orphan_legacy_provider_evidence_blocks_no_model_graph(self):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("orphan-provider-evidence")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="orphan-provider-evidence-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        GeminiRequestAttempt.objects.create(
            request_id="legacy-provider-request",
            role="chat",
            key_name="GEMINI_API",
            project_group="gemini-project-1",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            fsm_state=GeminiRequestAttempt.FsmState.LEGACY,
            logical_turn_id=f"t{client.pk}:{message.pk}",
            source_message_id=message.pk,
            client_id=client.pk,
            lane=Lane.LIVE,
            attempt_index=1,
            candidate_index=1,
        )

        persist_decision(
            message,
            classify_live_turn(TurnFacts(deterministic_action="duplicate_reply")),
        )

        message.refresh_from_db()
        self.assertEqual(message.gemini_task_class, "")
        self.assertEqual(GeminiRequest.objects.count(), 0)
        self.assertEqual(GeminiRequestAttempt.objects.count(), 1)

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_duplicate_source_lane_begin_is_blocked_and_cannot_send(self, post):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("duplicate-source-lane")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="duplicate-source-lane-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        ordinary = classify_live_turn(TurnFacts())
        persist_decision(message, ordinary)
        plan = _raw_plan("gemini-3.5-flash-lite")
        with turn_lineage(
            lane=Lane.LIVE,
            client_id=client.pk,
            source_message_id=message.pk,
            logical_turn_id=f"t{client.pk}:{message.pk}",
        ):
            canonical = runtime.begin_request(
                request_id="duplicate-source-lane-canonical",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=plan,
                deadline_seconds=35,
                routing_decision=ordinary,
            )
            duplicate = runtime.begin_request(
                request_id="duplicate-source-lane-loser",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=plan,
                deadline_seconds=35,
                routing_decision=ordinary,
            )

        self.assertTrue(canonical.enabled)
        self.assertTrue(duplicate.provider_blocked)
        duplicate_boundary = duplicate.attempt(
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            candidate_index=1,
        )
        with self.assertRaises(ai._GeminiAdmissionRejected):
            ai._gemini_call_once(
                "gemini-3.5-flash-lite",
                {"contents": [{"parts": [{"text": "duplicate"}]}]},
                "private-key",
                parse=False,
                attempt_boundary=duplicate_boundary,
            )
        post.assert_not_called()

        winner_boundary = canonical.attempt(
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            candidate_index=1,
        )
        ai._gemini_call_once(
            "gemini-3.5-flash-lite",
            {"contents": [{"parts": [{"text": "canonical"}]}]},
            "private-key",
            parse=False,
            attempt_boundary=winner_boundary,
        )
        self.assertEqual(post.call_count, 1)
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.request_id, "duplicate-source-lane-canonical")
        self.assertEqual(graph.winner_attempt_id, winner_boundary.attempt_id)
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(request_graph=graph).count(),
            1,
        )

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_stale_boundary_after_terminal_no_model_never_calls_provider(self, post):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("terminal-no-model-boundary")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="terminal-no-model-boundary-mid",
            status=InstagramBotMessage.Status.DONE,
        )
        persist_decision(
            message,
            classify_live_turn(TurnFacts(deterministic_action="reaction_only")),
        )
        graph = GeminiRequest.objects.get()
        stale_observer = runtime.RequestObserver(
            graph_id=graph.pk,
            request_id=graph.request_id,
            raw_plan=_raw_plan("gemini-3.5-flash-lite"),
        )
        boundary = stale_observer.attempt(
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
            candidate_index=1,
        )

        with self.assertRaises(ai._GeminiAdmissionRejected):
            ai._gemini_call_once(
                "gemini-3.5-flash-lite",
                {"contents": [{"parts": [{"text": "stale"}]}]},
                "private-key",
                parse=False,
                attempt_boundary=boundary,
            )

        post.assert_not_called()
        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(graph.terminal_reason, "no_model")
        self.assertIsNone(graph.provider_phase_started_at)
        self.assertEqual(GeminiRequestAttempt.objects.count(), 0)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**SHADOW)
    def test_database_rejects_historical_competing_source_lane_graph(self):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_accounting_contract import (
            canonical_candidate_plan_digest,
        )
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("competing-source-lane")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="competing-source-lane-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        ordinary = classify_live_turn(TurnFacts())
        persist_decision(message, ordinary)
        raw_plan = _raw_plan("gemini-3.5-flash-lite")
        with turn_lineage(
            lane=Lane.LIVE,
            client_id=client.pk,
            source_message_id=message.pk,
            logical_turn_id=f"t{client.pk}:{message.pk}",
        ):
            canonical = runtime.begin_request(
                request_id="competing-source-lane-canonical",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=raw_plan,
                deadline_seconds=35,
                routing_decision=ordinary,
            )
        safe_plan = runtime.sanitize_candidate_plan(raw_plan)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeminiRequest.objects.create(
                request_id="competing-source-lane-historical",
                lane=Lane.LIVE,
                task_class="ordinary_live",
                reasoning_task="customer_chat",
                logical_turn_id=f"t{client.pk}:{message.pk}",
                source_message_id=message.pk,
                client_id=client.pk,
                routing_policy_version=ordinary.policy_version,
                accounting_policy_version=runtime.ACCOUNTING_POLICY_VERSION,
                authority_snapshot_version=ordinary.authority_snapshot_version,
                routing_mode=ordinary.routing_mode.value,
                commercial_risk=ordinary.commercial_risk,
                candidate_plan=safe_plan,
                candidate_plan_digest=canonical_candidate_plan_digest(safe_plan),
                deadline_ms=35_000,
                accounting_mode=GeminiRequest.AccountingMode.SHADOW,
            )

        self.assertTrue(canonical.enabled)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(GeminiRequestAttempt.objects.count(), 0)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_same_index_model_nonmember_key_is_rejected_before_provider(self, post):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("nonmember-admission")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="nonmember-admission-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        ordinary = classify_live_turn(TurnFacts())
        persist_decision(message, ordinary)
        manual_plan = [{
            "candidate_index": 1,
            "key_name": "(manual)",
            "project_identity": "",
            "identity_status": "unknown",
            "model": "gemini-3.5-flash-lite",
            "skip_reason": "",
        }]
        with turn_lineage(
            lane=Lane.LIVE,
            client_id=client.pk,
            source_message_id=message.pk,
            logical_turn_id=f"t{client.pk}:{message.pk}",
        ):
            observer = runtime.begin_request(
                request_id="nonmember-admission-request",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=manual_plan,
                deadline_seconds=35,
                routing_decision=ordinary,
            )
        boundary = observer.attempt(
            key_name="NOT_A_PLAN_MEMBER",
            model="gemini-3.5-flash-lite",
            candidate_index=1,
        )

        with self.assertRaises(ai._GeminiAdmissionRejected):
            ai._gemini_call_once(
                "gemini-3.5-flash-lite",
                {"contents": [{"parts": [{"text": "spoof"}]}]},
                "unplanned-private-key",
                parse=False,
                attempt_boundary=boundary,
            )

        post.assert_not_called()
        graph = GeminiRequest.objects.get(request_id=observer.request_id)
        self.assertIsNone(graph.provider_phase_started_at)
        self.assertEqual(GeminiRequestAttempt.objects.count(), 0)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)
        self.assertEqual(GeminiModelQuotaUsage.objects.count(), 0)

    @override_settings(**SHADOW)
    def test_source_noncontention_database_outage_remains_shadow_fail_soft(self):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("source-db-outage")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="source-db-outage-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        ordinary = classify_live_turn(TurnFacts())
        persist_decision(message, ordinary)
        with (
            turn_lineage(
                lane=Lane.LIVE,
                client_id=client.pk,
                source_message_id=message.pk,
                logical_turn_id=f"t{client.pk}:{message.pk}",
            ),
            patch(
                "management.models.GeminiRequest.objects.create",
                side_effect=OperationalError("server has gone away"),
            ),
        ):
            observer = runtime.begin_request(
                request_id="source-db-outage-request",
                role="chat",
                reasoning_task="customer_chat",
                candidate_plan=_raw_plan("gemini-3.5-flash-lite"),
                deadline_seconds=35,
                routing_decision=ordinary,
            )

        self.assertFalse(observer.enabled)
        self.assertFalse(observer.provider_blocked)
        self.assertTrue(observer.attempt().before_provider(serialized_bytes=1))
        self.assertEqual(GeminiRequest.objects.count(), 0)

    @override_settings(**SHADOW)
    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "admission-cancel-key",
            "GEMINI_API2": "",
            "GEMINI_API3": "",
            "GEMINI_API4": "",
            "GEMINI_API5": "",
            "GEMINI_API6": "",
        },
        clear=False,
    )
    @patch("management.services.call_ai_analysis.requests.post")
    def test_final_admission_rejection_returns_legacy_quota_reservation(self, post):
        class RejectAtFinalBoundary:
            attempt_id = None

            def validate_ownership(self):
                return True

            def before_provider(self, **_kwargs):
                return False

            def cancelled_pre_dispatch(self, _error=None):
                return None

        class Observer:
            enabled = True
            provider_blocked = False
            request_id = "admission-cancel-request"

            def candidate_index(self, _key_name, _model):
                return 1

            def attempt(self, **_kwargs):
                return RejectAtFinalBoundary()

            def record_remaining(self, *_args, **_kwargs):
                return None

        with patch.object(runtime, "begin_request", return_value=Observer()):
            with self.assertRaises(ai.CallAIAnalysisError):
                ai.gemini_generate_text(
                    {"contents": [{"parts": [{"text": "bounded"}]}]},
                    role="chat",
                    model_chain_override=["gemini-3.5-flash-lite"],
                )

        post.assert_not_called()
        usage = GeminiModelQuotaUsage.objects.get(
            key_name="GEMINI_API",
            model="gemini-3.5-flash-lite",
        )
        self.assertEqual(usage.requests, 0)
        self.assertEqual(usage.minute_requests, 0)

    @override_settings(**SHADOW)
    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "shadow-hedge-key-1",
            "GEMINI_API2": "shadow-hedge-key-2",
            "GEMINI_API3": "",
            "GEMINI_API4": "",
            "GEMINI_API5": "",
            "GEMINI_API6": "",
        },
        clear=False,
    )
    @patch("management.services.call_ai_analysis.ENABLE_LEGACY_CHAT_HEDGE", True)
    @patch("management.services.call_ai_analysis.gemini_hedge.run_hedged")
    @patch("management.services.call_ai_analysis.gemini_quota.settle")
    @patch("management.services.call_ai_analysis.gemini_quota.try_reserve", return_value=True)
    @patch("management.services.call_ai_analysis.gemini_scoreboard.order_candidates")
    @patch("management.services.call_ai_analysis._gemini_call_once")
    def test_enabled_lite_hedge_is_disabled_in_shadow_and_graph_stays_canonical(
        self,
        call_once,
        order,
        _reserve,
        _settle,
        run_hedged,
    ):
        order.side_effect = lambda candidates, **_kwargs: list(reversed(candidates))

        def provider(*_args, **kwargs):
            boundary = kwargs["attempt_boundary"]
            boundary.before_provider(serialized_bytes=128)
            boundary.succeeded({})
            return "ok", {}

        call_once.side_effect = provider
        out = ai.gemini_generate_text(
            {"contents": [{"parts": [{"text": "bounded"}]}]},
            role="chat",
            model_chain_override=["gemini-3.5-flash-lite"],
        )

        self.assertEqual(out["parsed"], "ok")
        run_hedged.assert_not_called()
        graph = GeminiRequest.objects.get()
        attempts = list(
            GeminiRequestAttempt.objects.filter(request_graph=graph).order_by(
                "attempt_index"
            )
        )
        provider_rows = [row for row in attempts if row.provider_started_at]
        self.assertEqual(len(provider_rows), 1)
        winner = provider_rows[0]
        self.assertEqual(winner.project_identity, "gemini-project-2")
        self.assertEqual(winner.candidate_index, 1)
        self.assertTrue(winner.winner_claimed)
        graph.refresh_from_db()
        self.assertEqual(graph.winner_attempt_id, winner.pk)
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(graph.terminal_reason, "provider_success")
        self.assertFalse(
            GeminiRequestAttempt.objects.filter(
                request_graph=graph,
                candidate_index=winner.candidate_index,
                outcome="not_attempted",
            ).exists()
        )
        losers = [row for row in attempts if row.pk != winner.pk]
        self.assertTrue(losers)
        self.assertTrue(all(
            row.fsm_state == GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH
            for row in losers
        ))
        self.assertTrue(all(row.outcome == "not_attempted" for row in losers))
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                request_id=graph.request_id,
                request_graph__isnull=True,
            ).count(),
            0,
        )

    @override_settings(GEMINI_ACCOUNTING_V2_MODE="off", GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="")
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_default_off_has_absolute_zero_v2_rows(self, _post):
        with CaptureQueriesContext(connection) as captured:
            out = ai.gemini_generate_text(
                {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
                role="management",
                reasoning_task="memory_summary",
            )
        self.assertEqual(out["parsed"], "ok")
        accounting_sql = "\n".join(query["sql"].casefold() for query in captured)
        self.assertNotIn("management_geminirequest\"", accounting_sql)
        self.assertNotIn("management_geminiquotastate", accounting_sql)
        self.assertEqual(GeminiRequest.objects.count(), 0)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)
        self.assertEqual(
            GeminiRequestAttempt.objects.exclude(fsm_state=GeminiRequestAttempt.FsmState.LEGACY).count(),
            0,
        )

    @override_settings(**{**SHADOW, "GEMINI_NONLIVE_ADMISSION_MODE": "enforce"})
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post")
    @patch(
        "management.services.gemini_accounting_runtime.begin_request",
        side_effect=RuntimeError("accounting unavailable"),
    )
    def test_required_admission_failure_stops_generic_generation_before_provider(
        self, _begin, post
    ):
        with self.assertRaises(ai.CallAIAnalysisError):
            ai.gemini_generate_text(
                {"contents": [{"parts": [{"text": "hello"}]}]},
                role="management",
                reasoning_task="memory_summary",
            )
        post.assert_not_called()

    @override_settings(**{**SHADOW, "GEMINI_NONLIVE_ADMISSION_MODE": "enforce"})
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post")
    @patch("management.services.gemini_accounting_runtime.begin_request")
    def test_required_null_observer_stops_generic_generation_before_provider(
        self, begin_request, post
    ):
        begin_request.return_value = runtime.NULL_OBSERVER

        with self.assertRaises(ai.CallAIAnalysisError):
            ai.gemini_generate_text(
                {"contents": [{"parts": [{"text": "hello"}]}]},
                role="management",
                reasoning_task="memory_summary",
            )
        post.assert_not_called()

    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_internal_request_id_exists_only_in_shadow_meta_and_not_public_health(self, _post):
        payload = {"contents": [{"parts": [{"text": "memory"}]}]}
        with override_settings(
            GEMINI_ACCOUNTING_V2_MODE="off",
            GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM="",
        ):
            off = ai.gemini_generate_text(
                payload,
                role="management",
                reasoning_task="memory_summary",
            )
        self.assertNotIn("request_id", off.get("meta") or {})

        with override_settings(**SHADOW):
            shadow = ai.gemini_generate_text(
                payload,
                role="management",
                reasoning_task="memory_summary",
            )
            internal_id = shadow["meta"]["request_id"]
            self.assertEqual(
                GeminiRequest.objects.get(request_id=internal_id).request_id,
                internal_id,
            )
            public_snapshot = gemini_health.build_snapshot()
        self.assertNotIn(internal_id, json.dumps(public_snapshot, sort_keys=True))

    @override_settings(**SHADOW)
    def test_begin_request_profile_reads_are_bounded_and_plan_is_sanitized(self):
        with CaptureQueriesContext(connection) as captured:
            observer = self._observer()
        self.assertTrue(observer.enabled)
        self.assertLessEqual(
            len([
                q for q in captured
                if q["sql"].lstrip().upper().startswith("SELECT")
            ]),
            2,
        )
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        encoded = json.dumps(graph.candidate_plan, sort_keys=True)
        self.assertNotIn("GEMINI_API", encoded)
        self.assertNotIn("must-never", encoded)
        self.assertNotIn("customer-sentinel", encoded)

    @override_settings(**SHADOW)
    @patch.dict(
        os.environ,
        {
            "GEMINI_API3": "db-private-duplicate",
            "GEMINI_API4": "db-private-duplicate",
            "GEMINI_API5": "",
            "GEMINI_API6": "",
        },
        clear=False,
    )
    def test_duplicate_hmac_and_raw_credential_never_enter_request_database(self):
        from management.services import gemini_keys

        plan = runtime.generic_candidate_plan(
            role="management",
            models=["gemini-3.6-flash"],
            manual_key="db-private-duplicate",
        )
        observer = runtime.begin_request(
            request_id=uuid_for_test(),
            role="management",
            reasoning_task="memory_summary",
            candidate_plan=plan,
            lane="analysis",
        )
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        encoded = json.dumps(graph.candidate_plan, sort_keys=True)
        fingerprint = gemini_keys.credential_fingerprint("db-private-duplicate")
        self.assertNotIn("db-private-duplicate", encoded)
        self.assertNotIn(fingerprint.hex(), encoded)
        self.assertGreaterEqual(
            sum(row["identity_status"] == "duplicate" for row in graph.candidate_plan),
            2,
        )

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_success_creates_one_attempt_state_and_atomic_winner(self, _post):
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        parsed, usage = ai._gemini_call_once(
            "gemini-3.7-flash",
            {"contents": [{"parts": [{"text": "private prompt"}]}]},
            "private-key",
            parse=False,
            attempt_boundary=boundary,
        )
        self.assertEqual(parsed, "ok")
        self.assertEqual(usage["promptTokenCount"], 11)
        attempt = GeminiRequestAttempt.objects.get(request_graph_id=observer.graph_id)
        state = GeminiQuotaState.objects.get(
            project_identity="gemini-project-1", model="gemini-3.7-flash"
        )
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual(attempt.fsm_state, GeminiRequestAttempt.FsmState.SUCCEEDED)
        self.assertEqual(attempt.prompt_tokens, 11)
        self.assertEqual(attempt.total_tokens, 14)
        self.assertTrue(attempt.winner_claimed)
        self.assertEqual(graph.winner_attempt_id, attempt.pk)
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(state.rpd_dispatched, 1)
        self.assertEqual(state.in_flight_count, 0)

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", side_effect=requests.ReadTimeout("late"))
    def test_timeout_is_ambiguous_and_conservatively_counted(self, _post):
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        with self.assertRaises(ai._GeminiTransient):
            ai._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "private-key",
                parse=False, attempt_boundary=boundary,
            )
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        state = GeminiQuotaState.objects.get(pk=boundary.state_id)
        self.assertEqual(attempt.fsm_state, GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS)
        self.assertEqual(attempt.outcome, "timeout_ambiguous")
        self.assertEqual(state.rpd_dispatched, 1)
        self.assertEqual(state.rpd_uncertain, 1)
        self.assertEqual(state.in_flight_count, 0)

        # A caller/reaper replay must not consume or release the same attempt twice.
        boundary.failed(ai._GeminiTransient("timeout: repeated"))
        state.refresh_from_db()
        self.assertEqual(state.rpd_dispatched, 1)
        self.assertEqual(state.rpd_uncertain, 1)
        self.assertEqual(state.in_flight_count, 0)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch(
        "management.services.call_ai_analysis.requests.post",
        side_effect=requests.ReadTimeout("live-timeout"),
    )
    def test_live_legacy_audit_cannot_overwrite_timeout_fsm(self, _post):
        with self.assertRaises(ai.CallAIAnalysisError):
            ai.gemini_generate_text(
                {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
                role="chat",
                model_chain_override=["gemini-3.7-flash"],
            )
        graph = GeminiRequest.objects.get()
        provider_rows = GeminiRequestAttempt.objects.filter(
            request_graph=graph,
            provider_started_at__isnull=False,
        )
        self.assertGreaterEqual(provider_rows.count(), 1)
        self.assertFalse(
            provider_rows.exclude(
                fsm_state=GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
                outcome="timeout_ambiguous",
                failure_kind="read_timeout",
            ).exists()
        )
        self.assertFalse(
            GeminiRequestAttempt.objects.filter(
                request_id=graph.request_id,
                request_graph__isnull=True,
            ).exists()
        )

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_live_legacy_audit_cannot_reclassify_malformed_envelope(self, post):
        response = _Response()
        response.json = lambda: ["malformed"]
        post.return_value = response
        with self.assertRaises(ai.CallAIAnalysisError):
            ai.gemini_generate_text(
                {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
                role="chat",
                model_chain_override=["gemini-3.7-flash"],
            )
        graph = GeminiRequest.objects.get()
        provider_rows = GeminiRequestAttempt.objects.filter(
            request_graph=graph,
            provider_started_at__isnull=False,
        )
        self.assertGreaterEqual(provider_rows.count(), 1)
        self.assertFalse(
            provider_rows.exclude(
                fsm_state=GeminiRequestAttempt.FsmState.FAILED,
                outcome="failed",
                failure_kind="invalid_response",
            ).exists()
        )

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_malformed_200_envelope_is_not_transport_ambiguous(self, post):
        response = _Response()
        response.json = lambda: (_ for _ in ()).throw(ValueError("bad json"))
        post.return_value = response
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        with self.assertRaises(ai._GeminiTransient):
            ai._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "private-key",
                parse=False, attempt_boundary=boundary,
            )
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        state = GeminiQuotaState.objects.get(pk=boundary.state_id)
        self.assertEqual(attempt.fsm_state, GeminiRequestAttempt.FsmState.FAILED)
        self.assertEqual(attempt.failure_kind, "invalid_response")
        self.assertEqual(state.rpd_uncertain, 0)

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_wrong_200_envelope_shape_settles_failed_not_in_flight(self, post):
        response = _Response()
        response.json = lambda: ["not-an-envelope"]
        post.return_value = response
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        with self.assertRaises(ai._GeminiTransient):
            ai._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "private-key",
                parse=False, attempt_boundary=boundary,
            )
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        state = GeminiQuotaState.objects.get(pk=boundary.state_id)
        self.assertEqual(attempt.failure_kind, "invalid_response")
        self.assertEqual(attempt.fsm_state, GeminiRequestAttempt.FsmState.FAILED)
        self.assertEqual(state.in_flight_count, 0)

    @override_settings(**SHADOW)
    def test_final_body_failure_is_linked_cancelled_without_quota_spend(self):
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        with patch.object(ai, "PROVIDER_REQUEST_MAX_BYTES", 1):
            with self.assertRaises(ai._GeminiFatal):
                ai._gemini_call_once(
                    "gemini-3.7-flash",
                    {"contents": [{"parts": [{"text": "too large"}]}]},
                    "private-key",
                    parse=False,
                    attempt_boundary=boundary,
                )
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        self.assertEqual(
            attempt.fsm_state,
            GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
        )
        self.assertEqual(attempt.outcome, "cancelled_pre_dispatch")
        self.assertIsNone(attempt.provider_started_at)
        self.assertIsNone(attempt.dispatch_pacific_day)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual(
            graph.candidate_outcomes["1"]["outcome"],
            "cancelled_pre_dispatch",
        )

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    def test_gateway_payload_fatal_updates_same_linked_cancelled_row(self):
        with patch.object(ai, "PROVIDER_REQUEST_MAX_BYTES", 1):
            with self.assertRaises(ai.CallAIAnalysisError):
                ai.gemini_generate_text(
                    {"contents": [{"parts": [{"text": "too large"}]}]},
                    role="management",
                    reasoning_task="memory_summary",
                )
        graph = GeminiRequest.objects.get()
        rows = GeminiRequestAttempt.objects.filter(request_id=graph.request_id)
        attempt = rows.get(
            fsm_state=GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
            failure_kind="invalid_payload",
        )
        self.assertEqual(attempt.request_graph_id, graph.pk)
        self.assertEqual(
            attempt.fsm_state,
            GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
        )
        self.assertEqual(attempt.outcome, "cancelled_pre_dispatch")
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertFalse(rows.filter(request_graph__isnull=True).exists())

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_unknown_custom_and_unprofiled_25_never_create_invented_state(self, _post):
        for model, identity in (
            ("gemini-3.7-flash", ""),
            ("gemini-2.5-flash", "gemini-project-1"),
        ):
            key_name = "GEMINI_API" if identity else "(manual)"
            observer = (
                self._observer(model, identity=identity)
                if identity
                else runtime.begin_request(
                    request_id=uuid_for_test(),
                    role="management",
                    reasoning_task="customer_intelligence",
                    candidate_plan=[{
                        "candidate_index": 1,
                        "key_name": key_name,
                        "project_identity": "",
                        "identity_status": "unknown",
                        "model": model,
                        "skip_reason": "",
                    }],
                    lane="analysis",
                )
            )
            boundary = observer.attempt(
                key_name=key_name, model=model, candidate_index=1
            )
            ai._gemini_call_once(
                model, {"contents": []}, "unknown-custom", parse=False,
                attempt_boundary=boundary,
            )
            attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
            self.assertIsNone(attempt.quota_profile_id)
            self.assertEqual(attempt.shadow_decision, GeminiRequestAttempt.ShadowDecision.UNKNOWN)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_provider_truth_is_counted_even_when_shadow_would_deny(self, _post):
        profile = GeminiQuotaProfile.objects.get(
            profile_version=runtime.OWNER_PROFILE_VERSION,
            model="gemini-3.7-flash",
        )
        GeminiQuotaState.objects.create(
            project_identity="gemini-project-1",
            model=profile.model,
            quota_profile=profile,
            pacific_day=timezone.now().astimezone(runtime.PT).date(),
            rpd_dispatched=profile.rpd_limit,
        )
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model=profile.model, candidate_index=1
        )
        ai._gemini_call_once(
            profile.model, {"contents": []}, "private-key", parse=False,
            attempt_boundary=boundary,
        )
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        state = GeminiQuotaState.objects.get(project_identity="gemini-project-1", model=profile.model)
        self.assertEqual(attempt.shadow_decision, GeminiRequestAttempt.ShadowDecision.DENY)
        self.assertEqual(attempt.shadow_deny_reason, "rpd_exhausted")
        self.assertEqual(state.rpd_dispatched, profile.rpd_limit + 1)

    @override_settings(**SHADOW)
    def test_stale_provider_boundary_reconciles_and_late_success_is_idempotent(self):
        first = self._observer()
        first_boundary = first.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        first_boundary.before_provider(serialized_bytes=100, inline_count=0)
        stale_at = timezone.now() - dt.timedelta(seconds=1)
        GeminiRequestAttempt.objects.filter(pk=first_boundary.attempt_id).update(
            permit_expires_at=stale_at,
            reservation_expires_at=stale_at,
        )
        GeminiQuotaState.objects.filter(pk=first_boundary.state_id).update(
            next_permit_expiry_at=stale_at,
        )

        second = self._observer()
        second_boundary = second.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        second_boundary.before_provider(serialized_bytes=100, inline_count=0)
        first_attempt = GeminiRequestAttempt.objects.get(pk=first_boundary.attempt_id)
        self.assertEqual(
            first_attempt.fsm_state,
            GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
        )
        second_boundary.manual_result(
            succeeded=True,
            http_code=200,
            usage={"promptTokenCount": 3, "totalTokenCount": 4},
        )
        first_boundary.succeeded({"promptTokenCount": 5, "totalTokenCount": 7})
        first_boundary.succeeded({"promptTokenCount": 5, "totalTokenCount": 7})

        first_attempt.refresh_from_db()
        state = GeminiQuotaState.objects.get(pk=first_boundary.state_id)
        self.assertEqual(
            first_attempt.fsm_state,
            GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
        )
        self.assertEqual(state.rpd_dispatched, 2)
        self.assertEqual(state.rpd_uncertain, 0)
        self.assertEqual(state.in_flight_count, 0)

    @override_settings(**SHADOW)
    def test_late_success_does_not_release_a_newer_attempt_permit(self):
        first = self._observer()
        first_boundary = first.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        first_boundary.before_provider(serialized_bytes=100, inline_count=0)
        stale_at = timezone.now() - dt.timedelta(seconds=1)
        GeminiRequestAttempt.objects.filter(pk=first_boundary.attempt_id).update(
            permit_expires_at=stale_at,
            reservation_expires_at=stale_at,
        )

        second = self._observer()
        second_boundary = second.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        second_boundary.before_provider(serialized_bytes=100, inline_count=0)
        state = GeminiQuotaState.objects.get(pk=first_boundary.state_id)
        self.assertEqual(state.in_flight_count, 1)

        first_boundary.succeeded({"promptTokenCount": 2, "totalTokenCount": 3})
        state.refresh_from_db()
        self.assertEqual(state.in_flight_count, 1)

        third = self._observer()
        third_boundary = third.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        third_boundary.before_provider(serialized_bytes=100, inline_count=0)
        third_attempt = GeminiRequestAttempt.objects.get(pk=third_boundary.attempt_id)
        self.assertEqual(
            third_attempt.shadow_deny_reason,
            "permit_exhausted",
        )
        second_boundary.manual_result(succeeded=True, http_code=200, usage={})
        third_boundary.manual_result(succeeded=True, http_code=200, usage={})
        state.refresh_from_db()
        self.assertEqual(state.in_flight_count, 0)

    @override_settings(**SHADOW)
    def test_cross_midnight_settlement_keeps_original_dispatch_day(self):
        dispatch_at = dt.datetime(2026, 8, 31, 6, 59, tzinfo=dt.timezone.utc)
        finish_at = dt.datetime(2026, 8, 31, 7, 1, tzinfo=dt.timezone.utc)
        with patch.object(runtime.timezone, "now", return_value=dispatch_at):
            observer = self._observer()
            boundary = observer.attempt(
                key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
            )
            boundary.before_provider(serialized_bytes=100, inline_count=0)
        with patch.object(runtime.timezone, "now", return_value=finish_at):
            boundary.manual_result(
                succeeded=True,
                http_code=200,
                usage={"promptTokenCount": 3, "totalTokenCount": 4},
            )
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        self.assertEqual(attempt.dispatch_pacific_day, dt.date(2026, 8, 30))

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_structured_429_is_bounded_and_marks_external_drift(self, post):
        post.return_value = _Response(429, {
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "message": "private provider body",
                "details": [{
                    "retryDelay": "42s",
                    "violations": [{
                        "quotaMetric": "rpd",
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                        "quotaDimensions": {
                            "model": "gemini-3.7-flash",
                            "location": "global",
                            "project": "real-project-id-must-not-persist",
                        },
                    }],
                }],
            },
        })
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        with self.assertRaises(ai._Gemini429):
            ai._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "private-key",
                parse=False, attempt_boundary=boundary,
            )
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        state = GeminiQuotaState.objects.get(pk=boundary.state_id)
        self.assertEqual(attempt.provider_quota_metric, "rpd")
        self.assertEqual(
            attempt.provider_quota_id,
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        )
        self.assertEqual(attempt.provider_quota_dimensions["model"], "gemini-3.7-flash")
        self.assertNotIn("project", attempt.provider_quota_dimensions)
        self.assertEqual(attempt.provider_retry_after_seconds, 44)
        self.assertTrue(state.external_usage_suspected)
        self.assertNotIn("private provider body", json.dumps(state.provider_blocks))

    @override_settings(**SHADOW)
    def test_minute_and_topup_429_without_retryinfo_get_safe_durable_defaults(self):
        now = timezone.now()
        cases = (
            ("minute", ai.gemini_keys.DEFAULT_MINUTE_COOLDOWN),
            ("topup", ai.gemini_keys.TOPUP_COOLDOWN_SECONDS),
        )
        for index, (scope, expected_seconds) in enumerate(cases, start=1):
            error = ai._Gemini429(scope=scope, retry_after_seconds=0)
            parsed_scope, seconds = ai._quota_scope_and_retry(error)
            self.assertEqual((parsed_scope, seconds), (scope, expected_seconds))
            alias = f"GEMINI_API{index + 2}"
            ai.gemini_keys.mark_429(
                alias,
                parsed_scope,
                seconds,
                now=now,
                model="gemini-3.7-flash",
            )
            key_state = ai.gemini_keys.GeminiKeyState.get(alias)
            if scope == "minute":
                until = ai.gemini_keys._model_cooldown_until(
                    key_state, "gemini-3.7-flash"
                )
            else:
                until = key_state.cooldown_until
            self.assertGreaterEqual(
                (until - now).total_seconds(), expected_seconds - 1
            )

            observer = runtime.begin_request(
                request_id=uuid_for_test(),
                role="management",
                reasoning_task="customer_intelligence",
                candidate_plan=_raw_plan(),
                lane="analysis",
            )
            boundary = observer.attempt(
                key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
            )
            boundary.before_provider(serialized_bytes=100, inline_count=0)
            boundary.failed(error)
            attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
            self.assertEqual(
                attempt.provider_retry_after_seconds,
                expected_seconds,
            )
            self.assertGreaterEqual(
                (attempt.provider_block_until - attempt.finished_at).total_seconds(),
                expected_seconds - 1,
            )

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_one_request_has_one_immutable_winner(self, _post):
        plan = _raw_plan()
        plan.append({
            "candidate_index": 2,
            "key_name": "GEMINI_API2",
            "project_identity": "gemini-project-2",
            "identity_status": "known",
            "model": "gemini-3.7-flash",
            "skip_reason": "",
        })
        observer = runtime.begin_request(
            request_id=uuid_for_test(), role="chat", reasoning_task="customer_chat",
            candidate_plan=plan, lane="live",
        )
        boundaries = [
            observer.attempt(key_name=alias, model="gemini-3.7-flash", candidate_index=index)
            for index, alias in ((1, "GEMINI_API"), (2, "GEMINI_API2"))
        ]
        ai._gemini_call_once(
            "gemini-3.7-flash", {"contents": []}, "private-key",
            parse=False, attempt_boundary=boundaries[0],
        )
        with self.assertRaises(ai._GeminiAdmissionRejected):
            ai._gemini_call_once(
                "gemini-3.7-flash", {"contents": []}, "private-key",
                parse=False, attempt_boundary=boundaries[1],
            )
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual(graph.winner_attempt_id, boundaries[0].attempt_id)
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(request_graph=graph, winner_claimed=True).count(),
            1,
        )

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_reply_link_rejects_conflict_and_writes_bounded_audit(self, _post):
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        ai._gemini_call_once(
            "gemini-3.7-flash", {"contents": []}, "key", parse=False,
            attempt_boundary=boundary,
        )
        self.assertTrue(runtime.link_reply_if_present(
            request_id=observer.request_id, reply_message_id=77
        ))
        self.assertTrue(runtime.link_reply_if_present(
            request_id=observer.request_id, reply_message_id=77
        ))
        self.assertFalse(runtime.link_reply_if_present(
            request_id=observer.request_id, reply_message_id=88
        ))
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        self.assertEqual(graph.reply_message_id, 77)
        self.assertEqual(attempt.reply_message_id, 77)
        audit = AdminAuditLog.objects.get(
            action="ig_gemini.reply_link_conflict"
        )
        self.assertEqual(audit.before, {"reply_message_id": 77})
        self.assertEqual(audit.after, {"requested_reply_message_id": 88})
        self.assertNotIn("key", json.dumps(audit.before))

    @override_settings(**SHADOW)
    def test_pre_provider_observer_reads_are_bounded(self):
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        with CaptureQueriesContext(connection) as captured:
            boundary.before_provider(serialized_bytes=120, inline_count=0)
        reads = [q for q in captured if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertLessEqual(len(reads), 6, [q["sql"] for q in reads])

    @override_settings(**SHADOW)
    def test_selected_idle_state_rotates_to_newest_effective_profile_bounded(self):
        now = timezone.now()
        old_profile = GeminiQuotaProfile.objects.create(
            profile_version="shadow-profile-old",
            model="gemini-3.7-flash",
            rpm_limit=5,
            input_tpm_limit=250_000,
            rpd_limit=20,
            permit_limit=1,
            estimator_version="old-estimator",
            source=GeminiQuotaProfile.Source.ADMIN,
            observed_at=now - dt.timedelta(days=2),
            effective_from=now - dt.timedelta(days=2),
        )
        new_profile = GeminiQuotaProfile.objects.create(
            profile_version="shadow-profile-new",
            model="gemini-3.7-flash",
            rpm_limit=6,
            input_tpm_limit=260_000,
            rpd_limit=21,
            permit_limit=1,
            estimator_version="new-estimator",
            source=GeminiQuotaProfile.Source.ADMIN,
            observed_at=now - dt.timedelta(minutes=2),
            effective_from=now - dt.timedelta(minutes=1),
        )
        state = GeminiQuotaState.objects.create(
            project_identity="gemini-project-1",
            model="gemini-3.7-flash",
            quota_profile=old_profile,
            pacific_day=now.astimezone(runtime.PT).date(),
        )
        observer = self._observer()
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual(graph.quota_profile_version, new_profile.profile_version)
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        with CaptureQueriesContext(connection) as captured:
            boundary.before_provider(serialized_bytes=100, inline_count=0)
        reads = [q for q in captured if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertLessEqual(len(reads), 6, [q["sql"] for q in reads])
        state.refresh_from_db()
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        self.assertEqual(state.quota_profile_id, new_profile.pk)
        self.assertEqual(attempt.quota_profile_id, new_profile.pk)
        self.assertTrue(AdminAuditLog.objects.filter(
            action="ig_gemini.quota_profile_rotated",
            entity_id=str(state.pk),
            before__profile_version=old_profile.profile_version,
            after__profile_version=new_profile.profile_version,
        ).exists())

    @override_settings(**SHADOW)
    def test_inflight_state_keeps_old_profile_while_graph_reports_active_policy(self):
        now = timezone.now()
        old_profile = GeminiQuotaProfile.objects.create(
            profile_version="shadow-inflight-old",
            model="gemini-3.7-flash",
            rpm_limit=5,
            input_tpm_limit=250_000,
            rpd_limit=20,
            permit_limit=1,
            estimator_version="old-estimator",
            source=GeminiQuotaProfile.Source.ADMIN,
            observed_at=now - dt.timedelta(days=2),
            effective_from=now - dt.timedelta(days=2),
        )
        new_profile = GeminiQuotaProfile.objects.create(
            profile_version="shadow-inflight-new",
            model="gemini-3.7-flash",
            rpm_limit=6,
            input_tpm_limit=260_000,
            rpd_limit=21,
            permit_limit=1,
            estimator_version="new-estimator",
            source=GeminiQuotaProfile.Source.ADMIN,
            observed_at=now - dt.timedelta(minutes=2),
            effective_from=now - dt.timedelta(minutes=1),
        )
        state = GeminiQuotaState.objects.create(
            project_identity="gemini-project-1",
            model="gemini-3.7-flash",
            quota_profile=old_profile,
            pacific_day=now.astimezone(runtime.PT).date(),
            in_flight_count=1,
        )
        observer = self._observer()
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual(graph.quota_profile_version, new_profile.profile_version)
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        boundary.before_provider(serialized_bytes=100, inline_count=0)
        state.refresh_from_db()
        attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        self.assertEqual(state.quota_profile_id, old_profile.pk)
        self.assertEqual(attempt.quota_profile_id, old_profile.pk)
        self.assertFalse(AdminAuditLog.objects.filter(
            action="ig_gemini.quota_profile_rotated",
            entity_id=str(state.pk),
        ).exists())

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_live_uses_the_precreated_attempt_instead_of_double_counting(self, _post):
        out = ai.gemini_generate_text(
            {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            role="chat",
            model_chain_override=["gemini-3.5-flash-lite"],
        )
        self.assertEqual(out["parsed"], "ok")
        graph = GeminiRequest.objects.get()
        self.assertEqual(out["meta"]["request_id"], graph.request_id)
        provider_rows = GeminiRequestAttempt.objects.filter(
            request_id=graph.request_id,
            outcome="succeeded",
        )
        self.assertEqual(provider_rows.count(), 1)
        self.assertEqual(provider_rows.get().request_graph_id, graph.pk)
        self.assertEqual(provider_rows.get().fsm_state, GeminiRequestAttempt.FsmState.SUCCEEDED)
        encoded = json.dumps(graph.candidate_plan)
        self.assertNotIn("GEMINI_API", encoded)
        self.assertNotIn("shadow-key", encoded)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, {**KEY_ENV, "GEMINI_API6": ""}, clear=False)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_semantic_repair_reuses_candidate_and_accounts_failed_usage(self, post):
        from management.services.ig_provider_dispatch_budget import ValidationDecision

        def response(text, *, prompt_tokens, candidate_tokens):
            return _Response(payload={
                "candidates": [{
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": text}]},
                }],
                "usageMetadata": {
                    "promptTokenCount": prompt_tokens,
                    "candidatesTokenCount": candidate_tokens,
                    "totalTokenCount": prompt_tokens + candidate_tokens,
                },
            })

        post.side_effect = [
            response("invalid", prompt_tokens=17, candidate_tokens=5),
            response("valid", prompt_tokens=19, candidate_tokens=4),
        ]

        result = ai.gemini_generate_text(
            {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            role="chat",
            model_chain_override=["gemini-3.5-flash-lite"],
            result_validator=lambda parsed, *, usage: ValidationDecision(
                valid=parsed == "valid",
                reason_codes=() if parsed == "valid" else ("unverified_price",),
            ),
            repair_payload_factory=lambda payload, _parsed, _reasons: payload,
            max_actual_dispatches=2,
        )

        graph = GeminiRequest.objects.get(request_id=result["meta"]["request_id"])
        attempts = list(
            GeminiRequestAttempt.objects.filter(
                request_graph=graph,
                provider_started_at__isnull=False,
            ).order_by("attempt_index")
        )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].candidate_index, attempts[1].candidate_index)
        self.assertLess(attempts[0].attempt_index, attempts[1].attempt_index)
        self.assertEqual(attempts[0].fsm_state, GeminiRequestAttempt.FsmState.FAILED)
        self.assertEqual(attempts[0].failure_kind, "invalid_response")
        self.assertEqual(attempts[0].prompt_tokens, 17)
        self.assertEqual(attempts[0].candidates_tokens, 5)
        self.assertEqual(attempts[0].total_tokens, 22)
        self.assertFalse(attempts[0].winner_claimed)
        self.assertEqual(attempts[1].fsm_state, GeminiRequestAttempt.FsmState.SUCCEEDED)
        self.assertEqual(graph.winner_attempt_id, attempts[1].pk)
        self.assertEqual(result["parsed"], "valid")

    @override_settings(**SHADOW)
    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "frozen-key-1",
            "GEMINI_API2": "frozen-key-2",
            "GEMINI_API3": "frozen-key-3",
            "GEMINI_API4": "",
            "GEMINI_API5": "",
            "GEMINI_API6": "",
        },
        clear=False,
    )
    @patch("management.services.call_ai_analysis.gemini_quota.settle")
    @patch("management.services.call_ai_analysis.gemini_quota.try_reserve", return_value=True)
    @patch("management.services.call_ai_analysis.gemini_scoreboard.order_candidates")
    @patch("management.services.call_ai_analysis._gemini_call_once")
    def test_reordered_plan_indexes_match_attempt_and_winner_order(
        self,
        call_once,
        order,
        _reserve,
        _settle,
    ):
        order.side_effect = lambda candidates, **_kwargs: list(reversed(candidates))
        outcomes = iter([
            ai._Gemini429(scope="minute", retry_after_seconds=30),
            ("ok", {}),
        ])

        def provider(*_args, **kwargs):
            boundary = kwargs["attempt_boundary"]
            boundary.before_provider(serialized_bytes=128)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                boundary.failed(outcome)
                raise outcome
            boundary.succeeded(outcome[1])
            return outcome

        call_once.side_effect = provider

        out = ai.gemini_generate_text(
            {"contents": [{"parts": [{"text": "bounded"}]}]},
            role="chat",
            model_chain_override=["gemini-3.5-flash-lite"],
        )

        self.assertEqual(out["parsed"], "ok")
        graph = GeminiRequest.objects.get()
        self.assertEqual(
            [row["project_identity"] for row in graph.candidate_plan[:3]],
            ["gemini-project-3", "gemini-project-2", "gemini-project-1"],
        )
        attempts = list(
            GeminiRequestAttempt.objects.filter(request_graph=graph).order_by(
                "attempt_index"
            )
        )
        provider = [row for row in attempts if row.provider_started_at]
        self.assertEqual(
            [(row.project_identity, row.candidate_index) for row in provider],
            [("gemini-project-3", 1), ("gemini-project-2", 2)],
        )
        self.assertEqual(graph.winner_attempt.candidate_index, 2)
        self.assertEqual(
            GeminiRequestAttempt.objects.get(
                request_graph=graph,
                candidate_index=3,
            ).not_attempted_reason,
            "winner_found",
        )
        encoded = json.dumps(graph.candidate_plan, sort_keys=True)
        self.assertNotIn("GEMINI_API", encoded)
        self.assertNotIn("frozen-key", encoded)

    @override_settings(**SHADOW)
    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "sla-key-1",
            "GEMINI_API2": "sla-key-2",
            "GEMINI_API3": "sla-key-3",
            "GEMINI_API4": "",
            "GEMINI_API5": "",
            "GEMINI_API6": "",
        },
        clear=False,
    )
    @patch("management.services.call_ai_analysis.gemini_quota.try_reserve", return_value=True)
    @patch("management.services.call_ai_analysis.gemini_scoreboard.order_candidates")
    @patch("management.services.call_ai_analysis._gemini_call_once")
    def test_reordered_timeout_budget_records_exact_skipped_remainder(
        self,
        call_once,
        order,
        _reserve,
    ):
        order.side_effect = lambda candidates, **_kwargs: list(reversed(candidates))
        outcomes = iter([
            ai._GeminiTransient("first slow"),
            ai._GeminiTransient("second slow"),
        ])

        def provider(*_args, **kwargs):
            boundary = kwargs["attempt_boundary"]
            boundary.before_provider(serialized_bytes=128)
            outcome = next(outcomes)
            boundary.failed(outcome)
            raise outcome

        call_once.side_effect = provider

        with self.assertRaises(ai.CallAIAnalysisError):
            ai.gemini_generate_text(
                {"contents": [{"parts": [{"text": "bounded"}]}]},
                role="chat",
                model_chain_override=["gemini-3.7-flash"],
            )

        graph = GeminiRequest.objects.get()
        attempts = list(
            GeminiRequestAttempt.objects.filter(request_graph=graph).order_by(
                "attempt_index"
            )
        )
        provider = [row for row in attempts if row.provider_started_at]
        self.assertEqual(
            [(row.project_identity, row.candidate_index) for row in provider],
            [("gemini-project-3", 1), ("gemini-project-2", 2)],
        )
        skipped = GeminiRequestAttempt.objects.get(
            request_graph=graph,
            candidate_index=3,
        )
        self.assertEqual(skipped.project_identity, "gemini-project-1")
        self.assertEqual(skipped.not_attempted_reason, "sla_model_budget")
        self.assertEqual(graph.terminal_reason, "exhausted")

    @override_settings(**SHADOW)
    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "recovery-key-1",
            "GEMINI_API2": "recovery-key-2",
            "GEMINI_API3": "",
            "GEMINI_API4": "",
            "GEMINI_API5": "",
            "GEMINI_API6": "",
        },
        clear=False,
    )
    @patch("management.services.call_ai_analysis.gemini_quota.settle")
    @patch("management.services.call_ai_analysis.gemini_quota.try_reserve", return_value=True)
    @patch("management.services.call_ai_analysis.gemini_scoreboard.order_candidates")
    @patch("management.services.call_ai_analysis._gemini_call_once")
    def test_recovery_graph_uses_same_frozen_execution_order(
        self,
        _call_once,
        order,
        _reserve,
        _settle,
    ):
        from management.models import IgClient, InstagramBotMessage

        order.side_effect = lambda candidates, **_kwargs: list(reversed(candidates))

        def provider(*_args, **kwargs):
            boundary = kwargs["attempt_boundary"]
            boundary.before_provider(serialized_bytes=128)
            boundary.succeeded({})
            return "ok", {}

        _call_once.side_effect = provider
        client = IgClient.get_or_create_for_sender("recovery-frozen-order")
        source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="recovery-frozen-order-mid",
            status=InstagramBotMessage.Status.FAILED,
        )
        with turn_lineage(
            lane=Lane.RECOVERY,
            client_id=client.pk,
            source_message_id=source.pk,
            logical_turn_id=f"t{client.pk}:{source.pk}",
            recovery_job_id=99,
        ):
            out = ai.gemini_generate_text(
                {"contents": [{"parts": [{"text": "bounded"}]}]},
                role="chat",
                model_chain_override=["gemini-3.5-flash-lite"],
            )

        self.assertEqual(out["parsed"], "ok")
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.lane, Lane.RECOVERY)
        self.assertEqual(graph.recovery_job_id, 99)
        self.assertEqual(graph.candidate_plan[0]["project_identity"], "gemini-project-2")
        self.assertEqual(graph.winner_attempt.candidate_index, 1)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, {key: "" for key in KEY_ENV}, clear=False)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_unknown_live_custom_is_in_plan_but_never_becomes_a_seventh_state(self, _post):
        out = ai.gemini_generate_text(
            {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
            role="chat",
            manual_key="unknown-custom-key",
            model_chain_override=["gemini-3.5-flash-lite"],
        )
        self.assertEqual(out["parsed"], "ok")
        graph = GeminiRequest.objects.get()
        self.assertEqual(len(graph.candidate_plan), 7)
        self.assertTrue(any(
            item["identity_status"] == "unknown"
            and item["model"] == "gemini-3.5-flash-lite"
            for item in graph.candidate_plan
        ))
        attempt = GeminiRequestAttempt.objects.get(
            request_graph=graph,
            provider_started_at__isnull=False,
        )
        self.assertEqual(attempt.project_identity, "")
        self.assertIsNone(attempt.quota_profile_id)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM=SHADOW_FROM,
        GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY="shadow-test-hmac-key",
        GEMINI_KEY_PROJECT_GROUPS={},
    )
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_default_project_labels_are_assumed_not_quota_identities(self, _post):
        ai.gemini_generate_text(
            {"contents": [{"parts": [{"text": "memory"}]}]},
            role="management",
            reasoning_task="memory_summary",
        )
        graph = GeminiRequest.objects.get()
        self.assertTrue(any(
            row["identity_status"] == "assumed" for row in graph.candidate_plan
        ))
        attempt = GeminiRequestAttempt.objects.get(
            request_graph=graph,
            provider_started_at__isnull=False,
        )
        self.assertEqual(attempt.project_identity, "")
        self.assertIsNone(attempt.quota_profile_id)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_generic_memory_and_call_tasks_share_the_observed_gateway(self, _post):
        for task in ("memory_summary", "conversation_reanalysis", "customer_intelligence"):
            ai.gemini_generate_text(
                {"contents": [{"parts": [{"text": "bounded"}]}]},
                role="management",
                reasoning_task=task,
            )
        self.assertEqual(
            set(GeminiRequest.objects.values_list("reasoning_task", flat=True)),
            {"memory_summary", "conversation_reanalysis", "customer_intelligence"},
        )
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                request_graph__isnull=False,
                provider_started_at__isnull=False,
            ).count(),
            3,
        )

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_generic_manual_plan_is_first_and_all_skips_are_graph_linked(self, post):
        post.side_effect = [
            _Response(429, {
                "error": {
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "requests per minute",
                },
            }),
            _Response(),
        ]
        out = ai.gemini_generate_text(
            {"contents": [{"parts": [{"text": "memory"}]}]},
            role="management",
            manual_key="unknown-manual-key",
            reasoning_task="memory_summary",
        )
        self.assertEqual(out["parsed"], "ok")
        graph = GeminiRequest.objects.get()
        self.assertEqual(
            [row["model"] for row in graph.candidate_plan[:3]],
            [
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3.5-flash-lite",
            ],
        )
        attempts = list(
            GeminiRequestAttempt.objects.filter(request_graph=graph).order_by(
                "attempt_index"
            )
        )
        self.assertEqual(attempts[0].key_name, "(manual)")
        self.assertEqual(attempts[0].candidate_index, 1)
        self.assertTrue(all(row.request_graph_id == graph.pk for row in attempts))
        self.assertEqual(
            {row.candidate_index for row in attempts},
            {row["candidate_index"] for row in graph.candidate_plan},
        )
        self.assertEqual(
            len(graph.candidate_outcomes),
            len(graph.candidate_plan),
        )
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                request_id=graph.request_id,
                request_graph__isnull=True,
            ).count(),
            0,
        )

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    def test_shadow_manual_failure_keeps_fresh_legacy_env_sequence(self):
        payload = {"contents": [{"parts": [{"text": "memory"}]}]}

        def run(mode):
            calls = []

            def fake_once(model, _payload, key, **_kwargs):
                calls.append((model, key))
                if key == "manual-key":
                    raise ai._Gemini429(scope="minute")
                return "ok", {}

            with override_settings(
                **{
                    **SHADOW,
                    "GEMINI_ACCOUNTING_V2_MODE": mode,
                }
            ), patch.object(ai, "_gemini_call_once", side_effect=fake_once):
                ai.gemini_generate_text(
                    payload,
                    role="management",
                    manual_key="manual-key",
                    reasoning_task="memory_summary",
                )
            return calls

        off_calls = run("off")
        GeminiRequestAttempt.objects.all().delete()
        GeminiRequest.objects.all().delete()
        GeminiQuotaState.objects.all().delete()
        GeminiModelQuotaUsage.objects.all().delete()
        GeminiKeyState.objects.all().delete()
        GeminiModelState.objects.all().delete()
        ai.gemini_keys.clear_model_overload()
        ai.gemini_keys.clear_model_unavailable()
        shadow_calls = run("shadow")
        self.assertEqual(shadow_calls, off_calls)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_call_audio_wrapper_uses_one_profiled_shadow_attempt(self, post):
        post.return_value = _Response(payload={
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": '{"overall_score": 1}'}]},
            }],
            "usageMetadata": {"promptTokenCount": 20, "totalTokenCount": 24},
        })
        out = ai._gemini_analyze(b"bounded-audio", "audio/mpeg", "", "")
        self.assertEqual(out["parsed"]["overall_score"], 1)
        graph = GeminiRequest.objects.get()
        attempt = GeminiRequestAttempt.objects.get(
            request_graph=graph, provider_started_at__isnull=False
        )
        self.assertEqual(graph.reasoning_task, "customer_intelligence")
        self.assertEqual(graph.lane, "analysis")
        self.assertIsNotNone(attempt.quota_profile_id)
        self.assertEqual(attempt.fsm_state, GeminiRequestAttempt.FsmState.SUCCEEDED)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post")
    def test_checker_is_graphed_without_inventing_25_profile(self, post):
        post.return_value = _Response(payload={
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": '{"score": 1}'}]},
            }],
            "usageMetadata": {"promptTokenCount": 4, "totalTokenCount": 8},
        })
        ai.gemini_generate_grounded("system", "user")
        graph = GeminiRequest.objects.get()
        attempt = GeminiRequestAttempt.objects.get(
            request_graph=graph, provider_started_at__isnull=False
        )
        self.assertEqual(graph.reasoning_task, "conversion_analysis")
        self.assertEqual(attempt.role, "checker")
        self.assertIsNone(attempt.quota_profile_id)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_recovery_lineage_is_preserved(self, _post):
        from management.models import IgClient, InstagramBotMessage

        decision = classify_live_turn(TurnFacts(), settings_obj=None)
        client = IgClient.get_or_create_for_sender("recovery-lineage")
        source = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="recover",
            mid="recovery-lineage-mid",
            status=InstagramBotMessage.Status.FAILED,
        )
        with turn_lineage(
            lane=Lane.RECOVERY,
            client_id=client.pk,
            source_message_id=source.pk,
            logical_turn_id=f"t{client.pk}:{source.pk}",
            recovery_job_id=11,
        ):
            ai.gemini_generate_text(
                {"contents": [{"parts": [{"text": "recover"}]}]},
                role="chat",
                model_chain_override=["gemini-3.5-flash-lite"],
                routing_decision=decision,
            )
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.lane, "recovery")
        self.assertEqual(graph.client_id, client.pk)
        self.assertEqual(graph.source_message_id, source.pk)
        self.assertEqual(graph.recovery_job_id, 11)

    @override_settings(**SHADOW)
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.gemini_probe.requests.post", return_value=_Response())
    def test_quota_consuming_probe_is_diagnostic_generation(self, _post):
        result = gemini_probe.probe_key("gemini-3.7-flash", "shadow-key-1")
        self.assertEqual(result["status"], "ok")
        graph = GeminiRequest.objects.get()
        attempt = GeminiRequestAttempt.objects.get(
            request_graph=graph, provider_started_at__isnull=False
        )
        self.assertEqual(graph.lane, "diagnostic")
        self.assertEqual(attempt.role, "diagnostic")
        self.assertEqual(attempt.fsm_state, GeminiRequestAttempt.FsmState.SUCCEEDED)

    @override_settings(
        GEMINI_ACCOUNTING_V2_MODE="shadow",
        GEMINI_ACCOUNTING_V2_EFFECTIVE_FROM=SHADOW_FROM,
        GEMINI_ACCOUNTING_IDENTITY_HMAC_KEY="probe-shadow-test-key",
        GEMINI_KEY_PROJECT_GROUPS={},
    )
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.services.gemini_probe.requests.post", return_value=_Response())
    def test_probe_default_label_is_assumed_and_creates_no_quota_state(self, _post):
        result = gemini_probe.probe_key("gemini-3.7-flash", "shadow-key-1")
        self.assertEqual(result["status"], "ok")
        graph = GeminiRequest.objects.get()
        attempt = GeminiRequestAttempt.objects.get(
            request_graph=graph, provider_started_at__isnull=False
        )
        self.assertEqual(graph.candidate_plan[0]["identity_status"], "assumed")
        self.assertEqual(attempt.project_identity, "")
        self.assertIsNone(attempt.quota_profile_id)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**SHADOW)
    @patch("management.services.gemini_probe.requests.get")
    def test_metadata_get_never_creates_generation_accounting(self, get):
        response = _Response(payload={"supportedGenerationMethods": ["generateContent"]})
        get.return_value = response
        result = gemini_probe.probe_key_metadata("gemini-3.7-flash", "key")
        self.assertEqual(result["status"], "metadata_ok")
        self.assertEqual(GeminiRequest.objects.count(), 0)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)

    @override_settings(**{**SHADOW, "GEMINI_NONLIVE_ADMISSION_MODE": "enforce"})
    @patch.dict(os.environ, KEY_ENV, clear=False)
    @patch("management.models.GeminiRequest.objects.create", side_effect=DatabaseError("shadow down"))
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_v2_database_failure_blocks_real_generation(self, _post, _create):
        with self.assertRaises(ai.CallAIAnalysisError):
            ai.gemini_generate_text(
                {"contents": [{"parts": [{"text": "hello"}]}]},
                role="management",
                reasoning_task="memory_summary",
            )
        _post.assert_not_called()

    @override_settings(**SHADOW)
    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_reply_link_is_idempotent_and_targets_the_winner(self, _post):
        observer = self._observer()
        boundary = observer.attempt(
            key_name="GEMINI_API", model="gemini-3.7-flash", candidate_index=1
        )
        ai._gemini_call_once(
            "gemini-3.7-flash", {"contents": []}, "key", parse=False,
            attempt_boundary=boundary,
        )
        self.assertTrue(runtime.link_reply_if_present(request_id=observer.request_id, reply_message_id=77))
        self.assertTrue(runtime.link_reply_if_present(request_id=observer.request_id, reply_message_id=77))
        self.assertEqual(GeminiRequest.objects.get(pk=observer.graph_id).reply_message_id, 77)
        self.assertEqual(GeminiRequestAttempt.objects.get(pk=boundary.attempt_id).reply_message_id, 77)


@override_settings(**SHADOW)
class GeminiShadowAnalysisGraphRegressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_shadow_profiles()

    @staticmethod
    def _analysis_candidates():
        return [
            (alias, f"analysis-key-{index}", "gemini-3.6-flash")
            for index, alias in enumerate(
                ("GEMINI_API", "GEMINI_API2", "GEMINI_API3",
                 "GEMINI_API4", "GEMINI_API5", "GEMINI_API6"),
                start=1,
            )
        ]

    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "analysis-key-1",
            "GEMINI_API2": "analysis-key-2",
            "GEMINI_API3": "analysis-key-3",
            "GEMINI_API4": "analysis-key-4",
            "GEMINI_API5": "analysis-key-5",
            "GEMINI_API6": "analysis-key-6",
        },
        clear=False,
    )
    def test_timeout_rotates_to_next_frozen_project_and_terminalizes_winner(self):
        candidates = self._analysis_candidates()
        provider_keys = []

        def call_once(model, _payload, key, **kwargs):
            provider_keys.append(key)
            boundary = kwargs["attempt_boundary"]
            self.assertTrue(boundary.before_provider(serialized_bytes=128))
            if key == "analysis-key-1":
                error = ai._GeminiTransient("timeout: ambiguous provider result")
                boundary.failed(error)
                raise error
            boundary.succeeded({"promptTokenCount": 2, "totalTokenCount": 3})
            return "analysis recovered", {
                "promptTokenCount": 2,
                "totalTokenCount": 3,
            }

        with (
            patch.object(
                ai.gemini_keys,
                "task_model_chain",
                return_value=["gemini-3.6-flash"],
            ),
            patch.object(
                ai.gemini_keys,
                "iter_attempts",
                side_effect=lambda *_args, **_kwargs: iter(candidates),
            ) as iterator,
            patch.object(ai.gemini_keys, "attempts_per_model", return_value=2),
            patch.object(ai.gemini_keys, "max_rounds", return_value=2),
            patch.object(
                ai.gemini_keys,
                "acquire_key_lease",
                side_effect=lambda key_name, **_kwargs: f"lease-{key_name}",
            ),
            patch.object(ai.gemini_keys, "release_key_lease", return_value=True),
            patch.object(ai.gemini_quota, "try_reserve", return_value=True),
            patch.object(ai.gemini_quota, "settle"),
            patch.object(ai, "_gemini_call_once", side_effect=call_once),
        ):
            result = ai._run_with_pool(
                "management",
                {"contents": []},
                deadline_seconds=30,
                reasoning_task="conversation_reanalysis",
            )

        self.assertEqual(result["parsed"], "analysis recovered")
        iterator.assert_called_once_with(
            "management",
            model_chain_override=["gemini-3.6-flash"],
            include_skipped=True,
        )
        self.assertEqual(provider_keys, ["analysis-key-1", "analysis-key-2"])
        graph = GeminiRequest.objects.get()
        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(graph.terminal_reason, "provider_success")
        attempts = GeminiRequestAttempt.objects.filter(request_graph=graph)
        self.assertEqual(
            attempts.filter(candidate_index=1).count(),
            1,
            "one frozen candidate may cross the provider boundary only once",
        )
        timed_out = attempts.get(candidate_index=1)
        self.assertEqual(
            timed_out.fsm_state,
            GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
        )
        winner = attempts.get(candidate_index=2)
        self.assertTrue(winner.winner_claimed)
        self.assertEqual(graph.winner_attempt_id, winner.pk)
        self.assertEqual(
            attempts.filter(
                candidate_index__in=(3, 4, 5, 6),
                outcome="not_attempted",
                not_attempted_reason="winner_found",
            ).count(),
            4,
        )

    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "analysis-key-1",
            "GEMINI_API2": "analysis-key-2",
        },
        clear=False,
    )
    def test_configured_manual_key_is_not_dispatched_twice_under_two_labels(self):
        provider_keys = []

        def call_once(_model, _payload, key, **kwargs):
            provider_keys.append(key)
            boundary = kwargs["attempt_boundary"]
            self.assertTrue(boundary.before_provider(serialized_bytes=64))
            boundary.succeeded({"promptTokenCount": 1, "totalTokenCount": 2})
            return "ok", {"promptTokenCount": 1, "totalTokenCount": 2}

        candidates = [
            ("GEMINI_API", "analysis-key-1", "gemini-3.6-flash"),
            ("GEMINI_API2", "analysis-key-2", "gemini-3.6-flash"),
        ]
        with (
            patch.object(ai.gemini_keys, "manual_key_allowed", return_value=True),
            patch.object(
                ai.gemini_keys,
                "task_model_chain",
                return_value=["gemini-3.6-flash"],
            ),
            patch.object(
                ai.gemini_keys,
                "iter_attempts",
                side_effect=lambda *_args, **_kwargs: iter(candidates),
            ),
            patch.object(
                ai.gemini_keys,
                "acquire_key_lease",
                side_effect=lambda key_name, **_kwargs: f"lease-{key_name}",
            ),
            patch.object(ai.gemini_keys, "release_key_lease", return_value=True),
            patch.object(ai.gemini_quota, "try_reserve", return_value=True),
            patch.object(ai.gemini_quota, "settle"),
            patch.object(ai, "_gemini_call_once", side_effect=call_once),
        ):
            result = ai._run_with_pool(
                "management",
                {"contents": []},
                manual_key="analysis-key-1",
                deadline_seconds=30,
                reasoning_task="conversation_reanalysis",
            )

        self.assertEqual(result["parsed"], "ok")
        self.assertEqual(provider_keys, ["analysis-key-1"])
        graph = GeminiRequest.objects.get()
        self.assertFalse(any(
            row.get("key_name") == "(manual)" for row in graph.candidate_plan
        ))

    @override_settings(IG_GEMINI_ANALYSIS_SINGLE_BOUNDARY_ROTATION=False)
    @patch.object(ai, "_gemini_call_once")
    def test_active_shadow_rejects_unsafe_legacy_retry_configuration(self, provider):
        with self.assertRaisesRegex(
            ai.CallAIAnalysisError,
            "single-boundary",
        ):
            ai._run_with_pool(
                "management",
                {"contents": []},
                deadline_seconds=30,
                reasoning_task="conversation_reanalysis",
            )

        provider.assert_not_called()
        self.assertEqual(GeminiRequest.objects.count(), 0)

    @patch.dict(
        os.environ,
        {
            "GEMINI_API": "analysis-key-1",
            "GEMINI_API2": "analysis-key-2",
            "GEMINI_API3": "analysis-key-3",
            "GEMINI_API4": "analysis-key-4",
            "GEMINI_API5": "analysis-key-5",
            "GEMINI_API6": "analysis-key-6",
        },
        clear=False,
    )
    def test_ownership_exception_terminalizes_graph_and_all_candidates(self):
        candidates = self._analysis_candidates()
        with (
            patch.object(
                ai.gemini_keys,
                "task_model_chain",
                return_value=["gemini-3.6-flash"],
            ),
            patch.object(
                ai.gemini_keys,
                "iter_attempts",
                side_effect=lambda *_args, **_kwargs: iter(candidates),
            ),
            patch.object(ai.gemini_keys, "attempts_per_model", return_value=2),
            patch.object(ai.gemini_keys, "max_rounds", return_value=2),
            patch.object(
                ai.gemini_keys,
                "acquire_key_lease",
                return_value="ownership-lease",
            ),
            patch.object(ai.gemini_keys, "release_key_lease", return_value=True),
            patch.object(
                runtime.RequestObserver,
                "_validate_boundary",
                side_effect=RuntimeError("simulated ownership failure"),
            ),
            patch.object(ai, "_gemini_call_once") as provider,
        ):
            with self.assertRaises(ai.CallAIAnalysisError):
                ai._run_with_pool(
                    "management",
                    {"contents": []},
                    deadline_seconds=30,
                    reasoning_task="conversation_reanalysis",
                )

        provider.assert_not_called()
        graph = GeminiRequest.objects.get()
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertEqual(graph.terminal_reason, "ownership_conflict")
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                request_graph=graph,
                outcome="not_attempted",
                not_attempted_reason="ownership_conflict",
            ).count(),
            6,
        )

    def test_expired_unresolved_graph_is_reconciled_exactly_once(self):
        plan = _raw_plan("gemini-3.6-flash") + [{
            "candidate_index": 2,
            "key_name": "GEMINI_API2",
            "key_value": "must-never-be-persisted",
            "project_identity": "gemini-project-2",
            "identity_status": "known",
            "model": "gemini-3.6-flash",
            "skip_reason": "",
        }]
        observer = runtime.begin_request(
            request_id=uuid_for_test(),
            role="management",
            reasoning_task="conversation_reanalysis",
            candidate_plan=plan,
            deadline_seconds=1,
            lane="analysis",
        )
        boundary = observer.attempt(
            key_name="GEMINI_API",
            model="gemini-3.6-flash",
            candidate_index=1,
        )
        self.assertTrue(boundary.before_provider(serialized_bytes=128))
        boundary.failed(ai._GeminiTransient("timeout: unresolved provider result"))
        timeout_attempt = GeminiRequestAttempt.objects.get(pk=boundary.attempt_id)
        quota_state = GeminiQuotaState.objects.get(pk=boundary.state_id)
        self.assertEqual(
            timeout_attempt.fsm_state,
            GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
        )
        self.assertEqual(quota_state.rpd_uncertain, 1)
        self.assertEqual(quota_state.in_flight_count, 0)
        expired_at = timezone.now() - dt.timedelta(seconds=1)
        GeminiRequest._base_manager.filter(pk=observer.graph_id).update(
            deadline_at=expired_at,
        )

        self.assertEqual(
            runtime.reconcile_expired_request_graphs(now=timezone.now()),
            1,
        )
        self.assertEqual(
            runtime.reconcile_expired_request_graphs(now=timezone.now()),
            0,
        )

        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertEqual(graph.terminal_reason, "expired_reconcile")
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(
                request_graph=graph,
                outcome="not_attempted",
                not_attempted_reason="expired_reconcile",
            ).count(),
            1,
        )
        timeout_attempt.refresh_from_db()
        quota_state.refresh_from_db()
        self.assertEqual(
            timeout_attempt.fsm_state,
            GeminiRequestAttempt.FsmState.TIMEOUT_AMBIGUOUS,
        )
        self.assertEqual(quota_state.rpd_dispatched, 1)
        self.assertEqual(quota_state.rpd_uncertain, 1)
        self.assertFalse(
            GeminiRequestAttempt.objects.filter(
                request_graph=graph,
                fsm_state__in=(
                    GeminiRequestAttempt.FsmState.RESERVED,
                    GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                ),
            ).exists()
        )

    def test_expired_planned_attempt_is_cancelled_before_graph_failure(self):
        now = timezone.now()
        observer = runtime.begin_request(
            request_id=uuid_for_test(),
            role="management",
            reasoning_task="conversation_reanalysis",
            candidate_plan=_raw_plan("gemini-3.6-flash"),
            deadline_seconds=1,
            lane="analysis",
        )
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        attempt = GeminiRequestAttempt.objects.create(
            request_id=graph.request_id,
            request_graph=graph,
            role="management",
            key_name="GEMINI_API",
            project_group="gemini-project-1",
            project_identity="gemini-project-1",
            model="gemini-3.6-flash",
            outcome="planned",
            fsm_state=GeminiRequestAttempt.FsmState.PLANNED,
            accounting_mode="shadow",
            attempt_index=1,
            candidate_index=1,
        )
        GeminiRequest._base_manager.filter(pk=graph.pk).update(
            deadline_at=now - dt.timedelta(seconds=1),
        )

        self.assertEqual(runtime.reconcile_expired_request_graphs(now=now), 1)

        graph.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "failed")
        self.assertEqual(
            attempt.fsm_state,
            GeminiRequestAttempt.FsmState.CANCELLED_PRE_DISPATCH,
        )
        self.assertEqual(attempt.not_attempted_reason, "expired_reconcile")

    def test_reaper_does_not_guess_when_claim_and_reply_evidence_conflict(self):
        now = timezone.now()
        plan = _raw_plan("gemini-3.6-flash") + [{
            "candidate_index": 2,
            "key_name": "GEMINI_API2",
            "key_value": "must-never-be-persisted",
            "project_identity": "gemini-project-2",
            "identity_status": "known",
            "model": "gemini-3.6-flash",
            "skip_reason": "",
        }]
        observer = runtime.begin_request(
            request_id=uuid_for_test(),
            role="management",
            reasoning_task="conversation_reanalysis",
            candidate_plan=plan,
            deadline_seconds=1,
            lane="analysis",
        )
        graph = GeminiRequest.objects.get(pk=observer.graph_id)
        for index in (1, 2):
            GeminiRequestAttempt.objects.create(
                request_id=graph.request_id,
                request_graph=graph,
                role="management",
                key_name=f"GEMINI_API{index if index > 1 else ''}",
                project_group=f"gemini-project-{index}",
                project_identity=f"gemini-project-{index}",
                model="gemini-3.6-flash",
                outcome="succeeded",
                fsm_state=GeminiRequestAttempt.FsmState.SUCCEEDED,
                accounting_mode="shadow",
                attempt_index=index,
                candidate_index=index,
                provider_started_at=now - dt.timedelta(seconds=5),
                dispatch_pacific_day=now.astimezone(runtime.PT).date(),
                finished_at=now - dt.timedelta(seconds=4),
                settled_at=now - dt.timedelta(seconds=4),
                permit_released_at=now - dt.timedelta(seconds=4),
                winner_claimed=index == 1,
                reply_message_id=77 if index == 2 else None,
            )
        GeminiRequest._base_manager.filter(pk=graph.pk).update(
            deadline_at=now - dt.timedelta(seconds=1),
            reply_message_id=77,
        )

        self.assertEqual(runtime.reconcile_expired_request_graphs(now=now), 0)

        graph.refresh_from_db()
        self.assertEqual(graph.terminal_resolution, "")
        self.assertIsNone(graph.winner_attempt_id)

    def test_reaper_waits_for_effective_shadow_gate(self):
        with patch.object(runtime, "shadow_runtime_active", return_value=False):
            self.assertEqual(
                runtime.reconcile_expired_request_graphs(now=timezone.now()),
                0,
            )

    def test_expired_graph_with_success_evidence_is_never_marked_failed(self):
        now = timezone.now()
        plan = _raw_plan("gemini-3.6-flash") + [{
            "candidate_index": 2,
            "key_name": "GEMINI_API2",
            "key_value": "must-never-be-persisted",
            "project_identity": "gemini-project-2",
            "identity_status": "known",
            "model": "gemini-3.6-flash",
            "skip_reason": "",
        }]
        cases = (
            GeminiRequestAttempt.FsmState.SUCCEEDED,
            GeminiRequestAttempt.FsmState.SUCCEEDED_LATE,
        )
        graphs = []
        for index, fsm_state in enumerate(cases, start=1):
            observer = runtime.begin_request(
                request_id=uuid_for_test(),
                role="management",
                reasoning_task="conversation_reanalysis",
                candidate_plan=plan,
                deadline_seconds=1,
                lane="analysis",
            )
            graph = GeminiRequest.objects.get(pk=observer.graph_id)
            attempt = GeminiRequestAttempt.objects.create(
                request_id=graph.request_id,
                request_graph=graph,
                role="management",
                key_name="GEMINI_API",
                project_group="gemini-project-1",
                project_identity="gemini-project-1",
                model="gemini-3.6-flash",
                outcome="succeeded",
                fsm_state=fsm_state,
                accounting_mode="shadow",
                attempt_index=1,
                candidate_index=1,
                provider_started_at=now - dt.timedelta(seconds=5),
                dispatch_pacific_day=now.astimezone(runtime.PT).date(),
                finished_at=now - dt.timedelta(seconds=4),
                settled_at=now - dt.timedelta(seconds=4),
                permit_released_at=now - dt.timedelta(seconds=4),
            )
            GeminiRequest._base_manager.filter(pk=graph.pk).update(
                deadline_at=now - dt.timedelta(seconds=index),
            )
            graphs.append((graph.pk, attempt.pk))

        self.assertEqual(
            runtime.reconcile_expired_request_graphs(now=now),
            len(cases),
        )
        self.assertEqual(runtime.reconcile_expired_request_graphs(now=now), 0)

        for graph_id, attempt_id in graphs:
            with self.subTest(graph_id=graph_id):
                graph = GeminiRequest.objects.get(pk=graph_id)
                attempt = GeminiRequestAttempt.objects.get(pk=attempt_id)
                self.assertEqual(graph.terminal_resolution, "succeeded")
                self.assertEqual(
                    graph.terminal_reason,
                    "reconciled_success_evidence",
                )
                self.assertEqual(graph.winner_attempt_id, attempt.pk)
                self.assertTrue(attempt.winner_claimed)
                self.assertEqual(
                    graph.candidate_outcomes["1"]["outcome"],
                    "succeeded",
                )
                self.assertEqual(
                    GeminiRequestAttempt.objects.filter(
                        request_graph=graph,
                        candidate_index=2,
                        outcome="not_attempted",
                        not_attempted_reason="winner_found",
                    ).count(),
                    1,
                )
                self.assertFalse(
                    GeminiRequestAttempt.objects.filter(
                        request_graph=graph,
                        not_attempted_reason="expired_reconcile",
                    ).exists()
                )


@override_settings(**SHADOW)
class GeminiSourceLaneConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        seed_shadow_profiles()

    @patch("management.services.call_ai_analysis.requests.post", return_value=_Response())
    def test_two_callers_create_one_graph_and_only_canonical_dispatches(self, post):
        from management.models import IgClient, InstagramBotMessage
        from management.services.gemini_routing import persist_decision

        client = IgClient.get_or_create_for_sender("source-lane-thread-race")
        message = InstagramBotMessage.objects.create(
            sender_id=client.igsid,
            client=client,
            role=InstagramBotMessage.Role.USER,
            text="bounded",
            mid="source-lane-thread-race-mid",
            status=InstagramBotMessage.Status.PROCESSING,
        )
        ordinary = classify_live_turn(TurnFacts())
        persist_decision(message, ordinary)
        plan = _raw_plan("gemini-3.5-flash-lite")
        start = Barrier(2)

        def worker(index):
            close_old_connections()
            try:
                with turn_lineage(
                    lane=Lane.LIVE,
                    client_id=client.pk,
                    source_message_id=message.pk,
                    logical_turn_id=f"t{client.pk}:{message.pk}",
                ):
                    start.wait(timeout=10)
                    observer = runtime.begin_request(
                        request_id=f"source-lane-thread-race-{index}",
                        role="chat",
                        reasoning_task="customer_chat",
                        candidate_plan=plan,
                        deadline_seconds=35,
                        routing_decision=ordinary,
                    )
                boundary = observer.attempt(
                    key_name="GEMINI_API",
                    model="gemini-3.5-flash-lite",
                    candidate_index=1,
                )
                try:
                    ai._gemini_call_once(
                        "gemini-3.5-flash-lite",
                        {"contents": [{"parts": [{"text": "bounded"}]}]},
                        "private-key",
                        parse=False,
                        attempt_boundary=boundary,
                    )
                except ai._GeminiAdmissionRejected:
                    return "blocked", bool(boundary.admitted)
                return "canonical", bool(boundary.admitted)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, (1, 2)))

        self.assertEqual(
            sorted(outcomes),
            [("blocked", False), ("canonical", True)],
        )
        self.assertEqual(post.call_count, 1)
        graph = GeminiRequest.objects.get(
            source_message_id=message.pk,
            lane=Lane.LIVE,
        )
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self.assertEqual(
            GeminiRequestAttempt.objects.filter(request_graph=graph).count(),
            1,
        )


def uuid_for_test():
    import uuid

    return uuid.uuid4().hex
