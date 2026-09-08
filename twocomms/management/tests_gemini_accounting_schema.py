import datetime
import importlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
from unittest.mock import Mock

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from management.models import (
    AdminAuditLog,
    GeminiModelQuotaUsage,
    GeminiQuotaProfile,
    GeminiQuotaState,
    GeminiRequest,
    GeminiRequestAttempt,
)
from management.services.gemini_accounting_contract import (
    ATTEMPT_IMMUTABLE_FIELDS,
    ATTEMPT_MUTABLE_FIELDS,
    REQUEST_IMMUTABLE_FIELDS,
    REQUEST_MUTABLE_FIELDS,
    canonical_candidate_plan_digest,
    rotate_quota_state_profile,
)
from management.services.ig_engine_health import IG_RUNTIME_TABLES


PROFILE_VERSION = "owner-observed-2026-08-29.v1"
CALIBRATED_PROFILE_VERSION = "production-observed-2026-08-31.v2"
CALIBRATED_AT = datetime.datetime(
    2026, 8, 31, 17, 0, 14, tzinfo=datetime.timezone.utc
)
EXPECTED_PROFILES = {
    "gemini-3.7-flash": (5, 250_000, 20, 1),
    "gemini-3.6-flash": (5, 250_000, 20, 1),
    "gemini-3.5-flash": (5, 250_000, 20, 1),
    "gemini-3.5-flash-lite": (15, 250_000, 500, 2),
}


class GeminiAccountingSchemaTests(TestCase):
    def _new_profile(
        self,
        version,
        *,
        model="gemini-3.7-flash",
        effective_from=None,
        effective_until=None,
    ):
        now = timezone.now()
        return GeminiQuotaProfile.objects.create(
            profile_version=version,
            model=model,
            rpm_limit=5,
            input_tpm_limit=250_000,
            rpd_limit=20,
            permit_limit=1,
            estimator_version="test",
            source=GeminiQuotaProfile.Source.ADMIN,
            observed_at=now,
            effective_from=effective_from or now - datetime.timedelta(minutes=1),
            effective_until=effective_until,
        )

    def _identity_request(self, suffix="identity"):
        plan = [{
            "candidate_index": 1,
            "project_identity": "project-1",
            "model": "gemini-3.7-flash",
        }]
        return GeminiRequest.objects.create(
            request_id=f"request-{suffix}",
            lane="live",
            task_class="complex_live",
            reasoning_task="media_analysis",
            logical_turn_id=f"turn-{suffix}",
            source_message_id=101,
            client_id=202,
            recovery_job_id=303,
            routing_policy_version="routing-v2",
            accounting_policy_version="accounting-v2",
            quota_profile_version=PROFILE_VERSION,
            authority_snapshot_version="authority-v1",
            routing_mode="adaptive",
            commercial_risk="medium",
            requires_media_reasoning=True,
            candidate_plan=plan,
            candidate_plan_digest=canonical_candidate_plan_digest(plan),
            deadline_ms=45_000,
            deadline_at=timezone.now() + datetime.timedelta(seconds=45),
            accounting_mode=GeminiRequest.AccountingMode.OFF,
        )

    def _identity_attempt(self, request, suffix="identity"):
        profile = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.7-flash",
        )
        return GeminiRequestAttempt.objects.create(
            request_id=request.request_id,
            request_graph=request,
            role="chat",
            key_name="GEMINI_API",
            project_group="project-1",
            project_identity="project-1",
            model=profile.model,
            outcome="planned",
            fsm_state=GeminiRequestAttempt.FsmState.PLANNED,
            quota_profile=profile,
            accounting_mode="off",
            logical_turn_id=request.logical_turn_id,
            source_message_id=request.source_message_id,
            client_id=request.client_id,
            lane=request.lane,
            attempt_index=1,
            candidate_index=1,
            incident_id=404,
            recovery_job_id=request.recovery_job_id,
        )

    def test_migration_seeds_only_four_non_secret_profiles(self):
        profiles = {
            row.model: (
                row.rpm_limit,
                row.input_tpm_limit,
                row.rpd_limit,
                row.permit_limit,
            )
            for row in GeminiQuotaProfile.objects.filter(
                profile_version=PROFILE_VERSION
            )
        }

        self.assertEqual(profiles, EXPECTED_PROFILES)
        for row in GeminiQuotaProfile.objects.filter(profile_version=PROFILE_VERSION):
            self.assertEqual(row.source, GeminiQuotaProfile.Source.OWNER_OBSERVED)
            self.assertEqual(row.source_reference, "owner_ai_studio_screenshot")
            self.assertEqual(row.estimator_version, "shadow-calibration-required")

    def test_profile_seed_creates_zero_usage_state_or_request_rows(self):
        """S3a seeds policy only; it cannot claim that any project used quota."""
        self.assertEqual(GeminiQuotaProfile.objects.count(), 8)
        self.assertEqual(GeminiQuotaState.objects.count(), 0)
        self.assertEqual(GeminiRequest.objects.count(), 0)
        self.assertEqual(GeminiRequestAttempt.objects.count(), 0)
        self.assertEqual(GeminiModelQuotaUsage.objects.count(), 0)

    def test_request_attempt_graph_and_winner_fk_are_additive(self):
        profile = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.7-flash",
        )
        candidate_plan = [{
            "candidate_index": 1,
            "project_identity": "project-1",
            "model": profile.model,
        }]
        request = GeminiRequest.objects.create(
            request_id="schema-request-1",
            lane="live",
            task_class="complex_live",
            candidate_plan=candidate_plan,
            candidate_plan_digest=canonical_candidate_plan_digest(candidate_plan),
        )
        attempt = GeminiRequestAttempt.objects.create(
            request_id=request.request_id,
            request_graph=request,
            role="chat",
            key_name="",
            project_identity="project-1",
            model=profile.model,
            outcome="planned",
            fsm_state=GeminiRequestAttempt.FsmState.PLANNED,
            quota_profile=profile,
            attempt_index=1,
            candidate_index=1,
        )
        request.winner_attempt = attempt
        request.save(update_fields=["winner_attempt", "updated_at"])

        request.refresh_from_db()
        self.assertEqual(request.winner_attempt_id, attempt.pk)
        self.assertEqual(list(request.attempts.values_list("pk", flat=True)), [attempt.pk])

    def test_attempt_index_is_unique_inside_one_v2_request(self):
        request = GeminiRequest.objects.create(request_id="schema-request-unique")
        GeminiRequestAttempt.objects.create(
            request_id=request.request_id,
            request_graph=request,
            role="chat",
            key_name="",
            model="gemini-3.5-flash-lite",
            outcome="planned",
            attempt_index=1,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeminiRequestAttempt.objects.create(
                request_id=request.request_id,
                request_graph=request,
                role="chat",
                key_name="",
                model="gemini-3.5-flash-lite",
                outcome="planned",
                attempt_index=1,
            )

    def test_non_null_source_lane_has_one_graph_while_null_sources_remain_plural(self):
        GeminiRequest.objects.create(
            request_id="source-lane-canonical",
            source_message_id=991,
            lane="live",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeminiRequest.objects.create(
                request_id="source-lane-duplicate",
                source_message_id=991,
                lane="live",
            )

        GeminiRequest.objects.create(
            request_id="source-lane-null-1",
            source_message_id=None,
            lane="analysis",
        )
        GeminiRequest.objects.create(
            request_id="source-lane-null-2",
            source_message_id=None,
            lane="analysis",
        )
        self.assertEqual(
            GeminiRequest.objects.filter(
                source_message_id__isnull=True,
                lane="analysis",
            ).count(),
            2,
        )

    def test_provider_started_attempt_requires_original_dispatch_day(self):
        request = GeminiRequest.objects.create(request_id="schema-request-day")
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeminiRequestAttempt.objects.create(
                request_id=request.request_id,
                request_graph=request,
                role="chat",
                key_name="",
                model="gemini-3.5-flash-lite",
                outcome="started",
                fsm_state=GeminiRequestAttempt.FsmState.PROVIDER_STARTED,
                attempt_index=1,
                provider_started_at=timezone.now(),
            )

    def test_profile_and_state_pair_constraints_are_real(self):
        profile = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.6-flash",
        )
        GeminiQuotaState.objects.create(
            project_identity="project-1",
            model=profile.model,
            quota_profile=profile,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeminiQuotaState.objects.create(
                project_identity="project-1",
                model=profile.model,
                quota_profile=profile,
            )

    def test_profile_effective_window_must_move_forward(self):
        instant = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            GeminiQuotaProfile.objects.create(
                profile_version="invalid-window.v1",
                model="gemini-test",
                rpm_limit=1,
                input_tpm_limit=1,
                rpd_limit=1,
                permit_limit=1,
                estimator_version="test",
                source=GeminiQuotaProfile.Source.ADMIN,
                observed_at=instant,
                effective_from=instant,
                effective_until=instant,
            )

    def test_quota_profile_is_append_only_through_model_and_queryset_paths(self):
        profile = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.7-flash",
        )
        profile.rpd_limit = 21
        with self.assertRaisesMessage(ValidationError, "append-only"):
            profile.save(update_fields=["rpd_limit"])
        with self.assertRaisesMessage(ValidationError, "append-only"):
            GeminiQuotaProfile.objects.filter(pk=profile.pk).update(rpd_limit=21)
        with self.assertRaisesMessage(ValidationError, "append-only"):
            GeminiQuotaProfile.objects.bulk_update([profile], ["rpd_limit"])
        with self.assertRaisesMessage(ValidationError, "append-only"):
            GeminiQuotaProfile.objects.bulk_create(
                [profile],
                update_conflicts=True,
                update_fields=["rpd_limit"],
                unique_fields=["id"],
            )
        with self.assertRaisesMessage(ValidationError, "append-only"):
            profile.delete()
        with self.assertRaisesMessage(ValidationError, "append-only"):
            GeminiQuotaProfile.objects.filter(pk=profile.pk).delete()

        profile.refresh_from_db()
        self.assertEqual(profile.rpd_limit, 20)

    def test_attempt_request_id_must_match_parent_graph(self):
        request = GeminiRequest.objects.create(request_id="graph-request-id")
        with self.assertRaisesMessage(ValidationError, "does not match"):
            GeminiRequestAttempt.objects.create(
                request_id="different-request-id",
                request_graph=request,
                role="chat",
                key_name="",
                model="gemini-3.5-flash-lite",
                outcome="planned",
                attempt_index=1,
            )

    def test_attempt_and_state_model_must_match_profile(self):
        profile = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.6-flash",
        )
        request = GeminiRequest.objects.create(request_id="profile-mismatch-request")
        with self.assertRaisesMessage(ValidationError, "does not match"):
            GeminiRequestAttempt.objects.create(
                request_id=request.request_id,
                request_graph=request,
                role="chat",
                key_name="",
                model="gemini-3.7-flash",
                quota_profile=profile,
                outcome="planned",
                attempt_index=1,
            )
        with self.assertRaisesMessage(ValidationError, "does not match"):
            GeminiQuotaState.objects.create(
                project_identity="project-mismatch",
                model="gemini-3.7-flash",
                quota_profile=profile,
            )

    def test_winner_must_belong_to_the_same_request_graph(self):
        first = GeminiRequest.objects.create(request_id="winner-graph-first")
        second = GeminiRequest.objects.create(request_id="winner-graph-second")
        second_attempt = GeminiRequestAttempt.objects.create(
            request_id=second.request_id,
            request_graph=second,
            role="chat",
            key_name="",
            model="gemini-3.5-flash-lite",
            outcome="succeeded",
            attempt_index=1,
        )
        first.winner_attempt = second_attempt

        with self.assertRaisesMessage(ValidationError, "another request graph"):
            first.save(update_fields=["winner_attempt", "updated_at"])

    def test_candidate_plan_digest_is_correct_and_immutable(self):
        bad_plan = [{"candidate_index": 1, "model": "gemini-3.5-flash-lite"}]
        with self.assertRaisesMessage(ValidationError, "does not match"):
            GeminiRequest.objects.create(
                request_id="candidate-bad-digest",
                candidate_plan=bad_plan,
                candidate_plan_digest="0" * 64,
            )

        plan = [{
            "candidate_index": 1,
            "project_identity": "project-1",
            "model": "gemini-3.5-flash-lite",
        }]
        request = GeminiRequest.objects.create(
            request_id="candidate-immutable",
            candidate_plan=plan,
            candidate_plan_digest=canonical_candidate_plan_digest(plan),
        )
        changed = [*plan, {
            "candidate_index": 2,
            "project_identity": "project-2",
            "model": "gemini-3.5-flash-lite",
        }]
        request.candidate_plan = changed
        request.candidate_plan_digest = canonical_candidate_plan_digest(changed)
        with self.assertRaisesMessage(ValidationError, "immutable"):
            request.save(update_fields=["candidate_plan", "candidate_plan_digest"])
        with self.assertRaisesMessage(ValidationError, "immutable"):
            GeminiRequest.objects.filter(pk=request.pk).update(
                candidate_plan=changed,
                candidate_plan_digest=canonical_candidate_plan_digest(changed),
            )

        request.refresh_from_db()
        self.assertEqual(request.candidate_plan, plan)

    def test_every_request_routing_and_lineage_identity_field_is_immutable(self):
        parent = GeminiRequest.objects.create(request_id="request-parent")
        request = self._identity_request("all-fields")
        changed_plan = [{"candidate_index": 9, "model": "gemini-3.5-flash"}]
        mutations = {
            "request_id": "request-all-fields-changed",
            "parent_request_id": parent.pk,
            "lane": "analysis",
            "task_class": "ordinary_live",
            "reasoning_task": "customer_chat",
            "logical_turn_id": "turn-changed",
            "source_message_id": 102,
            "source_execution_key": "ig-revision:999",
            "client_id": 203,
            "recovery_job_id": 304,
            "routing_policy_version": "routing-changed",
            "accounting_policy_version": "accounting-changed",
            "quota_profile_version": "profile-changed",
            "authority_snapshot_version": "authority-changed",
            "routing_mode": "pinned",
            "commercial_risk": "high",
            "requires_media_reasoning": False,
            "candidate_plan": changed_plan,
            "candidate_plan_digest": canonical_candidate_plan_digest(changed_plan),
            "policy_manifest": {"version": "changed"},
            "deadline_ms": 99,
            "deadline_at": request.deadline_at + datetime.timedelta(seconds=1),
            "accounting_mode": GeminiRequest.AccountingMode.SHADOW,
            "created_at": request.created_at + datetime.timedelta(seconds=1),
        }
        self.assertEqual(set(mutations), set(REQUEST_IMMUTABLE_FIELDS))

        for field, value in mutations.items():
            with self.subTest(path="model", field=field):
                request.refresh_from_db()
                setattr(request, field, value)
                with self.assertRaisesMessage(ValidationError, "immutable"):
                    request.save()
            with self.subTest(path="queryset", field=field):
                with self.assertRaisesMessage(ValidationError, "immutable"):
                    GeminiRequest.objects.filter(pk=request.pk).update(**{field: value})
            with self.subTest(path="bulk", field=field):
                request.refresh_from_db()
                setattr(request, field, value)
                with self.assertRaisesMessage(ValidationError, "immutable"):
                    GeminiRequest.objects.bulk_update([request], [field])

    def test_request_mutable_allowlist_accepts_only_terminal_settlement_fields(self):
        request = self._identity_request("mutable")
        allowed = set(REQUEST_MUTABLE_FIELDS)
        self.assertEqual(allowed, {
            "candidate_outcomes", "reply_message_id", "terminal_resolution",
            "terminal_reason", "provider_phase_started_at", "resolved_at",
            "updated_at",
        })
        resolved_at = timezone.now()
        updated = GeminiRequest.objects.filter(pk=request.pk).update(
            candidate_outcomes={"1": "succeeded"},
            reply_message_id=909,
            terminal_resolution="succeeded",
            terminal_reason="winner",
            provider_phase_started_at=resolved_at,
            resolved_at=resolved_at,
            updated_at=resolved_at,
        )
        self.assertEqual(updated, 1)
        request.refresh_from_db()
        request.terminal_reason = "bulk-settled"
        self.assertEqual(
            GeminiRequest.objects.bulk_update([request], ["terminal_reason"]),
            1,
        )

    def test_every_attempt_project_candidate_and_lineage_identity_is_immutable(self):
        request = self._identity_request("attempt-all-fields")
        attempt = self._identity_attempt(request)
        alternate_profile = self._new_profile("attempt-alternate.v1")
        mutations = {
            "request_graph_id": None,
            "request_id": "attempt-request-changed",
            "role": "management",
            "key_name": "GEMINI_API2",
            "project_group": "project-2",
            "project_identity": "project-2",
            "model": "gemini-3.6-flash",
            "quota_profile_id": alternate_profile.pk,
            "accounting_mode": "shadow",
            "logical_turn_id": "attempt-turn-changed",
            "source_message_id": 103,
            "client_id": 204,
            "lane": "recovery",
            "attempt_index": 2,
            "candidate_index": 2,
            "incident_id": 405,
            "recovery_job_id": 305,
            "created_at": attempt.created_at + datetime.timedelta(seconds=1),
        }
        self.assertEqual(set(mutations), set(ATTEMPT_IMMUTABLE_FIELDS))

        for field, value in mutations.items():
            with self.subTest(path="model", field=field):
                attempt.refresh_from_db()
                setattr(attempt, field, value)
                with self.assertRaisesMessage(ValidationError, "immutable"):
                    attempt.save()
            with self.subTest(path="queryset", field=field):
                with self.assertRaisesMessage(ValidationError, "immutable"):
                    GeminiRequestAttempt.objects.filter(pk=attempt.pk).update(
                        **{field: value}
                    )
            with self.subTest(path="bulk", field=field):
                attempt.refresh_from_db()
                setattr(attempt, field, value)
                with self.assertRaisesMessage(ValidationError, "immutable"):
                    GeminiRequestAttempt.objects.bulk_update([attempt], [field])

    def test_attempt_mutable_allowlist_supports_fsm_and_settlement_only(self):
        request = self._identity_request("attempt-mutable")
        attempt = self._identity_attempt(request)
        self.assertIn("fsm_state", ATTEMPT_MUTABLE_FIELDS)
        self.assertIn("settled_at", ATTEMPT_MUTABLE_FIELDS)
        self.assertNotIn("project_identity", ATTEMPT_MUTABLE_FIELDS)
        settled_at = timezone.now()
        updated = GeminiRequestAttempt.objects.filter(pk=attempt.pk).update(
            fsm_state=GeminiRequestAttempt.FsmState.SUCCEEDED,
            outcome="succeeded",
            prompt_tokens=123,
            candidates_tokens=45,
            total_tokens=168,
            settled_at=settled_at,
            permit_released_at=settled_at,
            winner_claimed=True,
        )
        self.assertEqual(updated, 1)
        attempt.refresh_from_db()
        attempt.failure_kind = ""
        self.assertEqual(
            GeminiRequestAttempt.objects.bulk_update([attempt], ["failure_kind"]),
            1,
        )

    def test_quota_profile_rotation_is_revisioned_idle_same_model_and_audited(self):
        current = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.7-flash",
        )
        replacement = self._new_profile("rotation-current.v2")
        state = GeminiQuotaState.objects.create(
            project_identity="rotation-project",
            model=current.model,
            quota_profile=current,
            revision=7,
        )

        rotated, audit = rotate_quota_state_profile(
            state_id=state.pk,
            new_profile_id=replacement.pk,
            expected_revision=7,
            reason="verified profile update",
        )

        self.assertEqual(rotated.quota_profile_id, replacement.pk)
        self.assertEqual(rotated.revision, 8)
        self.assertEqual(audit.action, "ig_gemini.quota_profile_rotated")
        self.assertEqual(audit.before["profile_version"], PROFILE_VERSION)
        self.assertEqual(audit.after["profile_version"], "rotation-current.v2")
        self.assertEqual(audit.after["revision"], 8)

    def test_quota_profile_rotation_fails_closed_on_all_guards(self):
        current = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.7-flash",
        )
        valid = self._new_profile("rotation-guard-valid.v2")
        wrong_model = self._new_profile(
            "rotation-guard-wrong.v2", model="gemini-3.6-flash"
        )
        future = self._new_profile(
            "rotation-guard-future.v2",
            effective_from=timezone.now() + datetime.timedelta(hours=1),
        )
        expired = self._new_profile(
            "rotation-guard-expired.v2",
            effective_from=timezone.now() - datetime.timedelta(hours=2),
            effective_until=timezone.now() - datetime.timedelta(hours=1),
        )
        state = GeminiQuotaState.objects.create(
            project_identity="rotation-guard-project",
            model=current.model,
            quota_profile=current,
            revision=3,
            in_flight_count=1,
        )

        cases = (
            (valid, 3, "in-flight"),
            (wrong_model, 3, "in-flight"),
        )
        for profile, revision, message in cases:
            with self.subTest(profile=profile.profile_version):
                with self.assertRaisesMessage(ValidationError, message):
                    rotate_quota_state_profile(
                        state_id=state.pk,
                        new_profile_id=profile.pk,
                        expected_revision=revision,
                    )
        GeminiQuotaState._base_manager.filter(pk=state.pk).update(in_flight_count=0)
        for profile, revision, message in (
            (valid, 99, "revision changed"),
            (wrong_model, 3, "does not match"),
            (future, 3, "not currently effective"),
            (expired, 3, "not currently effective"),
        ):
            with self.subTest(profile=profile.profile_version, revision=revision):
                with self.assertRaisesMessage(ValidationError, message):
                    rotate_quota_state_profile(
                        state_id=state.pk,
                        new_profile_id=profile.pk,
                        expected_revision=revision,
                    )
        state.refresh_from_db()
        self.assertEqual(state.quota_profile_id, current.pk)
        self.assertEqual(state.revision, 3)
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action="ig_gemini.quota_profile_rotated",
                entity_id=str(state.pk),
            ).exists()
        )

    def test_quota_profile_cannot_rotate_through_generic_save_or_update(self):
        current = GeminiQuotaProfile.objects.get(
            profile_version=PROFILE_VERSION,
            model="gemini-3.7-flash",
        )
        replacement = self._new_profile("rotation-generic-block.v2")
        state = GeminiQuotaState.objects.create(
            project_identity="rotation-generic-project",
            model=current.model,
            quota_profile=current,
        )
        state.quota_profile = replacement
        with self.assertRaisesMessage(ValidationError, "immutable"):
            state.save(update_fields=["quota_profile"])
        with self.assertRaisesMessage(ValidationError, "contract boundary"):
            GeminiQuotaState.objects.filter(pk=state.pk).update(
                quota_profile=replacement
            )


class GeminiAccountingMigrationContractTests(TestCase):
    def test_mariadb_retry_script_bootstraps_from_clean_cwd_without_pythonpath(self):
        project_root = os.path.dirname(os.path.dirname(__file__))
        repository_root = os.path.dirname(project_root)
        script_path = os.path.join(
            repository_root,
            "scripts",
            "run_gemini_accounting_s3a_mariadb_retry.py",
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "TMPDIR", "SYSTEMROOT"}
        }
        environment["DJANGO_SETTINGS_MODULE"] = "test_settings_mariadb"
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as clean_cwd:
            result = subprocess.run(
                [sys.executable, script_path, "--confirm-disposable"],
                cwd=clean_cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEST_MARIADB_NAME must be set", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_engine_prerequisite_precedes_every_v2_fk_ddl(self):
        prerequisite = importlib.import_module(
            "management.migrations.0178_gemini_accounting_prerequisite_innodb"
        )
        schema = importlib.import_module(
            "management.migrations.0179_gemini_accounting_v2_schema"
        )
        self.assertEqual(
            prerequisite.Migration.dependencies,
            [("management", "0177_gemini_adaptive_routing")],
        )
        self.assertFalse(prerequisite.Migration.atomic)
        self.assertEqual(
            prerequisite.PREREQUISITE_TABLES,
            (
                "management_geminirequestattempt",
                "management_geminimodelquotausage",
            ),
        )
        self.assertEqual(
            schema.Migration.dependencies,
            [("management", "0178_gemini_accounting_prerequisite_innodb")],
        )
        self.assertFalse(schema.Migration.atomic)
        self.assertTrue(all(
            operation.__class__.__name__.startswith("Idempotent")
            for operation in schema.Migration.operations
        ))
        self.assertFalse(any(
            operation.__class__.__name__ == "RunPython"
            for operation in schema.Migration.operations
        ))

    def test_source_lane_unique_operation_is_exact_shape_and_drift_detecting(self):
        schema = importlib.import_module(
            "management.migrations.0179_gemini_accounting_v2_schema"
        )
        operation = next(
            item
            for item in schema.Migration.operations
            if getattr(getattr(item, "constraint", None), "name", "")
            == "gem_req_source_lane_uniq"
        )
        self.assertEqual(
            operation.__class__.__name__,
            "IdempotentAddExactUniqueConstraint",
        )
        self.assertEqual(
            tuple(operation.constraint.fields),
            ("source_message_id", "lane"),
        )

        model = Mock()
        model._meta.get_field.side_effect = lambda name: SimpleNamespace(
            column=name
        )
        valid = {
            "columns": ["source_message_id", "lane"],
            "unique": True,
            "primary_key": False,
            "check": False,
            "foreign_key": None,
        }
        operation._validate(Mock(), model, valid)
        with self.assertRaisesMessage(RuntimeError, "unexpected shape"):
            operation._validate(
                Mock(),
                model,
                {**valid, "columns": ["lane", "source_message_id"]},
            )

    def test_prerequisite_conversion_is_retry_idempotent_after_partial_progress(self):
        migration = importlib.import_module(
            "management.migrations.0178_gemini_accounting_prerequisite_innodb"
        )
        first_cursor = Mock()
        first_cursor.__enter__ = Mock(return_value=first_cursor)
        first_cursor.__exit__ = Mock(return_value=False)
        first_cursor.fetchone.side_effect = [("MyISAM",), RuntimeError("interrupted")]
        first_editor = Mock()
        first_editor.connection.vendor = "mysql"
        first_editor.connection.cursor.return_value = first_cursor
        first_editor.quote_name.side_effect = lambda value: f"`{value}`"

        with self.assertRaisesMessage(RuntimeError, "interrupted"):
            migration.ensure_prerequisite_tables_innodb(None, first_editor)
        first_editor.execute.assert_called_once_with(
            "ALTER TABLE `management_geminirequestattempt` ENGINE=InnoDB"
        )

        retry_cursor = Mock()
        retry_cursor.__enter__ = Mock(return_value=retry_cursor)
        retry_cursor.__exit__ = Mock(return_value=False)
        retry_cursor.fetchone.side_effect = [("InnoDB",), ("InnoDB",)]
        retry_editor = Mock()
        retry_editor.connection.vendor = "mysql"
        retry_editor.connection.cursor.return_value = retry_cursor

        migration.ensure_prerequisite_tables_innodb(None, retry_editor)
        retry_editor.execute.assert_not_called()

    def test_profile_seed_is_separate_retry_idempotent_and_drift_detecting(self):
        migration = importlib.import_module(
            "management.migrations.0180_seed_gemini_quota_profiles"
        )
        self.assertEqual(
            migration.Migration.dependencies,
            [("management", "0179_gemini_accounting_v2_schema")],
        )
        self.assertEqual(len(migration.PROFILE_MODELS), 4)
        self.assertNotIn("key", migration.PROFILE_VERSION.casefold())

        before = GeminiQuotaProfile.objects.count()
        migration.seed_owner_observed_profiles(django_apps, None)
        self.assertEqual(GeminiQuotaProfile.objects.count(), before)

        expected_time = datetime.datetime(
            2026, 8, 29, 17, 18, 56, tzinfo=datetime.timezone.utc
        )
        drifted = SimpleNamespace(
            rpm_limit=99,
            input_tpm_limit=250_000,
            rpd_limit=20,
            permit_limit=1,
            estimator_version="shadow-calibration-required",
            source="owner_observed",
            source_reference="owner_ai_studio_screenshot",
            observed_at=expected_time,
            effective_from=expected_time,
            effective_until=None,
        )
        historical_model = Mock()
        historical_model.objects.get_or_create.return_value = (drifted, False)
        historical_apps = Mock()
        historical_apps.get_model.return_value = historical_model

        with self.assertRaisesMessage(RuntimeError, "profile drift"):
            migration.seed_owner_observed_profiles(historical_apps, None)

    def test_calibrated_profile_seed_is_partial_evidence_and_idempotent(self):
        migration = importlib.import_module(
            "management.migrations.0186_calibrated_gemini_quota_profiles"
        )
        self.assertEqual(
            migration.Migration.dependencies,
            [("management", "0185_typed_memory_v2")],
        )
        self.assertEqual(migration.PROFILE_VERSION, CALIBRATED_PROFILE_VERSION)
        self.assertEqual(migration.ESTIMATOR_VERSION, "json_bytes_div4_v1")

        rows = {
            row.model: row
            for row in GeminiQuotaProfile.objects.filter(
                profile_version=CALIBRATED_PROFILE_VERSION
            )
        }
        self.assertEqual(set(rows), set(EXPECTED_PROFILES))
        self.assertEqual(rows["gemini-3.6-flash"].estimator_version, "json_bytes_div4_v1")
        self.assertEqual(rows["gemini-3.5-flash-lite"].estimator_version, "json_bytes_div4_v1")
        self.assertEqual(
            rows["gemini-3.7-flash"].estimator_version,
            "shadow-calibration-required",
        )
        self.assertEqual(
            rows["gemini-3.5-flash"].estimator_version,
            "shadow-calibration-required",
        )
        for row in rows.values():
            self.assertEqual(row.source, GeminiQuotaProfile.Source.ADMIN)
            self.assertEqual(row.observed_at, CALIBRATED_AT)
            self.assertEqual(row.effective_from, CALIBRATED_AT)
        self.assertIn("n15:min1.710", rows["gemini-3.6-flash"].source_reference)
        self.assertIn("n2:min2.531", rows["gemini-3.5-flash-lite"].source_reference)
        self.assertIn("owner_limits_2026_08_29", rows["gemini-3.6-flash"].source_reference)
        self.assertIn("under0", rows["gemini-3.6-flash"].source_reference)
        self.assertIn(
            "calibration_pending",
            rows["gemini-3.7-flash"].source_reference,
        )

        before = GeminiQuotaProfile.objects.count()
        migration.seed_calibrated_profiles(django_apps, None)
        self.assertEqual(GeminiQuotaProfile.objects.count(), before)

        drifted = SimpleNamespace(
            rpm_limit=99,
            input_tpm_limit=250_000,
            rpd_limit=20,
            permit_limit=1,
            estimator_version="json_bytes_div4_v1",
            source="admin",
            source_reference=(
                "owner_limits_2026_08_29;prod_ratio_2026_08_31:"
                "n15:min1.710:med1.823:max2.376:under0"
            ),
            observed_at=CALIBRATED_AT,
            effective_from=CALIBRATED_AT,
            effective_until=None,
        )
        historical_model = Mock()
        historical_model.objects.get_or_create.return_value = (drifted, False)
        historical_apps = Mock()
        historical_apps.get_model.return_value = historical_model
        with self.assertRaisesMessage(RuntimeError, "profile drift"):
            migration.seed_calibrated_profiles(historical_apps, None)

    def test_engine_migration_is_separate_non_atomic_and_complete(self):
        migration = importlib.import_module(
            "management.migrations.0181_gemini_accounting_v2_innodb"
        )
        self.assertFalse(migration.Migration.atomic)
        self.assertEqual(
            migration.Migration.dependencies,
            [("management", "0180_seed_gemini_quota_profiles")],
        )
        self.assertEqual(
            set(migration.GEMINI_ACCOUNTING_TABLES),
            {
                "management_geminiquotaprofile",
                "management_geminiquotastate",
                "management_geminirequest",
                "management_geminirequestattempt",
                "management_geminimodelquotausage",
            },
        )

    def test_engine_conversion_changes_only_non_innodb_tables(self):
        migration = importlib.import_module(
            "management.migrations.0181_gemini_accounting_v2_innodb"
        )
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        cursor.fetchone.side_effect = [
            ("InnoDB",),
            ("MyISAM",),
            ("InnoDB",),
            ("InnoDB",),
            ("InnoDB",),
        ]
        schema_editor = Mock()
        schema_editor.connection.vendor = "mysql"
        schema_editor.connection.cursor.return_value = cursor
        schema_editor.quote_name.side_effect = lambda value: f"`{value}`"

        migration.ensure_gemini_accounting_tables_innodb(None, schema_editor)

        schema_editor.execute.assert_called_once_with(
            "ALTER TABLE `management_geminiquotastate` ENGINE=InnoDB"
        )

    def test_engine_conversion_fails_if_a_required_table_is_missing(self):
        migration = importlib.import_module(
            "management.migrations.0181_gemini_accounting_v2_innodb"
        )
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        cursor.fetchone.return_value = None
        schema_editor = Mock()
        schema_editor.connection.vendor = "mysql"
        schema_editor.connection.cursor.return_value = cursor

        with self.assertRaisesMessage(
            RuntimeError,
            "required Gemini accounting table is missing: "
            "management_geminiquotaprofile",
        ):
            migration.ensure_gemini_accounting_tables_innodb(None, schema_editor)

    def test_engine_registry_declares_every_v2_and_legacy_quota_table(self):
        for table in (
            "management_geminimodelquotausage",
            "management_geminiquotaprofile",
            "management_geminiquotastate",
            "management_geminirequest",
            "management_geminirequestattempt",
        ):
            with self.subTest(table=table):
                self.assertIn(table, IG_RUNTIME_TABLES)

    def test_forward_migration_preserves_legacy_attempt_and_usage(self):
        script = textwrap.dedent(
            """
            import datetime
            import json
            import os
            import sys

            os.environ["DJANGO_SETTINGS_MODULE"] = "twocomms.settings"
            os.environ["IG_UGC_IDENTITY_HMAC_KEYRING"] = (
                '{"test":"0123456789abcdef0123456789abcdef"}'
            )
            os.environ["IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID"] = "test"
            from django.conf import settings
            settings.DATABASES["default"] = {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": sys.argv[1],
            }

            import django
            django.setup()

            from django.db import connection
            from django.db.migrations.executor import MigrationExecutor

            migrate_from = ("management", "0177_gemini_adaptive_routing")
            migrate_to = ("management", "0181_gemini_accounting_v2_innodb")

            executor = MigrationExecutor(connection)
            executor.migrate([migrate_from])
            executor = MigrationExecutor(connection)
            old_apps = executor.loader.project_state([migrate_from]).apps
            Attempt = old_apps.get_model("management", "GeminiRequestAttempt")
            Usage = old_apps.get_model("management", "GeminiModelQuotaUsage")
            attempt = Attempt.objects.create(
                request_id="legacy-request",
                role="chat",
                key_name="GEMINI_API",
                model="gemini-3.7-flash",
                outcome="failed",
                failure_kind="quota_429",
                attempt_index=3,
            )
            usage = Usage.objects.create(
                key_name="GEMINI_API",
                model="gemini-3.7-flash",
                day_date=datetime.date(2026, 8, 29),
                requests=3,
                tokens=321,
            )

            executor = MigrationExecutor(connection)
            executor.migrate([migrate_to])
            executor = MigrationExecutor(connection)
            new_apps = executor.loader.project_state([migrate_to]).apps
            AttemptV2 = new_apps.get_model("management", "GeminiRequestAttempt")
            UsageV1 = new_apps.get_model("management", "GeminiModelQuotaUsage")
            Profile = new_apps.get_model("management", "GeminiQuotaProfile")
            State = new_apps.get_model("management", "GeminiQuotaState")
            Request = new_apps.get_model("management", "GeminiRequest")
            attempt_v2 = AttemptV2.objects.get(pk=attempt.pk)
            usage_v1 = UsageV1.objects.get(pk=usage.pk)

            print("MIGRATION_RESULT=" + json.dumps({
                "attempt": {
                    "request_id": attempt_v2.request_id,
                    "outcome": attempt_v2.outcome,
                    "failure_kind": attempt_v2.failure_kind,
                    "attempt_index": attempt_v2.attempt_index,
                    "fsm_state": attempt_v2.fsm_state,
                    "request_graph_id": attempt_v2.request_graph_id,
                    "project_identity": attempt_v2.project_identity,
                },
                "usage": {
                    "requests": usage_v1.requests,
                    "tokens": usage_v1.tokens,
                },
                "seed": {
                    "profiles": Profile.objects.count(),
                    "states": State.objects.count(),
                    "requests": Request.objects.count(),
                },
            }, sort_keys=True))
            """
        )
        project_root = os.path.dirname(os.path.dirname(__file__))
        env = os.environ.copy()
        for key in (
            "DB_ENGINE",
            "DB_NAME",
            "DB_USER",
            "DB_PASSWORD",
            "DB_HOST",
            "DB_PORT",
        ):
            env.pop(key, None)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (project_root, env.get("PYTHONPATH", "")))
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-c", script, os.path.join(temp_dir, "migration.sqlite3")],
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        marker = next(
            line
            for line in result.stdout.splitlines()
            if line.startswith("MIGRATION_RESULT=")
        )
        payload = json.loads(marker.removeprefix("MIGRATION_RESULT="))
        self.assertEqual(payload["attempt"], {
            "attempt_index": 3,
            "failure_kind": "quota_429",
            "fsm_state": "legacy",
            "outcome": "failed",
            "project_identity": "",
            "request_graph_id": None,
            "request_id": "legacy-request",
        })
        self.assertEqual(payload["usage"], {"requests": 3, "tokens": 321})
        self.assertEqual(
            payload["seed"],
            {"profiles": 4, "requests": 0, "states": 0},
        )

    def test_interrupted_non_atomic_schema_migration_resumes_idempotently(self):
        script = textwrap.dedent(
            """
            import json
            import os
            import sys

            os.environ["DJANGO_SETTINGS_MODULE"] = "twocomms.settings"
            os.environ["IG_UGC_IDENTITY_HMAC_KEYRING"] = (
                '{"test":"0123456789abcdef0123456789abcdef"}'
            )
            os.environ["IG_UGC_IDENTITY_HMAC_ACTIVE_KEY_ID"] = "test"
            from django.conf import settings
            settings.DATABASES["default"] = {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": sys.argv[1],
            }

            import django
            django.setup()

            from django.db import connection
            from django.db.migrations.executor import MigrationExecutor
            from django.db.migrations.recorder import MigrationRecorder
            from management.migration_operations import IdempotentAddField

            before = ("management", "0178_gemini_accounting_prerequisite_innodb")
            schema = ("management", "0179_gemini_accounting_v2_schema")
            final = ("management", "0181_gemini_accounting_v2_innodb")
            executor = MigrationExecutor(connection)
            executor.migrate([before])

            original = IdempotentAddField.database_forwards
            counter = {"completed": 0}
            def interrupt_after_five(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                counter["completed"] += 1
                if counter["completed"] == 5:
                    raise RuntimeError("simulated-mariadb-ddl-interruption")
                return result
            IdempotentAddField.database_forwards = interrupt_after_five
            interrupted = False
            try:
                executor = MigrationExecutor(connection)
                executor.migrate([schema])
            except RuntimeError as exc:
                interrupted = str(exc) == "simulated-mariadb-ddl-interruption"
            finally:
                IdempotentAddField.database_forwards = original

            recorder = MigrationRecorder(connection)
            recorded_after_interrupt = recorder.migration_qs.filter(
                app="management", name=schema[1]
            ).exists()
            with connection.cursor() as cursor:
                partial_columns = {
                    column.name
                    for column in connection.introspection.get_table_description(
                        cursor, "management_geminirequestattempt"
                    )
                }

            executor = MigrationExecutor(connection)
            executor.migrate([final])
            executor = MigrationExecutor(connection)
            apps = executor.loader.project_state([final]).apps
            with connection.cursor() as cursor:
                final_columns = {
                    column.name
                    for column in connection.introspection.get_table_description(
                        cursor, "management_geminirequestattempt"
                    )
                }
                request_constraints = connection.introspection.get_constraints(
                    cursor, "management_geminirequest"
                )
            source_lane_unique = request_constraints.get(
                "gem_req_source_lane_uniq", {}
            )
            print("MIGRATION_RETRY_RESULT=" + json.dumps({
                "interrupted": interrupted,
                "completed_before_interrupt": counter["completed"],
                "recorded_after_interrupt": recorded_after_interrupt,
                "partial_accounting_mode": "accounting_mode" in partial_columns,
                "final_request_graph": "request_graph_id" in final_columns,
                "source_lane_unique": bool(
                    source_lane_unique.get("unique")
                    and source_lane_unique.get("columns")
                    == ["source_message_id", "lane"]
                ),
                "profiles": apps.get_model(
                    "management", "GeminiQuotaProfile"
                ).objects.count(),
            }, sort_keys=True))
            """
        )
        project_root = os.path.dirname(os.path.dirname(__file__))
        env = os.environ.copy()
        for key in (
            "DB_ENGINE", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
        ):
            env.pop(key, None)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (project_root, env.get("PYTHONPATH", "")))
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, "-c", script, os.path.join(temp_dir, "retry.sqlite3")],
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        marker = next(
            line for line in result.stdout.splitlines()
            if line.startswith("MIGRATION_RETRY_RESULT=")
        )
        payload = json.loads(marker.removeprefix("MIGRATION_RETRY_RESULT="))
        self.assertEqual(payload, {
            "completed_before_interrupt": 5,
            "final_request_graph": True,
            "interrupted": True,
            "partial_accounting_mode": True,
            "profiles": 4,
            "recorded_after_interrupt": False,
            "source_lane_unique": True,
        })
