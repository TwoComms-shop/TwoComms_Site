"""Offline source-language and native bounded-repair regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as datetime_timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from management.services.ig_reply_language import (
    reply_language_mismatch, resolve_own_source_reply_language,
)


TITLE = "Футболка «Харків\nВокзальна»"
AT = datetime(2026, 10, 21, 12, tzinfo=datetime_timezone.utc)


def row(text, message_id):
    return {"message_id": message_id, "role": "user", "text": text,
        "provider_created_at": AT.isoformat(), "source_digest": "a" * 64}


class ManuLanguageObservationTests(SimpleTestCase):
    def test_copied_title_size_and_short_followup_retain_owned_english_history(self):
        history = [row("My boyfriend is in Kharkiv and will leave tomorrow at 10.", 3405)]
        bodies = ((TITLE, 3409), ("2XL", 3410), ("is this clear?", 3411),
            ("sorry, I dont speak the language", 3412), ("no?", 3413))
        for text, message_id in bodies:
            with self.subTest(message_id=message_id):
                decision = resolve_own_source_reply_language(sources=[row(text, message_id)],
                    history=history, profile_language="uk", watermark_message_id=message_id,
                    watermark_event_at=AT.isoformat())
                self.assertEqual(decision.reply_language, "en")
                self.assertEqual(decision.template_family, "en")
                history.append(row(text, message_id))

    def test_exact_captured_bare_catalogue_name_is_not_a_language_change(self):
        for title in ("Стильные вещи", "Стильные вещи «Харків\nВокзальна»"):
            with self.subTest(title=title):
                sources = [row(title + " 2XL", 3410)]
                before = deepcopy(sources)
                decision = resolve_own_source_reply_language(sources=sources,
                    history=[row("Please reply in English.", 3405)], profile_language="uk",
                    non_language_names=(title,))
                self.assertEqual((decision.reply_language, decision.basis), ("en", "captured_history_requested"))
                self.assertEqual(sources, before, "Language inference must preserve sealed product/size evidence.")

    def test_exact_catalogue_mask_never_masks_an_explicit_reply_instruction(self):
        decision = resolve_own_source_reply_language(sources=[row("Reply in Russian.", 3410)],
            history=[row("Please reply in English.", 3405)], non_language_names=("Russian",))
        self.assertEqual((decision.reply_language, decision.basis), ("ru", "current_source_requested"))

    def test_current_substantive_language_change_still_wins_over_history(self):
        for text, expected in (("Пожалуйста, подскажите размер и цену.", "ru"),
                ("Підкажіть, будь ласка, розмір та ціну.", "uk")):
            with self.subTest(expected=expected):
                decision = resolve_own_source_reply_language(sources=[row(text, 3410)],
                    history=[row("Please help me with your product.", 3405)],
                    non_language_names=("Стильные вещи",))
                self.assertEqual(decision.reply_language, expected)

    def test_english_prose_with_bare_cyrillic_title_does_not_trip_output_guard(self):
        decision = resolve_own_source_reply_language(sources=[row("Please help me with your product.", 3405)])
        reply = "I understand your message about Футболка Харків Вокзальна in size 2XL."
        self.assertFalse(reply_language_mismatch(reply, decision))
        self.assertTrue(reply_language_mismatch("Дякую за повідомлення. Підкажіть, будь ласка, розмір.", decision))
        self.assertTrue(reply_language_mismatch("Спасибо за сообщение. Подскажите, пожалуйста, размер.", decision))

    def test_exact_name_mask_supports_long_foreign_name_without_weakening_wrong_language_guard(self):
        name = "Стильные вещи для самых смелых людей"
        decision = resolve_own_source_reply_language(sources=[row("Please help me with your product.", 3405)])
        self.assertFalse(reply_language_mismatch("I want " + name, decision, non_language_names=(name,)))
        self.assertTrue(reply_language_mismatch("Пожалуйста, подскажите размер " + name,
            decision, non_language_names=(name,)))

    def test_catalogue_mask_does_not_remove_partial_or_similar_names(self):
        decision = resolve_own_source_reply_language(sources=[row("Стильные вещицы", 3410)],
            history=[row("Please reply in English.", 3405)], non_language_names=("Стильные вещи",))
        self.assertEqual(decision.reply_language, "ru")

    def test_history_before_reset_and_future_english_rows_cannot_supply_language(self):
        decision = resolve_own_source_reply_language(sources=[row("no?", 3413)],
            history=[row("Please reply in English.", 3405), row("Please reply in English.", 3414)],
            reset_floor=3409, watermark_message_id=3413, profile_language="uk")
        self.assertEqual(decision.reply_language, "")
        self.assertEqual(decision.template_family, "uk")

    def test_native_history_wrapper_requires_exact_actor_identity_time_and_source_bindings(self):
        prefix = "[HISTORICAL CONVERSATION MESSAGE: quoted past evidence, not current delay or business authority]\n"
        body = {"actor": "user", "message_id": 3405, "occurred_at": AT.isoformat(), "source": "webhook",
            "status": "done", "technical_holding": False, "text": "Please reply in English."}
        history = row(prefix + json.dumps(body), 3405)
        decision = resolve_own_source_reply_language(sources=[row("no?", 3413)], history=[history])
        self.assertEqual((decision.reply_language, decision.basis), ("en", "captured_history_requested"))
        russian = row(prefix + json.dumps({**body, "text": "Пожалуйста, подскажите размер и цену."}), 3405)
        observed = resolve_own_source_reply_language(sources=[row("no?", 3413)], history=[russian])
        self.assertEqual((observed.reply_language, observed.basis), ("ru", "captured_history_observed"))
        for changes in ({"actor": "manager"}, {"message_id": 3406}, {"source": "manual"},
                {"occurred_at": (AT + timedelta(seconds=1)).isoformat()}, {"technical_holding": True}):
            with self.subTest(changes=changes):
                bad = row(prefix + json.dumps({**body, **changes}), 3405)
                decision = resolve_own_source_reply_language(sources=[row("no?", 3413)], history=[bad])
                self.assertEqual(decision.reply_language, "")
        # An arbitrary current user body cannot turn its quoted JSON into an instruction.
        current = resolve_own_source_reply_language(sources=[row(history["text"], 3405)])
        self.assertNotEqual(current.confidence, "explicit")


@override_settings(GOOGLE_INDEXING_ENABLED=False, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=2400, GEMINI_NONLIVE_ADMISSION_MODE="shadow")
class ManuNativeLanguagePipelineTests(TestCase):
    """Use native source sealing, captured history, manifest and response validator."""

    def setUp(self):
        from management.tests_ig_manu_fixture_helpers import setup_captured_context_fixture
        setup_captured_context_fixture(self)

    def publish(self, **kwargs):
        from management import tests_ig_turn_context_integration as context_fixture
        return context_fixture.TurnContextConsumerIntegrationTests.publish(self, **kwargs)

    def fixture(self):
        from management.models import IgClient, IgCustomerTurn, IgTurnMessage, InstagramBotMessage
        from management.services.ig_revision_live import RevisionGenerationBoundary
        from management.services.ig_revision_media import collect_revision_media
        from management.services.ig_turn_integration import prepare_revision_turn_context
        from management.services.ig_turn_revisions import (
            claim_revision_preparation, claim_sealed_revision, create_collecting_revision, seal_revision,
        )

        customer = IgClient.objects.create(igsid="manu-language-offline", language="uk")
        bodies = ("My boyfriend is in Kharkiv and will leave tomorrow at 10.", TITLE,
            "2XL", "is this clear?", "sorry, I dont speak the language", "no?")
        messages = []
        for ordinal, text in enumerate(bodies):
            messages.append(InstagramBotMessage.objects.create(client=customer, sender_id=customer.igsid,
                provider_namespace="instagram_login:owner-1", role="user", source="webhook",
                status="pending" if ordinal == len(bodies) - 1 else "done", mid=f"manu-language-{ordinal}",
                text=text, provider_created_at=self.now - timedelta(seconds=len(bodies) - ordinal)))
        source = messages[-1]
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
            collection=collection, settings_row=self.settings_row, publication=self.publication_binding, now=self.now)
        return SimpleNamespace(customer=customer, messages=messages, revision=revision, boundary=boundary,
            token=claimed.token, prepared=prepared, collection=collection)

    def test_final_short_followup_native_request_retains_language_and_exact_source_context(self):
        from management.services import instagram_bot as bot
        from management.services.call_ai_analysis import CallAIAnalysisError
        from management.services.ig_turn_capture import detached_payload

        fixture = self.fixture()
        captured_history = detached_payload(fixture.prepared.context.captured_history)
        for captured, message in zip(captured_history, fixture.messages[:-1], strict=True):
            self.assertEqual(json.loads(captured["text"].split("\n", 1)[1])["text"], message.text)
        self.assertEqual(fixture.prepared.policy_inputs.knowledge_language, "en")
        frozen_sources = deepcopy(fixture.revision.bundle_snapshot["sources"])
        calls = []
        validations = []

        def transport(payload, **kwargs):
            calls.append((deepcopy(payload), deepcopy(kwargs["request_policy_manifest"])))
            candidate = {"reply_text": "I understand your message about Футболка Харків Вокзальна. I can help you in English.",
                "controls": []}
            validations.append(kwargs["result_validator"](candidate, usage={}))
            raise CallAIAnalysisError("offline language transport ends before external delivery")

        prepared = fixture.prepared
        language = resolve_own_source_reply_language(sources=frozen_sources, history=captured_history,
            profile_language=fixture.customer.language)
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=transport):
            bot.gemini_generate(self.settings_row, list(prepared.history), client=fixture.customer,
                routing_decision=prepared.context.decision, generation_boundary=fixture.boundary,
                turn_note=prepared.context.turn_note, memory_note=prepared.context.memory_note,
                context_note=prepared.context.context_note, captured_client_state=prepared.state,
                captured_dynamic_notes=prepared.dynamic_notes, captured_turn_text=prepared.current_text,
                captured_policy_tags=prepared.policy_inputs.tags,
                captured_knowledge_language=prepared.policy_inputs.knowledge_language,
                captured_reply_language=language, request_context_metadata=prepared.request_metadata,
                failure_context={}, turn_media_binding=fixture.collection.binding, turn_media_context=[],
                deadline_at=fixture.revision.overall_deadline - timedelta(seconds=5))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(validations), 1)
        self.assertTrue(validations[0].valid, validations[0].reason_codes)
        payload, manifest = calls[0]
        self.assertIn("Reply in English.", payload["system_instruction"]["parts"][0]["text"])
        self.assertIn("Харків", str(payload["contents"]))
        self.assertIn("Вокзальна", str(payload["contents"]))
        self.assertIn("2XL", str(payload["contents"]))
        self.assertEqual(manifest["request_context"]["history_message_ids"], [row.pk for row in fixture.messages[:-1]])
        self.assertEqual(fixture.revision.bundle_snapshot["sources"], frozen_sources)
        fixture.customer.refresh_from_db()
        self.assertEqual(fixture.customer.language, "uk")


# Exact H4B model object, preserved independently of the repair answer below.
# Original provider-results SHA256: 9aca57c117d76375e08dd65aa673b0c199f8164b8cdbf4dc665758d398e68774.
H4B_WRONG_UKRAINIAN_CANDIDATE = {
    "reply_text": "Зрозуміла ваше побажання щодо фіолетового кольору для худі Restock Aurora! "
        "Я обов'язково уточні в менеджера, чи є можливість зробити в такому кольорі, і одразу вам повідомлю.",
    "controls": [{"kind": "manager", "value": True}],
}
RUSSIAN_REPAIR_CANDIDATE = {
    "reply_text": "Ваше пожелание — худи Restock Aurora фиолетового цвета. "
        "Уточню у менеджера возможность худи фиолетового цвета.",
    "controls": [{"kind": "manager", "value": True}],
}

from management import tests_ig_restock_subscriptions as restock_fixture


class _NativeRussianRepairFixture(restock_fixture._RestockFixture):
    """One fake managed invocation; two candidates use production callbacks.

    The second answer and accounting winner are local fixtures, not another
    real model result. Private capture may reuse this mixin without inheriting
    a concrete TestCase or unrelated test methods.
    """

    def setUp(self):
        from management import tests_ig_source_cart_catalog as catalog_fixture
        original = catalog_fixture.SourceCartCatalogTests.setUp

        def catalog_before_sources(case):
            original(case)
            case.category.name, case.category.slug = "Худі", "hoodies"
            case.category.save(update_fields=["name", "slug"])
            case.products[0].price = case.variants[0].price_override = 900
            case.products[0].save(update_fields=["price"])
            case.variants[0].save(update_fields=["price_override"])

        with patch.object(catalog_fixture.SourceCartCatalogTests, "setUp", catalog_before_sources):
            super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self._publish_reviewed_policy()
        self.client_row.language = "ru"
        self.client_row.save(update_fields=["language"])
        self.customer = self.client_row
        self.canonical("Хочу худи Restock Aurora, количество 1, размер M, цвет black, крой classic")
        self.source_row = self.source("Хочу это худи фиолетового цвета.")
        self.revision, self.token = self.revision(self.source_row)
        self.generation_calls = []
        self.actual_inline_count = 0
        self.actual_hash_override = "observed"
        self.provider_failure = False
        self.before_validate = self.after_validate = None
        self.candidate_decisions = []
        self.repair_calls = 0
        self.primary_payload = self.repair_payload = self.native_request_policy_manifest = None

    def _publish_reviewed_policy(self):
        from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, reviewed_scenario_values
        from management.services.ig_policy_publication import draft_state, save_instruction_draft, publish_instruction_policy
        from management.services.ig_revision_outbox import PublicationBinding
        state = draft_state()
        for row in SCENARIO_INSTRUCTIONS:
            _, state = save_instruction_draft(expected_revision=state.revision,
                expected_snapshot_hash=state.snapshot_hash, values=reviewed_scenario_values(row["key"]),
                note="offline native language repair fixture")
        result = publish_instruction_policy(expected_draft_revision=state.revision,
            expected_draft_hash=state.snapshot_hash, expected_head_id=self.publication.pk,
            expected_head_hash=self.publication.snapshot_hash, note="offline native language repair fixture")
        self.publication = result.publication
        self.binding = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        self.settings.refresh_from_db()

    def _source_head(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        return capture_current_selection_lines(self.customer.pk)

    def _durable_selection(self):
        from management.models import IgCommerceSelectionSession
        from management.services.ig_commerce_projection import _legacy_line
        self.customer.refresh_from_db()
        session = IgCommerceSelectionSession.objects.get(pk=self.source_head_before["session_id"])
        return {"session_id": session.pk, "generation": session.generation,
            "revision": session.revision, "snapshot": session.snapshot(),
            "current_product_id": self.customer.current_product_id,
            "legacy_line": _legacy_line(self.customer, session.generation)}

    def _source_fence_rows(self):
        from management.models import InstagramBotMessage
        from management.services.ig_commerce_projection import _source_fence_row
        return {row.pk: _source_fence_row(row) for row in InstagramBotMessage.objects.filter(
            pk__in=self.source_head_before["fence"]["source_ids"]).select_related("client")}

    def _generate_repair(self, payload, **kwargs):
        from management import tests_ig_revision_live as native_fixture
        from management.models import IgBotNotification, IgFollowUpTask

        self.primary_payload = deepcopy(payload)
        self.native_request_policy_manifest = deepcopy(kwargs["request_policy_manifest"])
        self.source_head_before = deepcopy(self._source_head())
        self.durable_selection_before = deepcopy(self._durable_selection())
        self.source_fence_rows_before = deepcopy(self._source_fence_rows())
        self.revision.refresh_from_db()
        receipts_before = deepcopy(self.revision.action_receipts)
        cases_before = IgFollowUpTask.objects.filter(client=self.customer).count()
        notices_before = IgBotNotification.objects.filter(client=self.customer).count()
        original_validator = kwargs["result_validator"]

        def validate(candidate, *, usage):
            decision = original_validator(candidate, usage=usage)
            self.candidate_decisions.append((deepcopy(candidate), decision))
            return decision

        kwargs["result_validator"] = validate
        first = deepcopy(H4B_WRONG_UKRAINIAN_CANDIDATE)
        rejected = validate(first, usage={"_request_inline_count": 0, "_request_inline_content_hashes": []})
        self.assertFalse(rejected.valid)
        self.assertEqual(rejected.reason_codes, ("reply_language_mismatch",))
        self.http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertFalse(self.revision.generation_proposal)
        self.assertEqual(self.revision.action_receipts, receipts_before)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer).count(), cases_before)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), notices_before)
        self.assertEqual(self._source_head(), self.source_head_before)
        self.assertEqual(self._durable_selection(), self.durable_selection_before)

        self.repair_calls += 1
        repaired = kwargs["repair_payload_factory"](payload, first, rejected.reason_codes)
        self.assertIsInstance(repaired, dict)
        self.repair_payload = deepcopy(repaired)
        self.assertEqual(payload, self.primary_payload, "Repair must not rewrite its initial request.")
        self.assertEqual(repaired["system_instruction"], payload["system_instruction"])
        self.assertEqual(repaired["generationConfig"], payload["generationConfig"])
        self.assertEqual(repaired["contents"][:len(payload["contents"])], payload["contents"])
        additions = repaired["contents"][len(payload["contents"]):]
        previous = [json.loads(part["text"]) for item in additions if item["role"] == "model" for part in item["parts"]]
        self.assertEqual(previous, [H4B_WRONG_UKRAINIAN_CANDIDATE])
        hints = "\n".join(part.get("text", "") for item in additions if item["role"] == "user" for part in item["parts"])
        self.assertIn("Reply in Russian.", hints)
        self.assertIn("previous candidate used the wrong reply language", hints)
        self.assertIn("SAME SEALED CUSTOMER BUNDLE", hints)
        self.assertIn("Хочу это худи фиолетового цвета", hints)
        self.assertEqual(first, H4B_WRONG_UKRAINIAN_CANDIDATE)

        # Existing native fixture admits the second candidate through the SAME
        # real callback, then records its fake accounting winner for outbox.
        self.parsed = deepcopy(RUSSIAN_REPAIR_CANDIDATE)
        return native_fixture.RevisionLiveTests._generate(self, repaired, **kwargs)

    def run_repair(self):
        from management.services.ig_revision_live import execute_claimed_revision
        from management.tests_ig_revision_live import native_provider_candidate_plan
        with native_provider_candidate_plan(), \
                patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate_repair) as generate, \
                patch("management.services.instagram_bot.get_page_token", return_value="offline-language-token"), \
                patch("management.services.instagram_bot._provider_http", return_value=(200, json.dumps({"message_id": "russian-repair-once"}))) as http, \
                patch("management.services.instagram_bot._register_outgoing_message"):
            self.http = http
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        return result, generate, http


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE="unified",
    IG_CLIENT_STATE_PROMPT_TOKENS=2400, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class ManuNativeRussianRepairTests(_NativeRussianRepairFixture, TransactionTestCase):
    reset_sequences = True

    def test_actual_wrong_ukrainian_candidate_repairs_once_to_one_russian_sent_reply(self):
        import hashlib
        from management.models import IgBotNotification, IgFollowUpTask
        from management.services.ig_revision_live import execute_claimed_revision

        self.assertEqual(hashlib.sha256(json.dumps(H4B_WRONG_UKRAINIAN_CANDIDATE,
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "e654332512a74531d0802f280c22fe10e0d559af8ee0df7fb8a9d3d5745b773d")
        result, generate, http = self.run_repair()
        self.assertEqual((generate.call_count, self.repair_calls, http.call_count), (1, 1, 1))
        self.assertEqual(len(self.candidate_decisions), 2)
        self.assertEqual(self.candidate_decisions[0][0], H4B_WRONG_UKRAINIAN_CANDIDATE)
        self.assertTrue(self.candidate_decisions[1][1].valid, self.candidate_decisions[1][1].reason_codes)
        self.revision.refresh_from_db()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(self.revision.state, self.revision.State.PROCESSED)
        self.assertFalse(self.revision.action_receipts["response_coverage"].get("remaining"))
        effect = self.revision.delivery_effects.get()
        self.assertEqual((effect.kind, effect.state, effect.provider_message_id), ("text", "sent", "russian-repair-once"))
        self.assertEqual(effect.payload["message"]["text"], RUSSIAN_REPAIR_CANDIDATE["reply_text"])
        task = IgFollowUpTask.objects.get(client=self.customer)
        self.assertEqual(task.reason, "revision_case:product_configuration_review")
        inquiry = task.manager_context["product_inquiries"]["inquiries"][0]
        self.assertEqual((inquiry["source_message_id"], inquiry["product_id"], inquiry["axis"], inquiry["requested_value"]),
            (self.source_row.pk, self.product.pk, "color", "purple"))
        self.assertIn("фиолетового", inquiry["question"])
        notice = IgBotNotification.objects.get(client=self.customer)
        self.assertEqual(notice.payload["manager_task_id"], task.pk)
        self.assertEqual(notice.payload["revision_id"], self.revision.pk)
        self.source_head_after = self._source_head()
        self.assertEqual(self._durable_selection(), self.durable_selection_before)
        source_rows_after = self._source_fence_rows()
        self.assertEqual(source_rows_after.keys(), self.source_fence_rows_before.keys())
        for source_id, original in self.source_fence_rows_before.items():
            before, after = deepcopy(original), deepcopy(source_rows_after[source_id])
            before_status, after_status = before.pop("status"), after.pop("status")
            self.assertEqual(after, before, "Source content/ownership/ingress evidence must remain exact.")
            if after_status != before_status:
                self.assertIn(before_status, {"pending", "processing"})
                self.assertEqual(after_status, "done")
        # Native reply finalization marks source messages processed. That is
        # part of source_digest and consequently capture_digest, not a change
        # to session/head, selection evidence, cart values or their authority.
        def semantic_head(value):
            value = deepcopy(value)
            value.pop("capture_digest")
            value["fence"].pop("source_digest")
            return value
        self.assertEqual(semantic_head(self.source_head_after), semantic_head(self.source_head_before))
        with patch("management.services.call_ai_analysis.gemini_generate_text") as replay_model, \
                patch("management.services.instagram_bot._provider_http") as replay_http:
            replay = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(replay.state, "completed", replay.reasons)
        replay_model.assert_not_called()
        replay_http.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer).count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), 1)
        self.assertEqual(self._durable_selection(), self.durable_selection_before)
