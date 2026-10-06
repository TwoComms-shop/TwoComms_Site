from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from management.models import (
    AdminAuditLog,
    BotInstruction,
    GeminiRequest,
    InstagramBotSettings,
)
from management.services import call_ai_analysis
from management.services import gemini_accounting_runtime
from management.services.gemini_accounting_contract import (
    RequestPolicyManifestError,
    sanitize_request_policy_manifest,
)


def policy_manifest():
    publication = {
        "id": 3,
        "version": 7,
        "hash": "a" * 64,
        "compiler_version": "instruction-set-v1",
    }
    return {
        "version": "compiled-core-v1",
        "content_hash": "b" * 64,
        "selected_ids": ["authority:server", "instruction:17"],
        "omitted": [{"id": "instruction:18", "reason": "not_relevant"}],
        "mandatory_ids": ["authority:server", "core:published_prompt"],
        "budget_chars": 48000,
        "visual_trigger_codes": ["gift_candidate"],
        "core": {
            "version": "2026-09-07.core.v1",
            "prompt_hash": "c" * 64,
            "directives_hash": "d" * 64,
        },
        "knowledge_hash": "e" * 64,
        "instruction_publication": publication,
        "instruction_selection": {
            "selected_ids": ["instruction:17"],
            "omitted": [{"id": "instruction:18", "reason": "not_relevant"}],
            "visual_trigger_codes": ["gift_candidate"],
            "publication_id": publication["id"],
            "publication_version": publication["version"],
            "publication_hash": publication["hash"],
            "publication_compiler_version": publication["compiler_version"],
        },
    }


class RequestPolicyManifestPureTests(SimpleTestCase):
    def test_inactive_accounting_blocks_captured_context_and_keeps_legacy_null(self):
        from management.services.ig_request_manifest import capture_request_context
        manifest = {**policy_manifest(), "request_context": capture_request_context(
            payload={"contents": []}, metadata={"client_id": 3},
        )}
        kwargs = dict(request_id=None, role="chat", reasoning_task="customer_chat", candidate_plan=[])
        with patch.object(gemini_accounting_runtime, "shadow_runtime_active", return_value=False):
            observer = gemini_accounting_runtime.begin_request(**kwargs, request_policy_manifest=manifest)
            legacy = gemini_accounting_runtime.begin_request(**kwargs, request_policy_manifest=policy_manifest())
        self.assertTrue(observer.provider_blocked)
        self.assertEqual(observer.block_reason, "policy_manifest_unavailable")
        self.assertIs(legacy, gemini_accounting_runtime.NULL_OBSERVER)

    def test_strict_content_free_manifest_is_canonical(self):
        manifest = policy_manifest()

        self.assertEqual(sanitize_request_policy_manifest(manifest), manifest)

        leaking = deepcopy(manifest)
        leaking["prompt_body"] = "must never persist"
        with self.assertRaises(RequestPolicyManifestError) as caught:
            sanitize_request_policy_manifest(leaking)
        self.assertEqual(caught.exception.code, "policy_manifest_invalid")

        leaking = deepcopy(manifest)
        leaking["selected_ids"] = ["https://private.example/customer"]
        with self.assertRaises(RequestPolicyManifestError):
            sanitize_request_policy_manifest(leaking)

    def test_publication_and_selection_mismatch_is_named(self):
        manifest = policy_manifest()
        manifest["instruction_selection"]["publication_version"] += 1

        with self.assertRaises(RequestPolicyManifestError) as caught:
            sanitize_request_policy_manifest(manifest)

        self.assertEqual(caught.exception.code, "policy_manifest_mismatch")

    def test_public_chat_forwards_manifest_only_to_live_runner(self):
        manifest = policy_manifest()
        with patch.object(
            call_ai_analysis,
            "_run_chat_with_pool",
            return_value={"parsed": "ok"},
        ) as runner:
            call_ai_analysis.gemini_generate_text(
                {"contents": []},
                role="chat",
                request_policy_manifest=manifest,
            )

        self.assertIs(
            runner.call_args.kwargs["request_policy_manifest"],
            manifest,
        )

    def test_shadow_manifest_observer_null_or_creation_failure_never_calls_provider(self):
        candidate = [{
            "candidate_index": 1,
            "key_name": "test-project",
            "key_value": "test-credential",
            "model": "gemini-3.5-flash-lite",
            "project_identity": "",
            "identity_status": "unknown",
            "skip_reason": "",
        }]
        outcomes = (
            gemini_accounting_runtime.NULL_OBSERVER,
            RuntimeError("observer unavailable"),
        )
        cases = [(True, policy_manifest())]
        from management.services.ig_request_manifest import capture_request_context
        cases.append((False, {
            **policy_manifest(),
            "request_context": capture_request_context(
                payload={"contents": []}, metadata={"client_id": 3},
            ),
        }))
        for active, manifest in cases:
            for outcome in outcomes:
                with self.subTest(active=active, outcome=type(outcome).__name__), patch.object(
                    call_ai_analysis.gemini_keys,
                    "live_chat_candidate_plan",
                    return_value=candidate,
                ), patch.object(
                    call_ai_analysis.gemini_scoreboard,
                    "order_candidates",
                    side_effect=lambda rows, **_kwargs: rows,
                ), patch.object(
                    gemini_accounting_runtime,
                    "shadow_runtime_active",
                    return_value=active,
                ), patch.object(
                    gemini_accounting_runtime,
                    "begin_request",
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                    return_value=(
                        outcome if not isinstance(outcome, Exception) else None
                    ),
                ), patch.object(call_ai_analysis.requests, "post") as provider:
                    with self.assertRaises(call_ai_analysis.CallAIAnalysisError) as caught:
                        call_ai_analysis.gemini_generate_text(
                            {"contents": []},
                            role="chat",
                            model_chain_override=["gemini-3.5-flash-lite"],
                            request_policy_manifest=manifest,
                        )

                provider.assert_not_called()
                self.assertEqual(
                    caught.exception.failure_kind,
                    "policy_manifest_unavailable",
                )


class RequestPolicyManifestGraphTests(TestCase):
    def _plan(self):
        return [{
            "candidate_index": 1,
            "key_name": "(manual)",
            "key_value": "test-credential",
            "model": "gemini-3.5-flash-lite",
            "project_identity": "",
            "identity_status": "unknown",
            "skip_reason": "",
        }]

    @patch.object(gemini_accounting_runtime, "shadow_runtime_active", return_value=True)
    def test_parent_graph_stores_once_and_reuse_never_overwrites(self, _shadow):
        manifest = policy_manifest()
        audit_count = AdminAuditLog.objects.count()
        observer = gemini_accounting_runtime.begin_request(
            request_id="manifest-request",
            role="chat",
            reasoning_task="customer_chat",
            candidate_plan=self._plan(),
            request_policy_manifest=manifest,
        )

        self.assertTrue(observer.enabled)
        graph = GeminiRequest.objects.get(request_id="manifest-request")
        self.assertEqual(graph.policy_manifest, manifest)
        self.assertEqual(AdminAuditLog.objects.count(), audit_count)

        changed = deepcopy(manifest)
        changed["content_hash"] = "f" * 64
        rejected = gemini_accounting_runtime.begin_request(
            request_id="manifest-request",
            role="chat",
            reasoning_task="customer_chat",
            candidate_plan=self._plan(),
            request_policy_manifest=changed,
        )
        self.assertTrue(rejected.provider_blocked)
        self.assertEqual(rejected.block_reason, "policy_manifest_mismatch")
        graph.refresh_from_db()
        self.assertEqual(graph.policy_manifest, manifest)

    @patch.object(gemini_accounting_runtime, "shadow_runtime_active", return_value=False)
    def test_accounting_off_creates_no_parent_graph(self, _shadow):
        observer = gemini_accounting_runtime.begin_request(
            request_id="manifest-accounting-off",
            role="chat",
            reasoning_task="customer_chat",
            candidate_plan=self._plan(),
            request_policy_manifest=policy_manifest(),
        )

        self.assertFalse(observer.enabled)
        self.assertFalse(GeminiRequest.objects.filter(
            request_id="manifest-accounting-off"
        ).exists())

    def test_model_contract_rejects_noncanonical_manifest(self):
        request = GeminiRequest(
            request_id="invalid-manifest-model",
            candidate_plan=[],
            policy_manifest={"body": "not metadata"},
        )
        with self.assertRaises(ValidationError):
            request.save()

    def test_reused_worker_settings_refreshes_and_binds_one_current_publication(self):
        from management.services import instagram_bot
        from management.services.ig_policy_publication import (
            load_active_policy_snapshot,
        )
        from management.tests_ig_policy_helpers import publish_current_instructions

        BotInstruction.objects.all().delete()
        instruction = BotInstruction.objects.create(
            title="First", body="First publication", intent_tags="global"
        )
        publish_current_instructions()
        stale_settings = InstagramBotSettings.objects.get(pk=1)
        first_publication_id = stale_settings.active_instruction_publication_id
        instruction.body = "Second publication"
        instruction.save(update_fields=["body", "updated_at"])
        second_snapshot = publish_current_instructions()
        self.assertNotEqual(first_publication_id, second_snapshot.publication_id)

        manifest = policy_manifest()
        publication = {
            "id": second_snapshot.publication_id,
            "version": second_snapshot.version,
            "hash": second_snapshot.snapshot_hash,
            "compiler_version": second_snapshot.compiler_version,
        }
        manifest["instruction_publication"] = publication
        manifest["instruction_selection"].update({
            "publication_id": publication["id"],
            "publication_version": publication["version"],
            "publication_hash": publication["hash"],
            "publication_compiler_version": publication["compiler_version"],
        })
        bound = []

        def assemble(_settings, **kwargs):
            bound.append(kwargs["instruction_publication"])
            kwargs["compiled_metadata"].update(deepcopy(manifest))
            return "system"

        provider_result = {
            "parsed": {"reply_text": "Чим можу допомогти?", "controls": []},
            "model": "gemini-test",
            "usage": {},
            "meta": {"key": "test", "reasoning_task": "customer_chat"},
        }
        with patch.object(
            instagram_bot,
            "assemble_system_instruction",
            side_effect=assemble,
        ), patch(
            "management.services.ig_policy_publication.load_active_policy_snapshot",
            wraps=load_active_policy_snapshot,
        ) as load_snapshot, patch(
            "management.services.call_ai_analysis.gemini_generate_text",
            return_value=provider_result,
        ) as provider:
            result = instagram_bot.gemini_generate(
                stale_settings,
                [{"role": "user", "text": "Привіт"}],
            )

        self.assertTrue(result.valid)
        self.assertEqual(load_snapshot.call_count, 1)
        self.assertEqual(bound[0].publication_id, second_snapshot.publication_id)
        self.assertEqual(
            stale_settings.active_instruction_publication_id,
            second_snapshot.publication_id,
        )
        self.assertEqual(
            provider.call_args.kwargs["request_policy_manifest"],
            manifest,
        )
