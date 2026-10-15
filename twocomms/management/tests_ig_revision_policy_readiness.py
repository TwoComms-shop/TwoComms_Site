"""Execute pre-provider policy failure proof/debt boundaries without Django I/O."""
import ast
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SERVICES = Path(__file__).parent / "services"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


def load_functions(namespace):
    tree = ast.parse((SERVICES / "ig_revision_recovery.py").read_text())
    names = {"policy_readiness_failure_code", "_policy_failure_details", "record_policy_readiness_failure", "_set_state"}
    nodes = [row for row in tree.body if isinstance(row, ast.FunctionDef) and row.name in names]
    nodes += [row for row in tree.body if isinstance(row, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in {"LOCAL_POLICY_READINESS_CODES", "POLICY_READINESS_FAILURE_KEY"}
        for target in row.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "ig_revision_recovery.py", "exec"), namespace)
    return namespace


class RevisionPolicyReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.publication = SimpleNamespace(publication_id=8, version=2, snapshot_hash="publication-hash")
        self.admission = {"schema_version": 1, "revision_id": 218, "logical_turn_id": "ig-revision:218",
            "snapshot_digest": "source-snapshot", "settings_id": 1, "settings_permission_epoch": 6,
            "publication": {"id": 8, "version": 2, "hash": "publication-hash"},
            "fact_bindings": [{"claim": "public_policy_inputs"}], "offer_bindings": [], "selection_digest": "selection"}
        self.admission["digest"] = digest(self.admission)
        self.revision = SimpleNamespace(pk=218, client_id=5, generation_proposal={}, generation_proposal_digest="",
            snapshot_digest="source-snapshot", bundle_snapshot={"sources": [{"message_id": 3373}]},
            action_receipts={"generation_admission": self.admission}, delivery_effects=SimpleNamespace(exists=Mock(return_value=False)),
            recovery_state="", recovery_code="", claim_token="real-token", lease_until=self.now, save=Mock())
        self.revisions = Mock()
        self.revisions.objects.filter.return_value.values.return_value.first.return_value = {"client_id": 5}
        self.revisions.objects.select_for_update.return_value.filter.return_value.first.return_value = self.revision
        self.clients = Mock()
        self.clients.objects.select_for_update.return_value.filter.return_value.first.return_value = SimpleNamespace(pk=5)
        self.settings = Mock()
        self.settings.objects.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = SimpleNamespace(pk=1)
        self.graphs = Mock()
        self.graphs.objects.filter.return_value.exists.return_value = False
        self.cas = Mock(return_value=SimpleNamespace(ready=True, reasons=()))
        self.sources = Mock(return_value=True)
        self.park = Mock()
        self.ns = load_functions({"re": re, "_digest": digest, "transaction": SimpleNamespace(atomic=nullcontext),
            "timezone": SimpleNamespace(now=lambda: self.now), "IgCustomerTurnRevision": self.revisions,
            "IgClient": self.clients, "InstagramBotSettings": self.settings, "GeminiRequest": self.graphs,
            "RecoveryAdmissionResult": lambda ready=False, reason="": SimpleNamespace(ready=ready, reason=reason),
            "RecoveryResult": lambda *args: args, "_cas_readiness": self.cas,
            "_selection_digest": Mock(return_value="selection"),
            "check_fact_bindings": Mock(), "check_offer_bindings": Mock()})

    def record(self, *, details=None, code="mandatory_policy_exceeds_budget"):
        modules = {"management.services.ig_revision_holding": module("management.services.ig_revision_holding", _sources_unchanged=self.sources),
            "management.services.ig_response_debt": module("management.services.ig_response_debt", park_manual_revision=self.park)}
        with patch.dict(sys.modules, modules):
            return self.ns["record_policy_readiness_failure"](218, "real-token", settings_id=1,
                settings_permission_epoch=6, publication=self.publication, code=code, details=details, now=self.now)

    def test_zero_dispatch_failure_records_exact_source_and_parks_real_manager_debt(self):
        result = self.record(details={"budget_chars": 48000, "required_chars": 49000,
            "mandatory_total_chars": 51000, "mandatory_module_lengths": {"context:turn": 16000}})
        self.assertTrue(result.ready)
        receipt = self.revision.action_receipts["policy_readiness_failure"]
        self.assertEqual(receipt["source_message_ids"], [3373])
        self.assertEqual(receipt["generation_admission_digest"], self.admission["digest"])
        self.assertEqual(receipt["snapshot_digest"], "source-snapshot")
        self.assertEqual(receipt["publication"], self.admission["publication"])
        self.assertFalse(receipt["provider_dispatched"])
        self.assertEqual(receipt["details"]["mandatory_total_chars"], 51000)
        self.assertEqual(receipt["digest"], digest({key: value for key, value in receipt.items() if key != "digest"}))
        self.assertEqual(self.revision.recovery_state, "manual")
        self.assertEqual(self.revision.recovery_code, "mandatory_policy_exceeds_budget")
        self.park.assert_called_once_with(self.revision, "mandatory_policy_exceeds_budget")
        self.graphs.objects.create.assert_not_called()

    def test_any_real_provider_graph_forbids_zero_dispatch_proof_even_without_effects(self):
        self.graphs.objects.filter.return_value.exists.return_value = True
        result = self.record()
        self.assertEqual(result.reason, "policy_failure_dispatch_not_excluded")
        self.assertNotIn("policy_readiness_failure", self.revision.action_receipts)
        self.park.assert_not_called()

    def test_unknown_or_started_delivery_cannot_be_overridden(self):
        # Existence is deliberately stronger than checking only sent effects:
        # planned, started, UNKNOWN and any other stored part all refuse proof.
        self.revision.delivery_effects.exists.return_value = True
        self.assertEqual(self.record().reason, "policy_failure_dispatch_not_excluded")
        self.park.assert_not_called()

    def test_existing_generation_proposal_cannot_be_discarded(self):
        self.revision.generation_proposal = {"response": "already generated"}
        self.assertEqual(self.record().reason, "policy_failure_dispatch_not_excluded")
        self.park.assert_not_called()

    def test_changed_admission_or_publication_refuses_durable_failure_receipt(self):
        self.admission["publication"]["version"] = 3
        self.admission["digest"] = digest({key: value for key, value in self.admission.items() if key != "digest"})
        self.assertEqual(self.record().reason, "policy_failure_admission_changed")
        self.park.assert_not_called()

    def test_changed_source_cannot_be_parked_as_the_captured_policy_failure(self):
        self.sources.return_value = False
        self.assertEqual(self.record().reason, "policy_failure_sources_changed")
        self.park.assert_not_called()

    def test_changed_selection_cannot_relabel_another_configuration_failure(self):
        self.ns["_selection_digest"].return_value = "new-selection"
        self.assertEqual(self.record().reason, "policy_failure_admission_changed")
        self.park.assert_not_called()

    def test_permission_or_newer_source_cas_rejection_preserves_native_fences(self):
        self.cas.return_value = SimpleNamespace(ready=False, reasons=("pending_inbound",))
        self.assertEqual(self.record().reason, "pending_inbound")
        self.park.assert_not_called()
        self.assertNotIn("policy_readiness_failure", self.revision.action_receipts)

    def test_diagnostics_keep_counts_and_finite_ids_never_prompt_or_customer_text(self):
        safe = self.ns["_policy_failure_details"]({"budget_chars": 48000, "mandatory_total_chars": 60000,
            "required_chars": True, "system_prompt": "private prompt", "customer_text": "private customer",
            "mandatory_ids": ["context:turn", "bad id with customer text"],
            "mandatory_module_lengths": {"facts:catalog": 19058, "customer private text": 100}})
        self.assertEqual(safe, {"budget_chars": 48000, "mandatory_total_chars": 60000,
            "mandatory_ids": ["context:turn"], "mandatory_module_lengths": {"facts:catalog": 19058}})

    def test_unrelated_invalid_payload_is_not_relabelled_as_policy_failure(self):
        code = self.ns["policy_readiness_failure_code"]
        self.assertEqual(code({"kind": "invalid_payload"}), "")
        self.assertEqual(code({"kind": "provider_error", "policy_readiness": "mandatory_policy_exceeds_budget"}), "")
        self.assertEqual(code({"kind": "invalid_payload", "policy_readiness": "private text"}), "")
        self.assertEqual(code({"kind": "invalid_payload", "policy_readiness": "mandatory_policy_exceeds_budget"}),
            "mandatory_policy_exceeds_budget")

    def test_unknown_readiness_code_never_grants_manual_debt_or_dispatch_proof(self):
        self.assertEqual(self.record(code="new_unknown_fault").reason, "policy_failure_code_unsupported")
        self.park.assert_not_called()
        self.revisions.objects.filter.assert_not_called()

    def test_actual_live_generation_branch_returns_specific_policy_code(self):
        tree = ast.parse((SERVICES / "ig_revision_live.py").read_text())
        generation = next(row for row in tree.body if isinstance(row, ast.FunctionDef) and row.name == "_generate_proposal")
        branch = next(row for row in generation.body if isinstance(row, ast.If)
            and ast.unparse(row.test) == "not isinstance(response, ValidatedResponse) or not response.valid")
        wrapper = ast.parse("def execute():\n    raise AssertionError('failure branch did not return')\n").body[0]
        wrapper.body = [deepcopy(branch), *wrapper.body]
        boundary = SimpleNamespace(last_reasons=())
        namespace = {"response": None, "ValidatedResponse": type("ValidatedResponse", (), {}), "boundary": boundary,
            "failure": {"kind": "invalid_payload", "policy_readiness": "mandatory_policy_exceeds_budget",
                "policy_readiness_details": {"budget_chars": 48000}}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "ig_revision_live.py", "exec"), namespace)
        with patch.dict(sys.modules, {"management.services.ig_revision_recovery": module("management.services.ig_revision_recovery",
            policy_readiness_failure_code=self.ns["policy_readiness_failure_code"])}):
            response, returned_boundary, reasons = namespace["execute"]()
        self.assertIsNone(response)
        self.assertIs(returned_boundary, boundary)
        self.assertEqual(reasons, ("mandatory_policy_exceeds_budget",))
        self.assertEqual(boundary.policy_readiness_failure["details"], {"budget_chars": 48000})


if __name__ == "__main__":
    unittest.main()


# The direct script above runs only the pure AST contracts. Django's test
# loader also discovers these native-row cases against its configured test DB.
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from management.models import IgFollowUpTask, IgRevisionDeliveryEffect, GeminiRequest, InstagramBotMessage, IgCustomerTurnRevision
from management.services.ig_revision_recovery import record_policy_readiness_failure, schedule_revision_recovery
from management import tests_ig_revision_recovery as recovery_fixtures


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionPolicyReadinessDatabaseTests(TransactionTestCase):
    reset_sequences = True
    _message = recovery_fixtures.RevisionRecoveryTests._message
    _admit = recovery_fixtures.RevisionRecoveryTests._admit
    _graph = recovery_fixtures.RevisionRecoveryTests._graph

    def setUp(self):
        recovery_fixtures.RevisionRecoveryTests.setUp(self)
        self._admit()

    def _record_failure(self, token=None):
        return record_policy_readiness_failure(self.revision.pk, token or self.token,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.publication_binding, code="mandatory_policy_exceeds_budget",
            details={"budget_chars": 48000, "required_chars": 49000, "mandatory_total_chars": 51000})

    def test_native_zero_graph_failure_creates_source_bound_debt_without_delivery_or_source_completion(self):
        before_status, before_send = self.source.status, self.source.send_state
        deadline = self.revision.overall_deadline
        result = self._record_failure()
        self.assertTrue(result.ready, result.reason)
        self.revision.refresh_from_db()
        self.source.refresh_from_db()
        receipt = self.revision.action_receipts["policy_readiness_failure"]
        self.assertEqual(receipt["source_message_ids"], [self.source.pk])
        self.assertEqual(receipt["snapshot_digest"], self.revision.snapshot_digest)
        self.assertEqual(receipt["generation_admission_digest"], self.revision.action_receipts["generation_admission"]["digest"])
        self.assertEqual(self.revision.recovery_state, "manual")
        self.assertEqual(self.revision.recovery_code, "mandatory_policy_exceeds_budget")
        self.assertEqual(self.revision.claim_token, "")
        self.assertIsNone(self.revision.lease_until)
        self.assertEqual(self.revision.overall_deadline, deadline)
        self.assertEqual((self.source.status, self.source.send_state), (before_status, before_send))
        self.assertFalse(GeminiRequest.objects.filter(logical_turn_id=f"ig-revision:{self.revision.pk}").exists())
        self.assertFalse(self.revision.delivery_effects.exists())
        task = IgFollowUpTask.objects.get(event_key=f"ig-revision-debt:{self.revision.pk}")
        self.assertEqual(task.manager_context["reason"], "mandatory_policy_exceeds_budget")
        self.assertFalse(task.manager_context["automatic_http_retry"])
        recovery = schedule_revision_recovery(self.revision.pk)
        self.assertEqual((recovery.state, recovery.reason), ("manual", "mandatory_policy_exceeds_budget"))

    def test_native_changed_source_refuses_failure_receipt_and_manager_debt(self):
        self.source.text += " Нова редакція джерела."
        self.source.save(update_fields=["text"])
        result = self._record_failure()
        self.assertEqual(result.reason, "policy_failure_sources_changed")
        self.revision.refresh_from_db()
        self.assertNotIn("policy_readiness_failure", self.revision.action_receipts)
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").exists())

    def test_native_policy_receipt_can_be_appended_but_never_changed_or_removed(self):
        self.assertTrue(self._record_failure().ready)
        for change in ("replace", "remove", "change_prior_admission", "append_unknown_key"):
            with self.subTest(change=change):
                self.revision.refresh_from_db()
                stored = deepcopy(self.revision.action_receipts)
                altered = deepcopy(stored)
                if change == "replace":
                    altered["policy_readiness_failure"]["code"] = "different_policy_failure"
                elif change == "remove":
                    del altered["policy_readiness_failure"]
                elif change == "change_prior_admission":
                    altered["generation_admission"]["settings_permission_epoch"] += 1
                else:
                    altered["unknown_policy_override"] = {"allowed": True}
                self.revision.action_receipts = altered
                with self.assertRaisesMessage(ValueError, "revision action receipt entries are immutable"):
                    self.revision.save(update_fields=["action_receipts", "updated_at"])
                self.revision.refresh_from_db()
                self.assertEqual(self.revision.action_receipts, stored)

    def test_native_policy_receipt_still_cannot_be_updated_through_queryset(self):
        self.assertTrue(self._record_failure().ready)
        with self.assertRaisesMessage(ValueError, "revision generation proposal is immutable"):
            IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(action_receipts={})
        self.revision.refresh_from_db()
        self.assertIn("policy_readiness_failure", self.revision.action_receipts)

    def test_native_claim_cas_rejects_another_execution_owner(self):
        result = self._record_failure(token="foreign-owner")
        self.assertEqual(result.reason, "revision_not_current")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.claim_token, self.token)
        self.assertNotIn("policy_readiness_failure", self.revision.action_receipts)

    def test_native_real_generation_graph_prevents_zero_dispatch_assertion(self):
        graph = self._graph(outcome="failed")
        result = self._record_failure()
        self.assertEqual(result.reason, "policy_failure_dispatch_not_excluded")
        self.assertTrue(GeminiRequest.objects.filter(pk=graph.pk).exists())
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").exists())

    def test_native_unknown_effect_is_preserved_and_cannot_become_retry_authority(self):
        effect = IgRevisionDeliveryEffect.objects.create(revision=self.revision, source_message=self.source,
            effect_key=f"policy-unknown-{self.revision.pk}", group="reply", kind="text", order_index=0,
            part_index=0, part_count=1, plan_digest="a" * 64, payload_digest="b" * 64,
            recipient_igsid=self.customer.igsid, provider_namespace="instagram_login:owner-1",
            settings_id_snapshot=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            client_permission_epoch=self.customer.reply_permission_epoch,
            revision_snapshot_digest=self.revision.snapshot_digest, publication_id=self.publication.pk,
            publication_version=self.publication.version, publication_hash=self.publication.snapshot_hash,
            authority_context_digest="c" * 64, state="unknown")
        self.assertEqual(self._record_failure().reason, "policy_failure_dispatch_not_excluded")
        effect.refresh_from_db()
        self.assertEqual(effect.state, "unknown")
        self.assertFalse(IgFollowUpTask.objects.filter(event_key=f"ig-revision-debt:{self.revision.pk}").exists())

    def test_native_audited_resume_uses_original_zero_dispatch_source_in_fresh_successor(self):
        from management.services.ig_manual_resume import resume_client_automation

        self.assertTrue(self._record_failure().ready)
        # Model the genuine legacy-DONE, manager-owned source used by the
        # audited Resume route. The recorder itself never changes these flags.
        self.source.status = InstagramBotMessage.Status.DONE
        self.source.save(update_fields=["status"])
        self.customer.bot_paused = True
        self.customer.manager_takeover = True
        self.customer.save(update_fields=["bot_paused", "manager_takeover"])
        actor = get_user_model().objects.create_user("policy-resume-operator", is_staff=True, is_superuser=True)
        with patch("management.services.ig_revision_live.revision_execution_enabled", return_value=True):
            resumed = resume_client_automation(self.customer.pk, actor=actor)
        self.assertTrue(resumed.successor_created, resumed.successor_reason)
        child = IgCustomerTurnRevision.objects.get(pk=resumed.successor_revision_id)
        self.assertEqual(child.origin, "manual_resume")
        self.assertEqual(list(child.sources.values_list("message_id", flat=True)), [self.source.pk])
        self.assertEqual(child.snapshot_digest, self.revision.snapshot_digest)
        self.assertIn("manual_resume_authorization", child.action_receipts)
        self.assertFalse(child.delivery_effects.exists())
        self.assertFalse(GeminiRequest.objects.filter(client_id=self.customer.pk).exists())
