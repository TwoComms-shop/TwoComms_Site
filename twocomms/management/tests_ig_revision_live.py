import hashlib
import base64
import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    BotPolicyPublication, GeminiRequest, GeminiRequestAttempt, IgClient,
    IgCustomerTurn, IgCustomerTurnRevision, IgTurnMessage,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services.gemini_accounting_contract import sanitize_request_policy_manifest
from management.services.ig_revision_execution import prepare_revision
from management.services.ig_revision_live import (
    RevisionGenerationBoundary, build_sealed_history, capture_revision_source,
    execute_claimed_revision, legacy_claimable_messages, process_pending_revisions,
    revision_execution_enabled,
)
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionLiveTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        snapshot = {"schema_version": 1, "instructions": []}
        self.publication = BotPolicyPublication.objects.create(
            version=1, kind=BotPolicyPublication.Kind.PUBLISH, schema_version=1,
            snapshot=snapshot,
            snapshot_hash=hashlib.sha256(json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
            compiler_version="instruction-set-v1", instruction_count=0,
        )
        self.settings = InstagramBotSettings.objects.create(
            pk=1, is_enabled=True, ai_enabled=True, ig_user_id="owner-1",
            active_instruction_publication=self.publication,
        )
        self.customer = IgClient.objects.create(igsid="17840000000001", language="uk")
        self.source = self._message("Що ви можете порадити?", "first")
        self.turn = IgCustomerTurn.objects.create(
            client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(
            self.turn, [self.source], bypass_quiet=True,
        ).revision
        self.token = ""
        self.parsed = {"reply_text": "Можу допомогти з вибором. Який стиль вам подобається?", "controls": []}
        self.generation_calls = []
        self.before_validate = None
        self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = "observed"
        self.provider_failure = False

    def _message(self, text, mid):
        return InstagramBotMessage.objects.create(
            client=self.customer, sender_id=self.customer.igsid, mid=mid,
            provider_namespace="instagram_login:owner-1", role="user",
            source="webhook", text=text, status=InstagramBotMessage.Status.PENDING,
        )

    def _prepare(self):
        prepared = prepare_revision(self.revision.pk, lambda **kwargs: None)
        self.assertTrue(prepared.ready, prepared.reason)
        self.token = prepared.execution_token
        self.revision.refresh_from_db()
        self.revision.client = self.customer

    def _generate(self, payload, **kwargs):
        from management.services.ig_turn_lineage import current_context

        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(kwargs["max_actual_dispatches"], 8)
        self.assertFalse(kwargs["legacy_provider_root"])
        self.assertLessEqual(kwargs["deadline_seconds"], 40)
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertNotIn("responseSchema", payload["generationConfig"])
        self.generation_calls.append(payload)
        context = current_context()
        revision_id = int(context["logical_turn_id"].rsplit(":", 1)[1])
        # This stub replaces begin_request too; preserve its durable frozen
        # route contract so outage fixtures exercise real recovery admission.
        from management.services.ig_revision_provider_execution import revision_provider_continuation
        from management.tests_gemini_accounting_shadow import _raw_plan

        owner = IgCustomerTurnRevision.objects.get(pk=revision_id)
        frozen = revision_provider_continuation(owner.pk, owner.claim_token,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            candidate_plan=_raw_plan())
        self.assertTrue(frozen.ready, frozen.reason)
        if self.provider_failure:
            from management.services.call_ai_analysis import CallAIAnalysisError

            graph = GeminiRequest.objects.create(
                request_id=f"live-request-{revision_id}", lane="live", task_class="simple_live",
                logical_turn_id=context["logical_turn_id"], source_execution_key=context["logical_turn_id"],
                client_id=self.customer.pk, source_message_id=context["source_message_id"],
                policy_manifest=sanitize_request_policy_manifest(kwargs["request_policy_manifest"]),
                accounting_mode="shadow", terminal_resolution="failed", terminal_reason="provider_outage",
            )
            GeminiRequestAttempt.objects.create(request_id=graph.request_id, request_graph=graph, role="chat", key_name="GEMINI_API", model="gemini-3.7-flash", outcome="transient", fsm_state="failed", http_code=503, logical_turn_id=graph.logical_turn_id, client_id=graph.client_id, source_message_id=graph.source_message_id, lane="live", attempt_index=1, candidate_index=1, accounting_mode="shadow")
            raise CallAIAnalysisError("http_5xx")
        if self.before_validate:
            self.before_validate()
        actual_hashes = [
            hashlib.sha256(base64.b64decode(part["inline_data"]["data"])).hexdigest()
            for content in payload["contents"] for part in content["parts"] if "inline_data" in part
        ][:self.actual_inline_count]
        usage = {"_request_inline_count": self.actual_inline_count}
        if self.actual_hash_override == "observed":
            usage["_request_inline_content_hashes"] = actual_hashes
        elif self.actual_hash_override is not None:
            usage["_request_inline_content_hashes"] = self.actual_hash_override
        decision = kwargs["result_validator"](self.parsed, usage=usage)
        if not decision.valid:
            from management.services.call_ai_analysis import CallAIAnalysisError

            self.assertIsNone(kwargs["repair_payload_factory"](payload, self.parsed, decision.reason_codes))
            raise CallAIAnalysisError("failed deterministic result validation")
        context = current_context()
        request_id = f"live-request-{revision_id}"
        model = "gemini-3.7-flash"
        graph = GeminiRequest.objects.create(
            request_id=request_id, lane="live", task_class="simple_live",
            logical_turn_id=context["logical_turn_id"], client_id=self.customer.pk,
            source_execution_key=context["logical_turn_id"],
            source_message_id=context["source_message_id"],
            policy_manifest=sanitize_request_policy_manifest(kwargs["request_policy_manifest"]),
            accounting_mode=GeminiRequest.AccountingMode.SHADOW,
        )
        winner = GeminiRequestAttempt.objects.create(
            request_id=request_id, request_graph=graph, role="chat", key_name="GEMINI_API",
            model=model, outcome="succeeded", fsm_state="succeeded", winner_claimed=True,
            logical_turn_id=graph.logical_turn_id, client_id=graph.client_id,
            source_message_id=graph.source_message_id, lane="live",
            attempt_index=1, candidate_index=1, accounting_mode=graph.accounting_mode,
        )
        graph.winner_attempt = winner
        graph.terminal_resolution = "succeeded"
        graph.save(update_fields=["winner_attempt", "terminal_resolution", "updated_at"])
        if self.after_validate:
            self.after_validate()
        return {
            "parsed": self.parsed, "model": model,
            "meta": {"request_id": request_id}, "usage": usage,
        }

    def _execute(self, send_results=None):
        provider_results = send_results or [(200, json.dumps({"message_id": "sent-1"}))]
        with (
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate,
            patch("management.services.instagram_bot.get_page_token", return_value="memory-token"),
            patch("management.services.instagram_bot._provider_http", side_effect=provider_results) as http,
            patch("management.services.instagram_bot._register_outgoing_message"),
        ):
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        return result, generate, http

    def test_default_off_shadows_remain_legacy_but_prepared_sources_are_sticky(self):
        from management.services.instagram_bot import _claim_exact_row

        self.assertFalse(revision_execution_enabled())
        self.assertTrue(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=self.source.pk)).exists())
        self._prepare()
        self.assertFalse(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=self.source.pk)).exists())
        self.assertIsNone(_claim_exact_row(self.source))
        self.source.refresh_from_db()
        self.assertEqual(self.source.attempts, 0)
        self.assertEqual(self.source.status, "pending")

    def test_sealed_history_preserves_original_tail_and_excludes_new_input(self):
        self._prepare()
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="mutable changed caption")
        newer = self._message("new incoming cannot leak", "newer")
        history = build_sealed_history(self.revision)
        text = json.dumps(history, ensure_ascii=False)
        self.assertIn("Що ви можете порадити?", text)
        self.assertNotIn("mutable changed caption", text)
        self.assertNotIn(newer.text, text)

    def test_capture_wrapper_passes_exact_deadline_without_transaction(self):
        deadline = timezone.now() + timedelta(seconds=3)
        calls = []
        with patch("management.services.instagram_bot._capture_message_media", side_effect=lambda row, **kwargs: calls.append((row.pk, kwargs, connection.in_atomic_block))):
            capture_revision_source(message_id=self.source.pk, deadline_at=deadline, remaining_seconds=3)
        self.assertEqual(calls, [(self.source.pk, {"deadline_at": deadline}, False)])

    def test_real_proposal_graph_to_immutable_plan_to_single_receipt(self):
        self._prepare()
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertTrue(self.revision.generation_proposal_digest)
        self.assertEqual(self.revision.state, "processed")
        source = self.revision.generation_proposal["sources"][0]
        self.assertEqual(source["message_id"], self.source.pk)
        receipt = self.revision.delivery_effects.get()
        self.assertEqual(receipt.state, "sent")
        self.assertEqual(receipt.provider_message_id, "sent-1")
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", source="revision_reply").count(), 1)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "done")
        self.assertEqual(self.source.attempts, 0)
        self.assertEqual(self.source.turn_intelligence_artifact, {})

    def test_pre_winner_permission_change_rejects_generation_without_send(self):
        self._prepare()
        self.before_validate = lambda: IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked")
        self.assertIn("client_permission_changed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertFalse(GeminiRequest.objects.exists())

    def test_late_permission_change_after_winner_never_dispatches_third_request_or_send(self):
        self._prepare()
        self.after_validate = lambda: IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked")
        self.assertIn("client_permission_changed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_partial_unknown_is_sticky_after_flag_rollback_and_never_repeats_part_one(self):
        self._prepare()
        self.parsed["reply_text"] = ("Можу допомогти з вибором стилю та кольору. " * 29).strip()
        result, generate, http = self._execute([
            (200, json.dumps({"message_id": "part-one"})), TimeoutError(),
        ])
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.assertEqual(result.sent_parts, 1)
        self.assertEqual(http.call_count, 2)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.state, "claimed")
        self.revision.lease_until = timezone.now() - timedelta(seconds=1)
        self.revision.save(update_fields=["lease_until", "updated_at"])
        with (
            patch("management.services.call_ai_analysis.gemini_generate_text") as retry_generation,
            patch("management.services.instagram_bot.get_page_token", return_value="memory-token"),
            patch("management.services.instagram_bot._provider_http") as retry_send,
            patch("management.services.instagram_bot.maintenance_status", return_value={"active": False}),
        ):
            count = process_pending_revisions(self.settings, create_new=False)
        self.assertEqual(count, 0)
        retry_generation.assert_not_called()
        retry_send.assert_not_called()
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", source="revision_reply").count(), 1)
        self.assertFalse(legacy_claimable_messages(InstagramBotMessage.objects.filter(pk=self.source.pk)).exists())

    def test_public_policy_change_cannot_repair_old_request_manifest(self):
        self._prepare()
        boundary = RevisionGenerationBoundary(
            self.revision, self.token, self.settings,
            PublicationBinding(self.publication.pk, 1, self.publication.snapshot_hash),
        )
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(knowledge_base="New public instructions")
        result = boundary.check()
        self.assertIn("public_policy_inputs_stale", result.reasons)
        self.assertIsNone(boundary.repair({}, {}, result.reasons, base_repair=lambda *_args: {}))

    def _prepare_two_images(self):
        second = self._message("І що видно тут?", "second-photo")
        body = b"owned-fixture-image"
        content_hash = hashlib.sha256(body).hexdigest()
        for index, row in enumerate((self.source, second)):
            row.private_media_state = InstagramBotMessage.PrivateMediaState.ACTIVE
            row.attachment_media = [{
                "source_part_id": "mp1_" + str(index + 1) * 32,
                "original_index": 0, "identity_origin": "ingress",
                "type": "image", "status": "owned", "mime": "image/jpeg",
                "bytes": len(body), "content_hash": content_hash,
                "private_storage": True, "storage_name": f"ig-private/source-{row.pk}",
            }]
            row.save(update_fields=["private_media_state", "attachment_media"])
        IgTurnMessage.objects.create(turn=self.turn, message=second, ordinal=2, role="user")
        self.revision = create_collecting_revision(
            self.turn, [self.source, second], bypass_quiet=True,
        ).revision
        self._prepare()
        self.actual_inline_count = 2
        self.parsed["reply_text"] = "На першому фото видно людину, на другому — документ. Що саме вас цікавить?"
        self.parsed["turn_intelligence"] = {
            "catalog_candidates": [], "intent": "visual_question", "confidence": 0.9,
            "audio_status": "not_applicable", "transcript": "",
            "image_observations": [
                {"source_image_index": index, "outcome": "understood", "evidence_code": "visual_content", "type_code": kind}
                for index, kind in enumerate(("selfie", "document"))
            ],
        }
        return second, body

    def test_two_sources_analyze_every_image_and_project_only_exact_parts(self):
        second, body = self._prepare_two_images()
        with patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", body)):
            result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        sources = self.revision.generation_proposal["turn_intelligence"]["media_request"]["submitted_parts"]
        self.assertEqual([item["source_message_id"] for item in sources], [self.source.pk, second.pk])
        for index, source in enumerate((self.source, second)):
            source.refresh_from_db()
            inspection = source.attachment_media[0]["inspection"]
            self.assertEqual(inspection["source_image_index"], index)
            self.assertEqual(inspection["revision_id"], self.revision.pk)
            self.assertEqual(source.turn_intelligence_artifact, {})
        prompt = self.generation_calls[0]
        inline = [part for part in prompt["contents"][-1]["parts"] if "inline_data" in part]
        self.assertEqual(len(inline), 2)

    def test_actual_missing_hash_evidence_fails_closed(self):
        _second, body = self._prepare_two_images()
        self.actual_hash_override = None
        with patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", body)):
            result, _generate, http = self._execute()
        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reasons, ("invalid_response",))
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertFalse(GeminiRequest.objects.exists())

    def test_actual_changed_hash_evidence_fails_closed(self):
        _second, body = self._prepare_two_images()
        self.actual_hash_override = ["a" * 64, "b" * 64]
        with patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", body)):
            result, _generate, http = self._execute()
        self.assertEqual(result.state, "blocked")
        self.assertEqual(result.reasons, ("invalid_response",))
        http.assert_not_called()
        self.assertFalse(GeminiRequest.objects.exists())

    def test_trimmed_suffix_keeps_prepared_source_with_only_actual_hash_evidence(self):
        second, body = self._prepare_two_images()
        self.actual_inline_count = 1
        self.parsed["turn_intelligence"]["image_observations"] = self.parsed["turn_intelligence"]["image_observations"][:1]
        with patch("management.services.instagram_bot._owned_media_bytes", return_value=("image/jpeg", body)):
            result, _generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        media = self.revision.generation_proposal["request_media_manifest"]
        self.assertEqual(len(media["items"]), 2)
        self.assertEqual(media["actual_inline_count"], 1)
        self.assertEqual(media["actual_content_hashes"], [hashlib.sha256(body).hexdigest()])
        second.refresh_from_db()
        self.assertEqual(second.attachment_media[0]["inspection"]["state"], "uninspected")
        self.assertEqual(http.call_count, 1)

    def test_recover_proposal_keeps_original_settings_epoch(self):
        self._prepare()
        with (
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate),
            patch("management.services.instagram_bot.get_page_token", return_value=""),
        ):
            first = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(first.reasons, ("provider_not_configured",))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["execution_binding"]["settings_permission_epoch"], 0)
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(reply_permission_epoch=1)
        self.settings.refresh_from_db()
        with (
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
            patch("management.services.instagram_bot._provider_http") as http,
        ):
            second = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(second.state, "blocked")
        self.assertIn("settings_permission_changed", second.reasons)
        generate.assert_not_called()
        http.assert_not_called()

    def test_selection_replay_uses_original_proposal_and_post_action_authority(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product

        category = Category.objects.create(name="Revision live", slug="revision-live")
        product = Product.objects.create(
            title="Revision live product", slug="revision-live-product", category=category,
            price=900, status="published",
        )
        color = Color.objects.create(name="Revision live black", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(product=product, color=color, price_override=900, is_default=True)
        self.parsed["controls"] = [
            {"kind": "product", "value": str(product.pk)},
            {"kind": "color_variant_id", "value": str(variant.pk)},
            {"kind": "qty", "value": "2"},
        ]
        self._prepare()
        with (
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate),
            patch("management.services.instagram_bot.get_page_token", return_value=""),
        ):
            first = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(first.reasons, ("provider_not_configured",))
        self.revision.refresh_from_db()
        receipt = json.dumps(self.revision.action_receipts, sort_keys=True)
        self.assertIn("client_configuration_update", self.revision.action_receipts)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, product.pk)
        self.assertEqual(self.customer.current_qty, 2)
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generation.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        for key, value in json.loads(receipt).items():
            self.assertEqual(self.revision.action_receipts[key], value)

    def test_completed_receipt_recovery_finishes_only_local_projection(self):
        self._prepare()
        with patch("management.services.ig_revision_live._project_completed_sources", side_effect=RuntimeError("crash after completion")):
            result, _generation, http = self._execute()
        self.assertEqual(result.state, "finalization_pending", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.state, "claimed")
        self.assertTrue(self.revision.claim_token.startswith("finalize:"))
        self.revision.lease_until = timezone.now() - timedelta(seconds=1)
        self.revision.save(update_fields=["lease_until", "updated_at"])
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        with (
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
            patch("management.services.instagram_bot.get_page_token") as token,
            patch("management.services.instagram_bot._provider_http") as http,
        ):
            count = process_pending_revisions(self.settings, create_new=False)
        self.assertEqual(count, 1)
        generate.assert_not_called()
        token.assert_not_called()
        http.assert_not_called()
        self.source.refresh_from_db()
        self.turn.refresh_from_db()
        self.assertEqual(self.source.status, "done")
        self.assertEqual(self.turn.terminal_reason, IgCustomerTurn.TerminalReason.REPLIED)

    def test_cancellation_after_plan_keeps_owed_sources_for_successor(self):
        from management.services.ig_revision_outbox import plan_revision_effects
        from management.services.ig_turn_revisions import create_refresh_successor

        self._prepare()
        successor_publication = BotPolicyPublication.objects.create(
            version=2, kind=BotPolicyPublication.Kind.PUBLISH, schema_version=1,
            snapshot=self.publication.snapshot, snapshot_hash=self.publication.snapshot_hash,
            compiler_version="instruction-set-v1", instruction_count=0,
        )

        def plan_then_change(*args, **kwargs):
            result = plan_revision_effects(*args, **kwargs)
            InstagramBotSettings.objects.filter(pk=self.settings.pk).update(active_instruction_publication=successor_publication)
            return result

        with patch("management.services.ig_revision_live.plan_revision_effects", side_effect=plan_then_change):
            result, generation, http = self._execute()
        self.assertEqual(result.state, "cancelled", result.reasons)
        self.assertIn("publication_changed", result.reasons)
        http.assert_not_called()
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")
        successor = create_refresh_successor(self.revision.pk, self.token, reason="publication_changed")
        self.assertTrue(successor.created, successor.reason)
        self.assertEqual(successor.revision.bundle_snapshot, self.revision.bundle_snapshot)

    def _replace_bundle(self, texts):
        sources = []
        for index, text in enumerate(texts):
            if index == 0:
                source = self.source
                source.text = text
                source.save(update_fields=["text"])
            else:
                source = self._message(text, f"bundle-{self.revision.pk}-{index}")
                IgTurnMessage.objects.create(turn=self.turn, message=source, ordinal=index + 1, role="user")
            sources.append(source)
        self.revision = create_collecting_revision(self.turn, sources, bypass_quiet=True).revision
        self._prepare()

    def test_question_survives_trailing_reaction_in_sealed_bundle(self):
        self._replace_bundle(["Які кольори доступні?", "👍"])
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(http.call_count, 1)

    def test_reaction_only_has_durable_no_model_decision(self):
        self._replace_bundle(["👍", "❤️"])
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(result.reasons, ("reaction_only",))
        generation.assert_not_called()
        http.assert_not_called()
        self.assertFalse(GeminiRequest.objects.exists())
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["input_decision"]["origin"], "no_reply")
        self.assertFalse(self.revision.generation_proposal_digest)

    def test_static_reply_trigger_survives_trailing_reaction_without_fake_gemini(self):
        self.settings.ai_enabled = False
        self.settings.trigger_text = "start"
        self.settings.reply_text = "Вітаємо! Чим можемо допомогти?"
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        self._replace_bundle(["start", "👍"])
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generation.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertFalse(GeminiRequest.objects.exists())
        self.revision.refresh_from_db()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.payload["message"]["text"], self.settings.reply_text)
        self.assertEqual(effect.generation_request_id, "")
        self.assertEqual(self.revision.action_receipts["input_decision"]["origin"], "static_reply")

    def test_static_effect_replay_rechecks_current_response_behavior(self):
        self.settings.ai_enabled = False
        self.settings.trigger_text = "start"
        self.settings.reply_text = "Перший налаштований текст."
        self.settings.save(update_fields=["ai_enabled", "trigger_text", "reply_text"])
        self._replace_bundle(["start"])
        with patch("management.services.ig_revision_live._drain_effects", side_effect=RuntimeError("crash after static plan")):
            with self.assertRaises(RuntimeError):
                self._execute()
        self.assertTrue(self.revision.delivery_effects.exists())
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(reply_text="Оновлений налаштований текст.")
        result, generation, http = self._execute()
        self.assertEqual(result.state, "cancelled", result.reasons)
        self.assertIn("fact_binding_unavailable", result.reasons)
        generation.assert_not_called()
        http.assert_not_called()
        self.assertFalse(GeminiRequest.objects.exists())

    def test_rate_admission_counts_once_and_receipts_cannot_change(self):
        from management.services.ig_revision_input import decide_revision_input

        self._prepare()
        with patch("management.services.ig_revision_input.RATE_LIMIT", 1):
            first = decide_revision_input(self.revision.pk, self.token, settings_id=self.settings.pk)
            repeat = decide_revision_input(self.revision.pk, self.token, settings_id=self.settings.pk)
            self.assertEqual(first.origin, "generate")
            self.assertTrue(repeat.replayed)
            self.assertEqual(first.receipt, repeat.receipt)
            self.revision.refresh_from_db()
            receipts = dict(self.revision.action_receipts)
            self.revision.action_receipts = {"input_decision": {**receipts["input_decision"], "origin": "static_reply"}}
            with self.assertRaises(ValueError):
                self.revision.save(update_fields=["action_receipts"])
            source = self._message("Ще одне питання?", "rate-next")
            turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source, window_started_at=timezone.now(), window_deadline=timezone.now())
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
            revision = create_collecting_revision(turn, [source], bypass_quiet=True).revision
            prepared = prepare_revision(revision.pk, lambda **_kwargs: None)
            limited = decide_revision_input(revision.pk, prepared.execution_token, settings_id=self.settings.pk)
        self.assertEqual(limited.origin, "no_reply")
        self.assertEqual(limited.reason, "rate_limited")
        self.assertFalse(GeminiRequest.objects.exists())

    def _checkout_bundle(self, *, two_items=False):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product

        category = Category.objects.create(name="Checkout live", slug="checkout-live")
        color = Color.objects.create(name="Checkout black", primary_hex="#111111")
        controls = []
        for index in range(2 if two_items else 1):
            product = Product.objects.create(title=f"Checkout product {index}", slug=f"checkout-product-{index}", category=category, price=900 + index * 100, status="published")
            variant = ProductColorVariant.objects.create(product=product, color=color, price_override=900 + index * 100, is_default=True)
            controls.append({"kind": "item", "value": f"{product.pk}|1|M||{variant.pk}"})
        controls.append({"kind": "paylink", "value": "full"})
        self.parsed["controls"] = controls
        self.parsed["reply_text"] = "Можна переходити до оформлення."
        self._replace_bundle(["Беру. Оформлюйте замовлення.", "👍"])

    def test_standard_checkout_uses_sealed_purchase_source_and_exact_token_plan(self):
        from management.models import IgCheckoutAccessToken

        self._checkout_bundle()
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
        self.revision.refresh_from_db()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.source_message_id, self.source.pk)
        self.assertIn("https://", effect.payload["message"]["text"])
        self.assertNotIn("client_configuration_update", self.revision.action_receipts)

    def test_two_item_checkout_creates_current_cart_without_duplicate_token_on_resume(self):
        from management.models import IgCheckoutAccessToken, IgCheckoutProposal

        self._checkout_bundle(two_items=True)
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(IgCheckoutProposal.objects.get().items.count(), 2)
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)
        resumed, generation, http = self._execute()
        generation.assert_not_called()
        http.assert_not_called()
        self.assertEqual(IgCheckoutAccessToken.objects.count(), 1)

    def test_shown_product_projection_uses_only_frozen_sent_effect_metadata(self):
        from management.services.ig_revision_live import _project_sent_history
        from storefront.models import Category, Product

        self._prepare()
        category = Category.objects.create(name="Frozen assets", slug="frozen-assets")
        for index in range(2):
            Product.objects.create(pk=index + 41, title=f"Frozen title {index}", slug=f"frozen-product-{index}", category=category, price=900, status="published", main_image=f"image-{index}.jpg")
        media = tuple({
            "group": "catalog_media", "kind": "image",
            "payload": {"recipient": {"id": self.customer.igsid}, "message": {"attachment": {
                "type": "image", "payload": {"url": f"https://twocomms.shop/media/image-{index}.jpg", "is_reusable": True},
            }}},
            "projection_metadata": {"part_index": index, "product_id": index + 41, "title": f"Frozen title {index}"},
        } for index in range(2))
        with patch("management.services.ig_revision_live._prepare_visible_content", return_value=(media, self.parsed["reply_text"], ())):
            result, _generation, http = self._execute([
                (200, json.dumps({"message_id": "image-one"})), TimeoutError(),
                (200, json.dumps({"message_id": "text-one"})),
            ])
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.assertEqual(http.call_count, 3)
        _project_sent_history(self.revision.pk)
        self.customer.refresh_from_db()
        shown = self.customer.sales_context["shown_products"]
        self.assertEqual(shown["items"], [{"position": 1, "product_id": 41, "title": "Frozen title 0"}])
        self.assertEqual(shown["source_message_watermark"], self.source.pk)
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", source="revision_reply").count(), 2)

    def test_model_stage_spam_order_are_advisory_and_useful_reply_delivers(self):
        initial_stage = self.customer.stage
        self.parsed["controls"] = [{"kind": "stage", "value": "qualifying"}, {"kind": "spam", "value": True}, {"kind": "order", "value": True}]
        self._prepare()
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.customer.refresh_from_db()
        self.revision.refresh_from_db()
        self.assertEqual(self.customer.stage, initial_stage)
        self.assertFalse(self.customer.is_blocked)
        self.assertFalse(self.customer.bot_paused)
        self.assertEqual(self.revision.generation_proposal["response"]["controls"], self.parsed["controls"])
        self.assertEqual(self.revision.generation_proposal["authority"]["allowed_actions"], [])

    def test_manager_promise_has_deduplicated_local_case_before_customer_send(self):
        from management.models import IgBotNotification, IgFollowUpTask

        self._replace_bundle(["Покличте менеджера, будь ласка."])
        self.parsed["reply_text"] = "Передам ваш запит менеджеру."
        self.parsed["controls"] = [{"kind": "manager", "value": True}]
        initial_stage = self.customer.stage
        with (
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate),
            patch("management.services.instagram_bot.get_page_token", return_value=""),
            patch("management.services.instagram_bot._deliver_manager_notification") as deliver_notification,
        ):
            first = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(first.reasons, ("provider_not_configured",))
        deliver_notification.assert_not_called()
        self.assertEqual(IgFollowUpTask.objects.filter(reason="revision_case:manager_handoff").count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.get().status, "pending")
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generation.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertEqual(IgBotNotification.objects.count(), 1)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.stage, initial_stage)

    def test_custom_checkout_request_creates_brief_without_unpriced_token(self):
        from management.models import IgCheckoutAccessToken, IgFollowUpTask

        self._replace_bundle(["Хочу власний принт з моїм малюнком."])
        self.parsed["reply_text"] = "Передам запит команді, щоб перевірити можливість і вартість."
        self.parsed["controls"] = [{"kind": "paylink", "value": "full"}]
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        self.assertFalse(IgCheckoutAccessToken.objects.exists())
        task = IgFollowUpTask.objects.get(reason="revision_case:custom_print")
        self.assertEqual(task.manager_context["required_decisions"], ["design_feasibility", "quote_approval"])
        self.assertEqual(task.manager_context["sources"][0]["message_id"], self.source.pk)
        self.assertFalse(task.manager_context["authority"]["price_confirmed"])

    def test_rate_alert_is_deferred_db_intent_and_does_not_send_customer_reply(self):
        from management.models import IgBotNotification

        self._prepare()
        with (
            patch("management.services.ig_revision_input.RATE_LIMIT", 0),
            patch("management.services.instagram_bot._deliver_manager_notification") as deliver_notification,
        ):
            result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(result.reasons, ("rate_limited",))
        generation.assert_not_called()
        http.assert_not_called()
        deliver_notification.assert_not_called()
        self.assertEqual(IgBotNotification.objects.get().event_type, "sender_rate_limited")
        self.assertEqual(IgBotNotification.objects.get().status, "pending")

    def test_diagnostic_postback_is_deterministic_and_has_no_gemini_graph(self):
        self.source.quick_reply_payload = f"twc:1:diagnostic:inout:{self.customer.pk}"
        self.source.save(update_fields=["quick_reply_payload"])
        self._replace_bundle(["IN ✅"])
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generation.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertFalse(GeminiRequest.objects.exists())
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["postback_decision"]["origin"], "postback")

    def test_optional_follow_candidate_is_preserved_but_not_sent(self):
        self._prepare()
        self.parsed["follow_cta"] = {"include": True, "text": "Якщо захочете, нові колекції можна переглядати на нашій сторінці."}
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.revision.refresh_from_db()
        self.assertIn("follow_cta", self.revision.generation_proposal["response"])
        payload = self.revision.delivery_effects.get().payload
        self.assertEqual(payload["message"]["text"], self.parsed["reply_text"])

    def test_mixed_parcel_got_and_question_keeps_action_and_one_model_answer(self):
        from management.models import IgDeal, IgFollowUpTask, IgSourceActionReceipt
        from orders.models import Order

        order = Order.objects.create(full_name="Parcel buyer", phone="+380501112233", city="Kyiv", np_office="1", total_sum=900, payment_status="prepaid", status="ship")
        deal = IgDeal.objects.create(client=self.customer, order=order, amount=900)
        reminder = IgFollowUpTask.objects.create(client=self.customer, deal=deal, kind="manager_task", trigger="reactive", reason=f"parcel_reminder:{order.pk}", due_at=timezone.now() + timedelta(hours=1))
        self.source.quick_reply_payload = f"twc:1:parcel:got:{order.pk}"
        self.source.save(update_fields=["quick_reply_payload"])
        self._replace_bundle(["Забрав ✅", "А які кольори худі доступні?"])
        result, generation, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generation.call_count, 1)
        self.assertEqual(http.call_count, 1)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "cancelled")
        self.assertEqual(IgSourceActionReceipt.objects.count(), 1)
        self.assertIn("А які кольори худі доступні?", json.dumps(self.generation_calls[0], ensure_ascii=False))
        self.assertIn("AUTHORITATIVE SOURCE ACTIONS ALREADY COMPLETED", json.dumps(self.generation_calls[0]))

    def test_next_source_does_not_restore_selection_from_stale_commerce_session(self):
        from management.models import IgCommerceSelectionSession, IgCommerceSelectionTransition, IgCommerceTurnDecision
        from management.services.ig_revision_commerce import reduce_revision_commerce

        self._checkout_bundle()
        item = self.parsed["controls"][0]["value"].split("|")
        product_id, variant_id = int(item[0]), int(item[4])
        self.parsed["controls"] = [{"kind": "product", "value": str(product_id)}, {"kind": "color_variant_id", "value": str(variant_id)}, {"kind": "qty", "value": "2"}]
        result, _generation, _http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual(session.lines[session.active_index]["product_id"], product_id)
        old_transitions = list(IgCommerceSelectionTransition.objects.values_list("pk", "to_revision"))
        source = self._message("Дякую, ще уточню деталі.", "selection-next-source")
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source, window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [source], bypass_quiet=True).revision
        prepared = prepare_revision(revision.pk, lambda **_kwargs: None)
        first = reduce_revision_commerce(revision.pk, prepared.execution_token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        repeated = reduce_revision_commerce(revision.pk, prepared.execution_token, settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch, publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash))
        self.assertTrue(first.ready, first.reason)
        self.assertTrue(repeated.replayed)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.current_product_id, product_id)
        self.assertEqual(self.customer.current_qty, 2)
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=source).count(), 1)
        self.assertEqual(list(IgCommerceSelectionTransition.objects.filter(pk__in=[row[0] for row in old_transitions]).values_list("pk", "to_revision")), old_transitions)

    def test_followup_failure_stays_recoverable_after_deadline_without_another_http(self):
        from management.services.ig_revision_followups import RevisionFollowupResult
        from management.services.ig_revision_execution import finalize_sent_revision_effects

        self._prepare()
        with patch("management.services.ig_revision_followups.settle_revision_normal_followups", return_value=RevisionFollowupResult(reason="normal_followup_failed")):
            first, _generation, http = self._execute()
        self.assertEqual(first.state, "finalization_pending")
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.state, "claimed")
        self.assertNotIn("normal_followups", self.revision.action_receipts)
        late = self.revision.overall_deadline + timedelta(seconds=1)
        self.revision.lease_until = timezone.now() - timedelta(seconds=1)
        self.revision.save(update_fields=["lease_until", "updated_at"])
        with (
            patch("management.services.ig_revision_followups.timezone.now", return_value=late),
            patch("management.services.instagram_bot._provider_http") as send,
            patch("management.services.instagram_bot.get_page_token") as token,
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
        ):
            settled = finalize_sent_revision_effects(self.revision.pk, now=late)
        self.assertTrue(settled.completed, settled.reason)
        send.assert_not_called()
        token.assert_not_called()
        generate.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["normal_followups"]["reason"], "reply_deadline_elapsed")
        self.assertEqual(self.revision.action_receipts["normal_followups"]["outcome"], "not_scheduled")

    def test_sent_history_finalizes_when_bot_disabled_and_client_permission_changed(self):
        from management.services.instagram_bot import process_pending

        self._prepare()
        with patch("management.services.ig_revision_live._project_sent_history", side_effect=RuntimeError("projection failure")):
            failed, _generation, _http = self._execute()
        self.assertEqual(failed.state, "finalization_pending")
        self.revision.refresh_from_db()
        self.revision.lease_until = timezone.now() - timedelta(seconds=1)
        self.revision.save(update_fields=["lease_until", "updated_at"])
        InstagramBotSettings.objects.filter(pk=self.settings.pk).update(is_enabled=False)
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1, manager_takeover=True)
        self.settings.refresh_from_db()
        with (
            patch("management.services.instagram_bot._provider_http") as send,
            patch("management.services.instagram_bot.get_page_token") as token,
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
        ):
            count = process_pending(self.settings)
        self.assertEqual(count, 1)
        send.assert_not_called()
        token.assert_not_called()
        generate.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.state, "processed")
        self.assertEqual(self.revision.action_receipts["normal_followups"]["outcome"], "not_scheduled")
        self.assertEqual(InstagramBotMessage.objects.filter(source="revision_reply").count(), 1)

    def test_partial_debt_reenters_finalization_only_after_exact_receipt_arrives(self):
        from management.services.ig_revision_execution import finalization_due_ids, finalize_sent_revision_effects

        self._prepare()
        self.parsed["reply_text"] = ("Можу допомогти з вибором стилю та кольору. " * 20).strip()
        result, _generation, _http = self._execute([(200, json.dumps({"message_id": "part-one"})), TimeoutError()])
        self.assertEqual(result.state, "delivery_pending")
        self.assertEqual(self.revision.delivery_effects.count(), 2)
        self.revision.refresh_from_db()
        self.assertTrue(self.revision.claim_token.startswith("debt:"))
        self.assertNotIn(self.revision.pk, finalization_due_ids())
        unknown = self.revision.delivery_effects.get(state="unknown")
        # Stand in for independently verified exact-MID reconciliation; this
        # fixture does not infer success from matching text or retry HTTP.
        type(unknown).objects.filter(pk=unknown.pk).update(state="sent", provider_message_id="reconciled-part-two")
        self.assertIn(self.revision.pk, finalization_due_ids())
        with patch("management.services.instagram_bot._provider_http") as send:
            settled = finalize_sent_revision_effects(self.revision.pk)
        self.assertTrue(settled.completed, settled.reason)
        send.assert_not_called()
        self.assertEqual(InstagramBotMessage.objects.filter(source="revision_reply").count(), 2)

    def test_delivered_manager_history_is_labeled_and_unconfirmed_command_is_excluded(self):
        manager = InstagramBotMessage.objects.create(pk=100, client=self.customer, sender_id=self.customer.igsid, role="manager", source="echo", text="Підготували варіант у синьому кольорі.", mid="human-received", status="done")
        InstagramBotMessage.objects.create(pk=101, client=self.customer, sender_id=self.customer.igsid, role="manager", source="manual", text="UNCONFIRMED HUMAN COMMAND", status="pending", send_state="unknown")
        self.source = self._message("Покажіть цей варіант.", "after-human")
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source, window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        history = build_sealed_history(self.revision)
        rendered = json.dumps(history, ensure_ascii=False)
        self.assertIn(manager.text, rendered)
        self.assertIn('\\"actor\\":\\"manager\\"', rendered)
        self.assertNotIn("UNCONFIRMED HUMAN COMMAND", rendered)
        self.assertEqual(history[-1]["role"], "user")

    def test_expired_shadow_is_classified_once_and_does_not_block_fresh_claim(self):
        from management.services.ig_revision_execution import due_revision_ids, expired_revision_debt_ids, record_expired_revision_debt

        past = timezone.now() - timedelta(minutes=3)
        client = IgClient.objects.create(igsid="old-unanswered")
        source = InstagramBotMessage.objects.create(client=client, sender_id=client.igsid, role="user", source="webhook", provider_namespace="instagram_login:owner-1", mid="old-question", text="Старе питання", status="pending", provider_created_at=past)
        turn = IgCustomerTurn.objects.create(client=client, primary_source_message=source, window_started_at=past, window_deadline=past)
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        expired = create_collecting_revision(turn, [source], now=past, bypass_quiet=True).revision
        self.assertNotIn(expired.pk, due_revision_ids())
        self.assertIn(self.revision.pk, due_revision_ids())
        self.assertIn(expired.pk, expired_revision_debt_ids())
        record_expired_revision_debt(expired.pk)
        self.assertNotIn(expired.pk, expired_revision_debt_ids())
        source.refresh_from_db()
        expired.refresh_from_db()
        self.assertEqual(source.status, "pending")
        self.assertEqual(expired.state, "collecting")

    @override_settings(
        IG_REVISION_EXECUTION_ENABLED=True,
        IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
    )
    def test_provider_outage_waits_without_operator_case_then_generates_fresh_child(self):
        from management.models import IgFollowUpTask

        self._prepare()
        self.provider_failure = True
        failed, _generation, http = self._execute()
        self.assertEqual(failed.state, "blocked", failed.reasons)
        http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertIn("generation_admission", self.revision.action_receipts)
        old_deadline = self.revision.overall_deadline
        with patch("management.services.ig_revision_live.timezone.now", return_value=old_deadline + timedelta(seconds=1)):
            self.assertEqual(process_pending_revisions(self.settings, max_items=1), 0)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_state, "waiting")
        self.assertFalse(IgFollowUpTask.objects.filter(reason="revision_case:execution_debt").exists())
        due = self.revision.recovery_due_at
        self.provider_failure = False
        with (
            patch("management.services.ig_revision_live.timezone.now", return_value=due),
            patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate,
            patch("management.services.instagram_bot.get_page_token", return_value="memory-token"),
            patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "recovered-reply"}))) as send,
            patch("management.services.instagram_bot._register_outgoing_message"),
        ):
            handled = process_pending_revisions(self.settings, max_items=1)
        self.assertEqual(handled, 1)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(send.call_count, 1)
        child = IgCustomerTurnRevision.objects.get(parent=self.revision, origin="outage_recovery")
        self.assertEqual(child.state, "processed")
        self.assertEqual(child.bundle_snapshot, self.revision.bundle_snapshot)
        self.assertEqual(GeminiRequest.objects.filter(source_message_id=self.source.pk, lane="live").count(), 2)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.overall_deadline, old_deadline)

    def test_rollback_does_not_spawn_waiting_outage_generation(self):
        from management.services.ig_revision_recovery import schedule_revision_recovery

        self._prepare()
        self.provider_failure = True
        self._execute()
        self.revision.refresh_from_db()
        queued = self.revision.overall_deadline + timedelta(seconds=1)
        schedule_revision_recovery(self.revision.pk, now=queued)
        self.revision.refresh_from_db()
        with (
            patch("management.services.ig_revision_live.timezone.now", return_value=self.revision.recovery_due_at),
            patch("management.services.call_ai_analysis.gemini_generate_text") as generate,
        ):
            process_pending_revisions(self.settings, create_new=False)
        generate.assert_not_called()
        self.assertFalse(IgCustomerTurnRevision.objects.filter(parent=self.revision, origin="outage_recovery").exists())

    @override_settings(
        IG_REVISION_EXECUTION_ENABLED=True,
        IG_REVISION_EXECUTION_CUTOVER_AT="2000-01-01T00:00:00+00:00",
    )
    def test_expired_manual_chain_remains_recovery_candidate_without_reopening_old_turn(self):
        from django.contrib.auth import get_user_model
        from management.models import AdminAuditLog
        from management.services.ig_revision_manual_resume import create_manual_resume_successor
        from management.services.ig_revision_execution import expired_revision_debt_ids

        self.customer.reply_permission_epoch = 1
        self.customer.save(update_fields=["reply_permission_epoch"])
        self.source.status = "done"
        self.source.save(update_fields=["status"])
        self.turn.claim_state = self.turn.ClaimState.PROCESSED
        self.turn.terminal_reason = self.turn.TerminalReason.NO_REPLY_NEEDED
        self.turn.save(update_fields=["claim_state", "terminal_reason"])
        actor = get_user_model().objects.create_user(username="manual-recovery-owner", is_staff=True, is_superuser=True)
        audit = AdminAuditLog.objects.create(actor=actor, actor_role="prompt_editor", action="ig_bot.manual_resume", entity_type="IgClient", entity_id=str(self.customer.pk), before={"permission_epoch": 0, "bot_paused": True, "manager_takeover": False}, after={"permission_epoch": 1, "bot_paused": False, "manager_takeover": False})
        manual = create_manual_resume_successor(self.customer, settings_obj=self.settings, audit_id=audit.pk, source_message_id=self.source.pk)
        self.assertTrue(manual.created, manual.reason)
        self.revision = manual.revision
        self.provider_failure = True
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate:
            process_pending_revisions(self.settings, max_items=1)
        self.assertEqual(generate.call_count, 1)
        self.revision.refresh_from_db()
        expired = self.revision.overall_deadline + timedelta(seconds=1)
        self.assertIn(self.revision.pk, expired_revision_debt_ids(now=expired))
        with patch("management.services.ig_revision_live.timezone.now", return_value=expired):
            process_pending_revisions(self.settings, max_items=1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_state, "waiting")
        self.turn.refresh_from_db()
        self.assertEqual(self.turn.terminal_reason, self.turn.TerminalReason.NO_REPLY_NEEDED)

    def _unavailable_image(self, caption):
        self.source.attachment_media = [{"source_part_id": "mp1_" + "7" * 32, "original_index": 0, "type": "image", "status": "failed", "capture_terminal": True}]
        self.source.save(update_fields=["attachment_media"])
        self._replace_bundle([caption])

    def test_bare_unavailable_media_has_deterministic_clarification_without_model_or_manager(self):
        from management.models import IgFollowUpTask

        self._unavailable_image("")
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertFalse(IgFollowUpTask.objects.filter(kind="manager_task").exists())
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["input_decision"]["origin"], "generate")
        reply = self.revision.action_receipts["media_unavailable_reply"]
        self.assertEqual(reply["media_coverage"]["unavailable"], 1)
        self.assertEqual(self.revision.delivery_effects.get().payload["message"]["text"], reply["reply_text"])
        self.assertFalse(self.revision.generation_proposal_digest)

    def test_meaningful_caption_survives_unavailable_media_and_still_uses_model(self):
        self._unavailable_image("Підкажіть, які є розміри?")
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertNotIn("media_unavailable_reply", self.revision.action_receipts)
        self.assertIn("Підкажіть, які є розміри?", json.dumps(self.generation_calls[0], ensure_ascii=False))
