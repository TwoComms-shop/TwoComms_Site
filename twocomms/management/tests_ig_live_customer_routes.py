import json
from unittest.mock import patch

from django.db import DatabaseError, connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import IgClient, IgConversationRouteDecision, InstagramBotMessage
from management.services.ig_response_control import parse_structured_response, structured_response_schema
from management.services.ig_revision_live import execute_claimed_revision, _restore_response


def route_payload(message_id):
    return {"schema_version": "customer-route.v1", "intents": [{
        "kind": "employment", "subtype": "none", "operation": "open",
        "evidence_message_ids": [message_id], "confidence": 0.9,
    }], "focus_index": 0}


class OptionalCustomerRouteParserTests(SimpleTestCase):
    def test_valid_route_is_typed_and_optional_in_schema(self):
        response = parse_structured_response({"reply_text": "Дякую за запит.",
            "controls": [], "customer_routes": route_payload(17)})
        self.assertTrue(response.valid)
        self.assertEqual(response.customer_routes.intents[0].evidence_message_ids, (17,))
        self.assertEqual(response.route_abstention_reason, "")
        schema = structured_response_schema()
        self.assertIn("customer_routes", schema["properties"])
        self.assertNotIn("customer_routes", schema["required"])

    def test_malformed_or_missing_route_abstains_without_invalidating_reply(self):
        bad_bool = route_payload(17)
        bad_bool["intents"][0]["confidence"] = True
        for payload in (None, {}, [], bad_bool, {"schema_version": "wrong", "intents": []}):
            with self.subTest(payload=payload):
                response = parse_structured_response({"reply_text": "Дякую.",
                    "controls": [], "customer_routes": payload})
                self.assertTrue(response.valid, response.error)
                self.assertIsNone(response.customer_routes)
                self.assertTrue(response.route_abstention_reason)
        missing = parse_structured_response({"reply_text": "Дякую.", "controls": []})
        self.assertTrue(missing.valid)
        self.assertEqual(missing.route_abstention_reason, "route_missing")

    def test_routes_cannot_relax_control_or_media_schema(self):
        for optional in ({"controls": [{"kind": "paid", "value": True}]},
            {"controls": [], "turn_intelligence": {}},
            {"controls": [{"kind": "manager", "value": False}]}):
            response = parse_structured_response({"reply_text": "Дякую.",
                "customer_routes": route_payload(17), **optional})
            self.assertFalse(response.valid)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionCustomerRouteIntegrationTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute

    def _with_route(self):
        self._prepare()
        self.parsed["customer_routes"] = route_payload(self.source.pk)

    def test_single_winning_request_persists_exact_source_binding_before_send(self):
        self._with_route()
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        proposal = self.revision.generation_proposal
        binding = proposal["route_binding"]
        self.assertEqual(binding["input_digest"], self.revision.snapshot_digest)
        self.assertEqual(binding["watermark_message_id"], self.source.pk)
        self.assertEqual(binding["client_permission_epoch"], self.revision.permission_epoch)
        self.assertIsNone(proposal["route_expected_previous_decision_id"])
        decision = IgConversationRouteDecision.objects.get(revision=self.revision)
        self.assertEqual(decision.active_intents[0]["key"], "employment:none")
        self.assertEqual(decision.source_binding["source_digest"], self.revision.generation_proposal_digest)
        prompt = self.generation_calls[0]["system_instruction"]["parts"][0]["text"]
        context_text = prompt.split("[CURRENT CUSTOMER ROUTE EVIDENCE]\n", 1)[1]
        context = json.JSONDecoder().raw_decode(context_text[context_text.index("{"):])[0]
        self.assertEqual(context["sources"], [{"message_id": self.source.pk, "text": self.source.text}])
        self.assertNotIn("route_binding", context)
        self.assertNotIn(self.token, context_text)
        self.assertEqual(set(context), {"sources", "active_intent_keys", "focus_key"})
        self.assertEqual(_restore_response(proposal).customer_routes.to_dict(), proposal["customer_routes"])

    def test_stored_proposal_retry_accepts_once_without_regeneration(self):
        self._with_route()
        with patch("management.services.instagram_bot.get_page_token", return_value=""):
            with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate:
                first = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(first.state, "blocked")
        self.assertIn("provider_not_configured", first.reasons)
        self.assertEqual(generate.call_count, 1)
        original = IgConversationRouteDecision.objects.get(revision=self.revision)
        self.revision.refresh_from_db()
        original_digest = self.revision.generation_proposal_digest
        second, generate, http = self._execute()
        self.assertEqual(second.state, "completed", second.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(list(IgConversationRouteDecision.objects.values_list("pk", flat=True)), [original.pk])
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal_digest, original_digest)

    def test_malformed_route_does_not_trigger_repair_or_block_reply(self):
        self._with_route()
        self.parsed["customer_routes"]["intents"][0]["confidence"] = False
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertFalse(IgConversationRouteDecision.objects.exists())
        self.revision.refresh_from_db()
        self.assertTrue(self.revision.generation_proposal["route_abstention_reason"])
        self.assertNotIn("customer_routes", self.revision.generation_proposal)

    def test_guessed_source_id_is_not_accepted(self):
        self._with_route()
        self.parsed["customer_routes"] = route_payload(self.source.pk + 100000)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertFalse(IgConversationRouteDecision.objects.exists())
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["route_abstention_reason"], "evidence_outside_source")

    def test_permission_change_during_http_cannot_be_recaptured_as_new_authority(self):
        self._with_route()
        self.before_validate = lambda: IgClient.objects.filter(pk=self.customer.pk).update(
            reply_permission_epoch=self.customer.reply_permission_epoch + 1)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked")
        self.assertEqual(generate.call_count, 1)
        http.assert_not_called()
        self.assertFalse(IgConversationRouteDecision.objects.exists())
        self.revision.refresh_from_db()
        self.assertFalse(self.revision.generation_proposal_digest)

    def test_changed_input_after_capture_abstains_route(self):
        self._with_route()
        self.after_validate = lambda: InstagramBotMessage.objects.filter(pk=self.source.pk).update(
            text="Змінений текст після запиту")
        result, generate, http = self._execute()
        self.assertEqual(generate.call_count, 1)
        self.assertFalse(IgConversationRouteDecision.objects.exists())
        self.revision.refresh_from_db()
        if self.revision.generation_proposal_digest:
            self.assertEqual(self.revision.generation_proposal["route_abstention_reason"], "route_source_changed")
        else:
            self.assertEqual(result.state, "blocked")

    def test_missing_route_leaves_previous_context_without_new_journal_entry(self):
        self._prepare()
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertFalse(IgConversationRouteDecision.objects.exists())

    def _assert_optional_fault_preserves_main_reply(self, target, side_effect):
        self._with_route()
        with patch(target, side_effect=side_effect):
            # Stop immediately before transport to exercise the stored-proposal
            # continuation too, while the exact provider winner already exists.
            with (
                patch("management.services.instagram_bot.get_page_token", return_value=""),
                patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate) as generate,
            ):
                first = execute_claimed_revision(self.revision.pk, self.token, self.settings)
            self.assertEqual(first.state, "blocked", first.reasons)
            self.assertIn("provider_not_configured", first.reasons)
            self.assertEqual(generate.call_count, 1)
            self.revision.refresh_from_db()
            digest = self.revision.generation_proposal_digest
            self.assertTrue(digest)
            self.assertEqual(self.revision.generation_proposal["response"]["reply_text"], self.parsed["reply_text"])
            self.assertNotIn("private-route-fault", json.dumps(self.revision.generation_proposal))
            second, generate, http = self._execute()
        self.assertEqual(second.state, "completed", second.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal_digest, digest)
        self.assertEqual(self.revision.delivery_effects.get().state, "sent")
        self.assertFalse(IgConversationRouteDecision.objects.exists())

    def test_capture_exception_preserves_provider_reply_and_stored_retry(self):
        with self.assertLogs("management.services.ig_revision_live", level="WARNING") as logs:
            self._assert_optional_fault_preserves_main_reply(
                "management.services.ig_revision_proposal.capture_revision_customer_routes",
                DatabaseError("private-route-fault"))
        self.assertIn("revision_route_capture_unavailable", " ".join(logs.output))
        self.assertNotIn("private-route-fault", " ".join(logs.output))

    def test_projection_db_error_rolls_back_savepoint_preserving_won_proposal(self):
        def broken_projection(*args, **kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertTrue(connection.savepoint_ids)
            # A real cursor error, not merely a raised mock exception: the
            # canonical proposal write must remain usable after the rollback.
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM private_route_fault_missing_table")

        self._assert_optional_fault_preserves_main_reply(
            "management.services.ig_revision_proposal._customer_route_projection", broken_projection)
        self.assertEqual(self.revision.generation_proposal["route_abstention_reason"],
            "route_projection_unavailable")

    def test_acceptance_exception_preserves_send_and_does_not_regenerate(self):
        with self.assertLogs("management.services.ig_revision_live", level="WARNING") as logs:
            self._assert_optional_fault_preserves_main_reply(
                "management.services.ig_conversation_routes.accept_customer_routes",
                DatabaseError("private-route-fault"))
        self.assertIn("revision_route_acceptance_unavailable", " ".join(logs.output))
        self.assertNotIn("private-route-fault", " ".join(logs.output))
