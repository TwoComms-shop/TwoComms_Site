"""Captured policy inputs: DB-only fixtures; no provider or business effects."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone

from management.models import IgClient, IgCommercialEpisode, IgPostSaleCase, InstagramBotMessage
from management.services.bot_playbooks import active_instruction_selection
from management.services.ig_captured_policy_inputs import capture_policy_inputs
from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
from management.services.ig_policy_publication import ActivePolicySnapshot
from management.services.ig_turn_intelligence import SCOPE_KEYS, TurnContextError, capture_digest


class CapturedPolicyInputsTests(TestCase):
    def setUp(self):
        self.client_row = IgClient.objects.create(igsid="captured-policy-client", language="ru",
            primary_objection="price", stage="paid", intent="payment")
        self.at = timezone.now()
        self.source = InstagramBotMessage.objects.create(client=self.client_row,
            sender_id=self.client_row.igsid, role="user", source="webhook", status="pending",
            provider_namespace="instagram_login:policy-owner", text="Це не дорого.", provider_created_at=self.at)
        self.sources = [dict(message_id=self.source.pk, role="user", text=self.source.text,
            source_namespace=self.source.provider_namespace, provider_created_at=self.at.isoformat(),
            source_digest="a" * 64)]
        self.boundary = dict(client_id=self.client_row.pk, episode_id=None, order_id=None,
            line_id="", recipient_id="self", reset_id=None, reset_floor=1, erasure_epoch="",
            source_namespace=self.source.provider_namespace, source_ids=[self.source.pk],
            source_digests={str(self.source.pk): "a" * 64}, sealed_sources_digest=capture_digest(self.sources),
            watermark=dict(message_id=self.source.pk, event_at=self.at.isoformat()), history_digests={})
        self.plan = SimpleNamespace(choices={}, scope={})

    def capture(self, *, history=()):
        return capture_policy_inputs(self.client_row.pk, boundary=self.boundary, sources=self.sources,
            history=history, response_plan=self.plan, captured_at=timezone.now())

    def text(self, value):
        self.source.text = value
        self.source.save(update_fields=["text"])
        self.sources[0]["text"] = value
        self.boundary["sealed_sources_digest"] = capture_digest(self.sources)

    def publication(self):
        modules = [dict(id="instruction:" + str(index), source_id=index, title=code,
            body="Reviewed " + code + " guidance", active=True, priority=index,
            locale=code, tags=["global"], triggers=[], trust_scope="public_policy", allowed_actions=[])
            for index, code in enumerate(("uk", "ru", "en"), 1)]
        modules.append(dict(id="instruction:4", source_id=4, title="Discount", body="Discount guidance",
            active=True, priority=4, locale="all", tags=["discount"], triggers=[],
            trust_scope="public_policy", allowed_actions=[]))
        snapshot = {"schema_version": 1, "instructions": modules}
        return ActivePolicySnapshot(1, 1, capture_digest(snapshot), "instruction-set-v1", snapshot)

    def test_negated_current_price_excludes_stale_profile_objection_stage_and_intent(self):
        result = self.capture()
        self.assertFalse(result.tags & {"price", "discount", "paid", "payment"})
        with patch("management.services.bot_playbooks.tags_for_client", side_effect=AssertionError("uncaptured reread")):
            selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
                publication_snapshot=self.publication(), turn_text=self.source.text)
        self.assertNotIn("instruction:4", selected.selected_ids)

    def test_active_current_objection_selects_price_discount_without_global_state(self):
        self.text("Мені дорого.")
        result = self.capture()
        self.assertTrue({"price", "discount"} <= result.tags)
        selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
            publication_snapshot=self.publication(), turn_text=self.source.text)
        self.assertIn("instruction:4", selected.selected_ids)

    def objection_publication(self):
        original = self.publication()
        snapshot = deepcopy(original.snapshot)
        for index, tags, triggers in ((5, ["objection_price"], []), (6, [], ["price_objection"]),
                                     (7, [], ["hesitation"]), (8, [], ["price_question"])):
            snapshot["instructions"].append(dict(id="instruction:" + str(index), source_id=index,
                title="Scenario " + str(index), body="Reviewed scenario guidance", active=True,
                priority=index, locale="all", tags=tags, triggers=triggers,
                trust_scope="public_policy", allowed_actions=[]))
        return ActivePolicySnapshot(1, 1, capture_digest(snapshot), "instruction-set-v1", snapshot)

    def test_published_alias_and_semantic_trigger_select_actual_current_price_objection(self):
        self.text("Мені дорого.")
        result = self.capture()
        self.assertIn("objection_price", result.tags)
        selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
            publication_snapshot=self.objection_publication(), turn_text=self.source.text)
        self.assertTrue({"instruction:5", "instruction:6"} <= set(selected.selected_ids))

    def test_captured_aliases_follow_shared_current_kinds_without_ledger_reads(self):
        for text, kind in (("Ще подумаю.", "thinking"), ("Не довіряю передоплаті.", "prepayment_trust"),
                           ("Боюсь, що розмір не підійде.", "size_risk")):
            with self.subTest(kind=kind):
                self.text(text)
                with patch("management.services.ig_objections.objection_tags_for_client",
                           side_effect=AssertionError("uncaptured ledger read")):
                    self.assertIn("objection_" + kind, self.capture().tags)

    def test_published_objection_rules_omit_negated_quoted_reported_and_historical_sources(self):
        for text in ("Це не дорого.", "«Дорого» — це цитата.", "Друг сказав, що дорого.",
                     "Раніше було дорого.", "Мені дорого. Це не дорого.", "Привіт", "Дякую"):
            with self.subTest(text=text):
                self.text(text)
                result = self.capture()
                self.assertNotIn("objection_price", result.tags)
                selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
                    publication_snapshot=self.objection_publication(), turn_text=text)
                self.assertFalse({"instruction:4", "instruction:5", "instruction:6"} & set(selected.selected_ids))
                reasons = {item.id: item.reason for item in selected.omitted}
                self.assertEqual(reasons["instruction:5"], "not_relevant")
                self.assertEqual(reasons["instruction:6"], "not_relevant")

    def test_published_plain_price_question_preserves_question_without_objection_rule(self):
        self.text("Скільки коштує футболка?")
        result = self.capture()
        selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
            publication_snapshot=self.objection_publication(), turn_text=self.source.text)
        self.assertIn("instruction:8", selected.selected_ids)
        self.assertNotIn("instruction:6", selected.selected_ids)

    def test_published_hesitation_rule_requires_current_own_concern(self):
        for text, expected in (("Ще подумаю.", True), ("Не подумаю.", False),
                               ('«Подумаю» — цитата.', False), ("Раніше думав: подумаю.", False)):
            with self.subTest(text=text):
                self.text(text)
                result = self.capture()
                selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
                    publication_snapshot=self.objection_publication(), turn_text=text)
                self.assertEqual("instruction:7" in selected.selected_ids, expected)

    def test_semantic_selector_keeps_existing_whole_module_budget_omission(self):
        self.text("Мені дорого.")
        result = self.capture()
        selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
            publication_snapshot=self.objection_publication(), turn_text=self.source.text, budget_chars=0)
        reasons = {item.id: item.reason for item in selected.omitted}
        self.assertEqual(reasons["instruction:5"], "budget_exhausted")
        self.assertEqual(reasons["instruction:6"], "budget_exhausted")
        self.assertFalse(selected.modules)

    def test_current_ru_en_language_selects_actual_publication_modules_and_state_prompt(self):
        for code, text in (("ru", "Пожалуйста, отвечайте на русском языке."), ("en", "Please answer in English.")):
            with self.subTest(code=code):
                self.text(text)
                result = self.capture()
                self.assertEqual(result.knowledge_language, code)
                self.assertEqual(result.state_slots["language"]["status"], "confirmed")
                self.assertEqual(result.state_slots["language"]["source_refs"][0]["id"], self.source.pk)
                self.client_row.language = "uk"  # assembly must not consult this changed profile
                selected = active_instruction_selection(self.client_row, captured_tags=result.tags,
                    publication_snapshot=self.publication(), turn_text=text)
                self.assertEqual(selected.selected_ids, ("instruction:2",) if code == "ru" else ("instruction:3",))
                state = assemble_client_state(boundary={**self.boundary, "source_watermark": self.boundary["watermark"]},
                    components={"slots": result.state_slots}, captured_at=timezone.now())
                self.assertEqual(state.as_dict()["slots"]["language"]["value"], code)
                self.assertIn('"value":"' + code + '"', render_client_state_prompt(state, budget=2400).text)

    def test_explicit_request_wins_over_observed_current_language(self):
        self.text("Please answer in Russian.")
        self.assertEqual(self.capture().knowledge_language, "ru")

    def test_profile_language_remains_unknown_slot_but_compatible_knowledge_hint(self):
        self.text("❤️")
        result = self.capture()
        self.assertEqual(result.knowledge_language, "ru")
        self.assertEqual(result.state_slots["language"]["status"], "unknown")
        self.assertIsNone(result.state_slots["language"]["value"])
        self.assertEqual(result.metadata["knowledge_language_basis"], "stored_profile_hint")

    def test_validated_customer_history_can_supply_language_but_manager_cannot(self):
        self.text("❤️")
        # History identity must precede the current source.
        self.sources[0]["message_id"] += 1
        self.boundary.update(source_ids=[self.sources[0]["message_id"]],
            source_digests={str(self.sources[0]["message_id"]): "a" * 64},
            sealed_sources_digest=capture_digest(self.sources),
            watermark=dict(message_id=self.sources[0]["message_id"], event_at=self.at.isoformat()))
        row = dict(message_id=self.source.pk, text="Please answer in English.", role="user",
            event_at=(self.at-timedelta(seconds=1)).isoformat(), scope={key: self.boundary[key] for key in SCOPE_KEYS})
        self.boundary["history_digests"] = {str(row["message_id"]): capture_digest(row)}
        self.assertEqual(self.capture(history=[row]).knowledge_language, "en")
        row["role"] = "manager"
        self.boundary["history_digests"][str(row["message_id"])] = capture_digest(row)
        self.assertEqual(self.capture(history=[row]).knowledge_language, "ru")

    def test_service_case_is_frozen_before_later_case_mutation(self):
        case = IgPostSaleCase.objects.create(client=self.client_row, source_message=self.source,
            case_type="exchange", status="open")
        with CaptureQueriesContext(connection) as queries:
            result = self.capture()
        self.assertLessEqual(len(queries), 4)
        self.assertEqual(result.metadata["service"]["status"], "captured")
        self.assertTrue({"service", "post_sale", "exchange"} <= result.tags)
        self.assertNotIn("sales", result.tags)
        original = result.automation_note
        case.status, case.case_type = "cancelled", "return"
        case.save(update_fields=["status", "case_type", "updated_at"])
        with self.assertNumQueries(0):
            self.assertEqual(result.automation_note, original)
            self.assertEqual(result.metadata["service"]["case_status"], "open")
            active_instruction_selection(self.client_row, captured_tags=result.tags,
                publication_snapshot=self.publication(), turn_text=self.source.text)

    def test_foreign_episode_service_case_does_not_gain_current_routing_authority(self):
        episode = IgCommercialEpisode.objects.create(client=self.client_row, sequence=1,
            materialization_key="policy-old-episode", open_slot=None)
        IgPostSaleCase.objects.create(client=self.client_row, source_message=self.source,
            case_type="return", commercial_episode=episode)
        result = self.capture()
        self.assertEqual(result.metadata["service"]["status"], "unknown")
        self.assertNotIn("return", result.tags)

    def test_capture_does_no_writes_and_returns_defensive_views(self):
        with CaptureQueriesContext(connection) as queries:
            result = self.capture()
        self.assertLessEqual(len(queries), 4)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in queries))
        result.tags.add("discount")
        slots, metadata = result.state_slots, result.metadata
        slots["language"]["value"] = "en"
        metadata["service"]["status"] = "changed"
        self.assertNotIn("discount", result.tags)
        self.assertNotEqual(result.state_slots["language"]["value"], "en")
        self.assertNotEqual(result.metadata["service"]["status"], "changed")

    def test_changed_scope_or_digest_rejects_before_optional_reads(self):
        for change in ("namespace", "reset", "digest", "erasure"):
            boundary = deepcopy(self.boundary)
            if change == "namespace":
                boundary["source_namespace"] = "foreign"
            elif change == "reset":
                boundary["reset_floor"] = self.source.pk + 1
            elif change == "digest":
                boundary["sealed_sources_digest"] = "f" * 64
            else:
                boundary["erasure_epoch"] = self.at.isoformat()
            with self.subTest(change=change), self.assertNumQueries(0), self.assertRaises(TurnContextError):
                capture_policy_inputs(self.client_row.pk, boundary=boundary, sources=self.sources,
                    history=[], response_plan=self.plan, captured_at=timezone.now())
