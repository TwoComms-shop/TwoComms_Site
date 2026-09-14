"""Historical inactivity cannot invent a current response-delay apology."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import json

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management.services.ig_revision_conversation_context import (
    conversation_timing_guidance, historical_message_text,
    normalize_response_delay_apology, response_delay_apology_allowed,
)
from management.services.ig_revision_outbox import _digest
from management.services.ig_response_guard import ProviderResponseGuard
from management.services.ig_reply_truth import ReplyTruthContext
from management import tests_ig_revision_live as live_fixtures


class RevisionConversationContextTests(SimpleTestCase):
    def revision(self, text="Проверка тайпинга", **extra):
        snapshot = {"sources": [{"message_id": 2951, "role": "user", "text": text,
                                 "provider_created_at": "2026-09-14T09:54:58.962000+00:00"}]}
        return SimpleNamespace(pk=32, bundle_snapshot=snapshot, snapshot_digest=_digest(snapshot),
                               origin=extra.pop("origin", "inbound"), **extra)

    def test_fresh_test_turn_has_no_delay_claim_from_old_pause_or_holding(self):
        revision = self.revision()
        draft = "Вибачте за затримку! Підкажіть, будь ласка, чим саме можу допомогти?"
        self.assertEqual(normalize_response_delay_apology(draft, revision),
                         "Підкажіть, будь ласка, чим саме можу допомогти?")
        guidance = conversation_timing_guidance(revision)
        self.assertIn('"response_delay_apology_supported":false', guidance)
        self.assertIn("2026-09-14T09:54:58.962000+00:00", guidance)
        self.assertIn("intentional operator/customer pauses", guidance)
        self.assertNotIn(revision.bundle_snapshot["sources"][0]["text"], guidance)

    def test_only_standalone_leading_response_delay_prefix_is_removed(self):
        revision = self.revision()
        for prefix in ("Перепрошую за технічну затримку.", "Извините за задержку с ответом!", "Sorry for the delay."):
            with self.subTest(prefix=prefix):
                self.assertEqual(normalize_response_delay_apology(prefix + " Чим можу допомогти?", revision),
                                 "Чим можу допомогти?")
        for draft in ("Вибачте за затримку.", "Вибачте за помилку з розміром. Підкажіть номер замовлення.",
                      "Вибачте за затримку доставки. Перевірмо замовлення.",
                      "Вибачте. Перевірмо, що сталося з товаром.",
                      "Вітаю! Вибачте за затримку. Чим можу допомогти?",
                      'Ви написали: "Вибачте за затримку!"'):
            with self.subTest(draft=draft):
                self.assertEqual(normalize_response_delay_apology(draft, revision), draft)

    def test_current_response_and_shipping_complaints_keep_legitimate_apology(self):
        draft = "Вибачте за затримку! Перевірмо ваше звернення."
        for text in ("Почему вы не отвечаете?", "Чекаю на відповідь уже довго.", "Моє замовлення не прийшло."):
            with self.subTest(text=text):
                revision = self.revision(text)
                self.assertTrue(response_delay_apology_allowed(revision))
                self.assertEqual(normalize_response_delay_apology(draft, revision), draft)

    def test_recovery_requires_current_same_source_lineage_not_an_old_incident(self):
        draft = "Вибачте за технічну затримку. Чим можу допомогти?"
        revision = self.revision(origin="outage_recovery")
        with patch("management.services.ig_revision_recovery.recovery_lineage_for_authority", return_value=([revision], "")):
            self.assertEqual(normalize_response_delay_apology(draft, revision), draft)
        with patch("management.services.ig_revision_recovery.recovery_lineage_for_authority", return_value=([], "recovery_lineage_invalid")):
            self.assertEqual(normalize_response_delay_apology(draft, revision), "Чим можу допомогти?")

    def test_historical_holding_is_dated_and_labeled_as_evidence(self):
        when = timezone.now() - timedelta(days=13)
        row = SimpleNamespace(pk=2837, role="model", source="ai_holding", status="done",
                              provider_created_at=when, created_at=timezone.now(), text="Вибачте за технічну затримку.")
        for source in ("ai_holding", "revision_holding", "unknown"):
            with self.subTest(source=source):
                row.source = source
                rendered = historical_message_text(row)
                metadata = json.loads(rendered.split("\n", 1)[1])
                self.assertEqual(metadata["technical_holding"], source != "unknown")
                self.assertEqual(metadata["source"], source)
                self.assertEqual(metadata["occurred_at"], when.isoformat())
                self.assertEqual(metadata["text"], row.text)
                self.assertIn("not current delay", rendered)

    def test_normalized_candidate_still_runs_truth_guard_and_preserves_provider_source(self):
        from dataclasses import replace
        revision = self.revision()
        guard = ProviderResponseGuard(context_factory=lambda _control, _text: ReplyTruthContext(),
            response_normalizer=lambda response: replace(response,
                reply_text=normalize_response_delay_apology(response.reply_text, revision)))
        parsed = {"reply_text": "Вибачте за затримку! Підкажіть, чим можу допомогти?", "controls": []}
        original = deepcopy(parsed)
        self.assertTrue(guard.validate(parsed).valid)
        self.assertIs(guard.source, parsed)
        self.assertEqual(parsed, original)
        self.assertEqual(guard.response.reply_text, "Підкажіть, чим можу допомогти?")
        rejected = guard.validate({"reply_text": "Вибачте за затримку! Оплата підтверджена.", "controls": []})
        self.assertFalse(rejected.valid)
        self.assertIn("unverified_payment", rejected.reason_codes)
        self.assertIsNone(guard.response)

    def test_default_guard_keeps_legacy_reply_unchanged(self):
        guard = ProviderResponseGuard(context_factory=lambda _control, _text: ReplyTruthContext())
        draft = "Вибачте за затримку! Підкажіть, чим можу допомогти?"
        self.assertTrue(guard.validate({"reply_text": draft, "controls": []}).valid)
        self.assertEqual(guard.response.reply_text, draft)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionConversationContextIntegrationTests(TransactionTestCase):
    reset_sequences = True
    setUp = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _generate = live_fixtures.RevisionLiveTests._generate
    _execute = live_fixtures.RevisionLiveTests._execute

    def test_normalized_winner_is_stored_and_sent_once_with_original_model_lineage(self):
        from management.models import GeminiRequest, InstagramBotMessage
        from management.services.ig_revision_execution import finalize_sent_revision_effects

        self._prepare()
        clean = "Можу допомогти з вибором. Який стиль вам подобається?"
        self.parsed = {"reply_text": "Вибачте за затримку! " + clean, "controls": []}
        original = deepcopy(self.parsed)
        with self.assertLogs("management.services.ig_revision_live", level="INFO") as logs:
            result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.parsed, original)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["response"]["reply_text"], clean)
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.payload["message"]["text"], clean)
        self.assertEqual(effect.state, "sent")
        self.assertEqual(effect.generation_model, "gemini-3.7-flash")
        self.assertEqual(effect.generation_request_id, f"live-request-{self.revision.pk}")
        graph = GeminiRequest.objects.get(request_id=effect.generation_request_id)
        self.assertEqual(graph.winner_attempt.model, effect.generation_model)
        self.assertEqual(graph.terminal_resolution, "succeeded")
        self.assertEqual(InstagramBotMessage.objects.get(source="revision_reply").text, clean)
        telemetry = "\n".join(logs.output)
        self.assertIn("unsupported_response_delay_prefix", telemetry)
        self.assertNotIn(original["reply_text"], telemetry)
        with patch("management.services.instagram_bot._provider_http") as send:
            self.assertTrue(finalize_sent_revision_effects(self.revision.pk).completed)
        send.assert_not_called()

    def test_current_source_text_and_timestamp_remain_exact_in_history(self):
        from management.services.ig_revision_live import build_sealed_history
        when = timezone.now() - timedelta(seconds=9)
        self.source.provider_created_at = when
        self.source.save(update_fields=["provider_created_at"])
        # Seal a new source envelope so provider time is immutable source data.
        from management.services.ig_turn_revisions import create_collecting_revision
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        original = deepcopy(self.revision.bundle_snapshot)
        history = build_sealed_history(self.revision)
        tail = json.loads(history[-1]["text"].split("\n", 1)[1])
        self.assertEqual(tail[0]["text"], self.source.text)
        self.assertEqual(tail[0]["provider_created_at"], when.isoformat())
        self.assertEqual(history[-1]["role"], "user")
        self.assertEqual(self.revision.bundle_snapshot, original)
