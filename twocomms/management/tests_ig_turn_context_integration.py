"""Offline tests of the actual captured-context request consumers.

Fixtures use real immutable publications, sealed sources and source reductions.
Only the provider transport is mocked; no HTTP or external credentials are used.
"""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import os

from django.test import TestCase, override_settings
from django.utils import timezone

from management.models import (
    BotInstruction, BotPolicyPublication, IgClient, IgCustomerTurn,
    IgTurnMessage, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_policy_publication import (
    load_active_policy_snapshot, snapshot_from_rows, snapshot_hash,
)
from management.services.ig_revision_live import (
    RevisionGenerationBoundary, _generate_proposal,
)
from management.services.ig_revision_media import collect_revision_media
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_integration import prepare_revision_turn_context
from management.services.ig_turn_revisions import (
    claim_revision_preparation, claim_sealed_revision, create_collecting_revision,
    seal_revision,
)


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified",
                   IG_CLIENT_STATE_PROMPT_TOKENS=2400)
class TurnContextConsumerIntegrationTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.ordinal = 0
        self.instruction = BotInstruction.objects.create(title="Captured publication",
            body="Captured public playbook A: answer with verified facts.", intent_tags="global",
            is_active=True, priority=1, locale="all")
        self.publication = self.publish(version=1)
        self.settings_row = InstagramBotSettings.objects.create(pk=1, is_enabled=True,
            ai_enabled=True, ig_user_id="owner-1", active_instruction_publication=self.publication)
        self.publication_binding = PublicationBinding(self.publication.pk, self.publication.version,
            self.publication.snapshot_hash)
        self.publication_snapshot = load_active_policy_snapshot(self.settings_row)
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        # Timing's ownership/receipt semantics are tested by their own suites;
        # retain one stable nonempty timing component here.
        timing = patch("management.services.ig_revision_conversation_context.conversation_timing_guidance",
            return_value="[CURRENT REPLY TIMING CONTEXT]\nNo unsupported delay apology.")
        timing.start()
        self.addCleanup(timing.stop)

    def publish(self, *, version):
        snapshot = snapshot_from_rows(BotInstruction.objects.all().order_by("priority", "pk"))
        return BotPolicyPublication.objects.create(version=version, kind="publish", schema_version=1,
            snapshot=snapshot, snapshot_hash=snapshot_hash(snapshot), compiler_version="instruction-set-v1",
            instruction_count=len(snapshot["instructions"]))

    def fixture(self, text="Please reply in English. Size L.", *, reduction=True):
        self.ordinal += 1
        customer = IgClient.objects.create(igsid=f"context-consumer-{self.ordinal}", language="uk")
        past = InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook", status="done",
            mid=f"context-past-{self.ordinal}", text="Historical preference before this captured request.",
            provider_created_at=self.now - timedelta(minutes=1))
        source = InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook", status="pending",
            mid=f"context-current-{self.ordinal}", text=text, provider_created_at=self.now)
        if reduction:
            from management.services.ig_revision_commerce import reduce_inbound_commerce_source
            reduced = reduce_inbound_commerce_source(customer, source,
                expected_provider_namespace="instagram_login:owner-1")
            self.assertTrue(reduced.ready, reduced.reason)
            customer.refresh_from_db()
        turn = IgCustomerTurn.objects.create(client=customer, primary_source_message=source,
            window_started_at=self.now, window_deadline=self.now)
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [source], now=self.now, bypass_quiet=True).revision
        preparing = claim_revision_preparation(revision.pk, now=self.now)
        sealed = seal_revision(revision.pk, preparing.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(revision.pk, now=self.now)
        self.assertTrue(claimed.token, claimed.reason)
        revision = claimed.revision
        revision.client = customer
        collection = collect_revision_media(revision.pk, claimed.token)
        boundary = RevisionGenerationBoundary(revision, claimed.token, self.settings_row,
            self.publication_binding, has_images=False)
        prepared = prepare_revision_turn_context(revision, generation_boundary=boundary,
            collection=collection, settings_row=self.settings_row, publication=self.publication_binding,
            now=self.now)
        return SimpleNamespace(customer=customer, past=past, source=source, turn=turn,
            revision=revision, token=claimed.token, collection=collection, boundary=boundary, prepared=prepared)

    def transport(self, payload, **kwargs):
        from management.services.call_ai_analysis import CallAIAnalysisError
        self.requests.append((deepcopy(payload), deepcopy(kwargs["request_policy_manifest"]), kwargs))
        raise CallAIAnalysisError("offline fixture transport unavailable")

    def consume(self, fixture, *, request_metadata=None, turn_note=None):
        from management.services import instagram_bot as bot
        prepared = fixture.prepared
        failure = {}
        result = bot.gemini_generate(self.settings_row, list(prepared.history), client=fixture.customer,
            routing_decision=prepared.context.decision, generation_boundary=fixture.boundary,
            turn_note=prepared.context.turn_note if turn_note is None else turn_note,
            memory_note=prepared.context.memory_note,
            context_note=prepared.context.context_note, captured_client_state=prepared.state,
            captured_dynamic_notes=prepared.dynamic_notes, captured_turn_text=prepared.current_text,
            captured_policy_tags=prepared.policy_inputs.tags,
            captured_knowledge_language=prepared.policy_inputs.knowledge_language,
            request_context_metadata=request_metadata or prepared.request_metadata, failure_context=failure,
            turn_media_binding=fixture.collection.binding, turn_media_context=[],
            deadline_at=fixture.revision.overall_deadline - timedelta(seconds=5))
        return result, failure

    def test_actual_request_reuses_captured_text_history_state_readiness_language_and_tags(self):
        from management.services import instagram_bot as bot
        from management.services.bot_knowledge import read_knowledge_manifest
        from management.services.bot_playbooks import active_instruction_selection
        fixture = self.fixture()
        captured_state = fixture.prepared.state.as_dict()
        captured_readiness = fixture.prepared.dynamic_notes["checkout_readiness"]
        self.assertEqual(captured_state["source_selection"]["values"]["size"], "L")
        self.assertEqual(fixture.prepared.policy_inputs.knowledge_language, "en")
        fixture.customer.language, fixture.customer.current_size = "ru", "XL"
        fixture.customer.primary_objection = "price"
        fixture.customer.save(update_fields=["language", "current_size", "primary_objection"])
        InstagramBotMessage.objects.filter(pk=fixture.past.pk).update(text="New history outside capture")
        InstagramBotMessage.objects.filter(pk=fixture.source.pk).update(text="New current text outside capture")
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider, patch(
                "management.services.bot_knowledge.read_knowledge_manifest", wraps=read_knowledge_manifest) as knowledge, patch(
                "management.services.bot_playbooks.active_instruction_selection", wraps=active_instruction_selection) as playbooks, patch.object(
                bot, "client_state_note", side_effect=AssertionError("live state loader used")) as state_loader, patch.object(
                bot, "_checkout_readiness_note", side_effect=AssertionError("live readiness loader used")) as readiness_loader:
            self.consume(fixture)
        self.assertEqual(provider.call_count, 1)
        state_loader.assert_not_called()
        readiness_loader.assert_not_called()
        self.assertEqual(knowledge.call_args.args[0], "en")
        self.assertEqual(playbooks.call_args.kwargs["captured_tags"], fixture.prepared.policy_inputs.tags)
        self.assertEqual(playbooks.call_args.kwargs["turn_text"], fixture.prepared.current_text)
        payload, manifest, kwargs = self.requests[0]
        text = str(payload)
        self.assertIn("Historical preference before this captured request.", text)
        self.assertIn("Please reply in English. Size L.", text)
        self.assertNotIn("New history outside capture", text)
        self.assertNotIn("New current text outside capture", text)
        self.assertIn(captured_readiness, payload["system_instruction"]["parts"][0]["text"])
        self.assertEqual(fixture.prepared.state.as_dict(), captured_state)
        self.assertEqual(manifest["request_context"]["history_message_ids"], [fixture.past.pk])
        self.assertEqual(manifest["request_context"]["source_message_ids"], [fixture.source.pk])
        self.assertEqual(kwargs["max_actual_dispatches"], 8)

    def test_explicit_captured_publication_keeps_playbook_when_current_head_moves(self):
        from management.services.instagram_bot import assemble_system_instruction
        fixture = self.fixture()
        prepared = fixture.prepared
        before_metadata = {}
        before = assemble_system_instruction(self.settings_row, client=fixture.customer,
            captured_client_state=prepared.state, captured_dynamic_notes=prepared.dynamic_notes,
            captured_policy_tags=prepared.policy_inputs.tags,
            captured_knowledge_language=prepared.policy_inputs.knowledge_language,
            turn_text=prepared.current_text, turn_note=prepared.context.turn_note,
            instruction_publication=self.publication_snapshot, compiled_metadata=before_metadata)
        self.instruction.body = "New public playbook B outside the captured publication."
        self.instruction.save(update_fields=["body"])
        new_head = self.publish(version=2)
        InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(active_instruction_publication=new_head)
        self.settings_row.refresh_from_db()
        after_metadata = {}
        after = assemble_system_instruction(self.settings_row, client=fixture.customer,
            captured_client_state=prepared.state, captured_dynamic_notes=prepared.dynamic_notes,
            captured_policy_tags=prepared.policy_inputs.tags,
            captured_knowledge_language=prepared.policy_inputs.knowledge_language,
            turn_text=prepared.current_text, turn_note=prepared.context.turn_note,
            instruction_publication=self.publication_snapshot, compiled_metadata=after_metadata)
        self.assertEqual(before, after)
        self.assertEqual(before_metadata, after_metadata)
        self.assertIn("Captured public playbook A", after)
        self.assertNotIn("New public playbook B", after)

    def test_live_consumer_rejects_stale_publication_before_provider(self):
        fixture = self.fixture()
        self.instruction.body = "New public playbook B outside the captured publication."
        self.instruction.save(update_fields=["body"])
        new_head = self.publish(version=2)
        InstagramBotSettings.objects.filter(pk=self.settings_row.pk).update(active_instruction_publication=new_head)
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            result, failure = self.consume(fixture)
        self.assertIsNone(result)
        self.assertEqual(provider.call_count, 0, "Captured request must not spend provider quota on a different publication.")
        self.assertIn(failure.get("kind"), {"invalid_payload", "revision_not_ready", "publication_changed"})
        self.assertEqual(failure.get("policy_readiness"), "publication_changed")

    def test_invalid_logical_context_manifest_stops_before_provider_with_finite_reason(self):
        fixture = self.fixture()
        metadata = {**deepcopy(fixture.prepared.request_metadata), "raw_customer_text": "private unsupported field"}
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            response, failure = self.consume(fixture, request_metadata=metadata)
        self.assertIsNone(response)
        provider.assert_not_called()
        self.assertEqual(failure.get("kind"), "invalid_payload")
        self.assertEqual(failure.get("policy_readiness"), "policy_manifest_context_invalid")
        self.assertNotIn("private unsupported field", str(failure))

    def test_actual_signal_source_refs_use_state_card_id_contract(self):
        from management.models import IgConversationSignal
        fixture = self.fixture()
        signal = IgConversationSignal.objects.create(client=fixture.customer, message=fixture.source,
            signal_type="size_concern", value="L")
        IgConversationSignal.objects.filter(pk=signal.pk).update(created_at=self.now)
        prepared = prepare_revision_turn_context(fixture.revision, generation_boundary=fixture.boundary,
            collection=fixture.collection, settings_row=self.settings_row, publication=self.publication_binding,
            now=self.now + timedelta(seconds=5))
        slot = prepared.state.as_dict()["slots"]["conversation_signals"]
        self.assertEqual(slot["status"], "confirmed")
        self.assertEqual(slot["omission_reason"], "")
        self.assertEqual(slot["source_refs"], [{"kind": "message", "id": fixture.source.pk}])

    def test_customer_body_changes_context_hmac_without_changing_public_policy_identity(self):
        first = self.fixture("Please reply in English. Hello!", reduction=False)
        second = self.fixture("Please reply in English. Thank you!", reduction=False)
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            self.consume(first)
            self.consume(second)
        self.assertEqual(provider.call_count, 2)
        first_payload, first_manifest, _ = self.requests[0]
        second_payload, second_manifest, _ = self.requests[1]
        self.assertNotEqual(first_payload["contents"], second_payload["contents"])
        for key in ("content_hash", "core", "knowledge_hash", "instruction_publication", "instruction_selection"):
            self.assertEqual(first_manifest[key], second_manifest[key], key)
        self.assertNotEqual(first_manifest["request_context"]["context_digest"], second_manifest["request_context"]["context_digest"])
        self.assertNotEqual(first_manifest["request_context"]["request_digest"], second_manifest["request_context"]["request_digest"])
        self.assertNotIn("Hello!", str(first_manifest))

    def test_modes_report_actual_request_context_without_extra_provider_calls(self):
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            for mode in ("legacy", "shadow", "unified"):
                with self.subTest(mode=mode), override_settings(IG_TURN_CONTEXT_MODE=mode):
                    fixture = self.fixture("Please reply in English. Hello!", reduction=False)
                    count = provider.call_count
                    proposal, _, reasons = _generate_proposal(fixture.revision, fixture.token,
                        self.settings_row, self.publication_binding, fixture.collection)
                    self.assertIsNone(proposal)
                    self.assertEqual(provider.call_count, count + 1, reasons)
                    payload, manifest, kwargs = self.requests[-1]
                    context = manifest["request_context"]
                    self.assertEqual(context["effective_mode"], mode)
                    self.assertEqual(context["source_message_ids"], [fixture.source.pk])
                    self.assertEqual(context["bundle_digest"], fixture.revision.snapshot_digest)
                    self.assertIn("CURRENT CUSTOMER BUNDLE", str(payload["contents"]))
                    self.assertEqual(kwargs["max_actual_dispatches"], 8)
                    if mode == "unified":
                        self.assertEqual(context["builder_version"], "ig-turn-intelligence.v1")
                        self.assertEqual(context["history_message_ids"], [fixture.past.pk])
                        self.assertEqual(context["view_versions"]["state_view_version"], "captured-client-state.v1")
                    else:
                        self.assertEqual(context["builder_version"], "legacy_revision.v1")
                        self.assertEqual(context["history_message_ids"], [])
        self.assertEqual(provider.call_count, 3)

    @override_settings(IG_CLIENT_STATE_PROMPT_TOKENS=1)
    def test_mandatory_state_overflow_stops_before_transport_with_finite_reason(self):
        fixture = self.fixture()
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            response, failure = self.consume(fixture)
        self.assertIsNone(response)
        provider.assert_not_called()
        self.assertEqual(failure["kind"], "invalid_payload")
        self.assertEqual(failure["policy_readiness"], "captured_state_exceeds_budget")

    @override_settings(IG_BOT_POLICY_BUDGET_CHARS=48000)
    def test_response_plan_survives_real_48k_budget_when_large_public_playbook_is_omitted(self):
        self.instruction.body = "Optional legitimate public guidance. " * 1250
        self.instruction.save(update_fields=["body"])
        publication = self.publish(version=2)
        self.settings_row.active_instruction_publication = publication
        self.settings_row.save(update_fields=["active_instruction_publication"])
        self.publication_binding = PublicationBinding(publication.pk, publication.version, publication.snapshot_hash)
        fixture = self.fixture()
        instruction_id = publication.snapshot["instructions"][0]["id"]
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            self.consume(fixture)
        self.assertEqual(provider.call_count, 1)
        payload, manifest, _ = self.requests[0]
        prompt = payload["system_instruction"]["parts"][0]["text"]
        self.assertEqual(manifest["budget_chars"], 48000)
        self.assertIn("context:turn", manifest["mandatory_ids"])
        self.assertIn("context:turn", manifest["selected_ids"])
        self.assertIn(fixture.boundary.response_plan.prompt_guidance(), prompt)
        self.assertEqual(prompt.count("[SERVER RESPONSE PLAN]"), 1)
        self.assertEqual(fixture.prepared.state.as_dict()["source_selection"]["values"]["size"], "L")
        self.assertNotIn("Optional legitimate public guidance.", prompt)
        self.assertNotIn(instruction_id, manifest["selected_ids"])
        self.assertIn({"id": instruction_id, "reason": "budget_exhausted"}, manifest["omitted"])
        # It was eligible at publication selection; only total compilation
        # budget omitted it after reserving every mandatory turn requirement.
        self.assertIn(instruction_id, manifest["instruction_selection"]["selected_ids"])

    @override_settings(IG_BOT_POLICY_BUDGET_CHARS=48000)
    def test_turn_requirements_over_real_48k_budget_stop_before_provider(self):
        fixture = self.fixture()
        note = (fixture.prepared.context.turn_note + "\n[VERIFIED CURRENT SOURCE COVERAGE]\n"
                + "Customer source requirement. " * 2000)
        self.assertGreater(len(note), 48000)
        self.requests = []
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self.transport) as provider:
            result, failure = self.consume(fixture, turn_note=note)
        self.assertIsNone(result)
        provider.assert_not_called()
        self.assertEqual(failure["kind"], "invalid_payload")
        self.assertEqual(failure["policy_readiness"], "mandatory_policy_exceeds_budget")
        self.assertEqual(self.requests, [])
