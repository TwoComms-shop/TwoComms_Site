"""One-call conversion diagnostics with native source/receipt ownership."""
import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import IgCommercialEpisode, IgConversationAnalysisSnapshot, InstagramBotMessage, IgConversationAnalysisJob
from management.services import bot_conversation_analysis as analysis
from management.services.ig_analysis_reply_review import build_reply_review_context, public_conversion_audit
from management.services.ig_revision_outbox import claim_next_effect, mark_provider_started, finish_effect
from management import tests_ig_revision_delivery as delivery


@override_settings(IG_ANALYSIS_V2_MODE="off", IG_ANALYSIS_MATERIALITY_MODE="off",
                   GOOGLE_INDEXING_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class ConversionAuditAnalyzerTests(TransactionTestCase):
    _payload = delivery.RevisionDeliveryTests._payload
    _plan = delivery.RevisionDeliveryTests._plan

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"}))
        delivery.RevisionDeliveryTests.setUp(self)
        self.settings.ig_user_id = "owner-1"
        self.settings.analysis_reconcile_after = timezone.now() - timedelta(days=1)
        self.settings.save(update_fields=["ig_user_id", "analysis_reconcile_after"])
        self.source.status = "done"
        self.source.save(update_fields=["status"])
        self.enterContext(patch.object(analysis, "_customer_reply_work_waiting", return_value=False))

    def _send(self):
        self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("Confirmed reply")}])
        claim = claim_next_effect(self.revision.pk, self.revision_token, "substantive_text")
        mark_provider_started(claim.effect.pk, claim.token, self.revision_token)
        return claim

    def _finish(self, claim):
        return finish_effect(claim.effect.pk, claim.token, provider_namespace="instagram_login:owner-1",
                             http_status=200, provider_message_id="native-audit-sent")

    def _parsed(self, message, *, optional=None):
        return {"parsed": {"interaction_type": "product_interest", "score_band": "qualified",
            "purchase_probability": .7, "confidence": .8,
            "evidence": [{"message_id": message.pk, "quote": message.text, "claim": "customer request"}],
            "conversion_audit": optional}, "model": "fake-analysis", "meta": {}}

    def _run(self, message, value):
        analysis.schedule_analysis(self.client_row, message, now=timezone.now() - timedelta(minutes=1))
        with patch.object(analysis, "gemini_generate_json", return_value=value) as gateway:
            result = analysis.process_due_analysis(limit=1)
        self.assertEqual(result["done"], 1, result)
        gateway.assert_called_once()
        snapshot = IgConversationAnalysisSnapshot.objects.filter(client=self.client_row, analysis_model="fake-analysis").latest("id")
        return snapshot, gateway

    def test_native_receipt_extends_same_request_after_source_watermark(self):
        claim = self._send()
        before = build_reply_review_context(self.client_row, self.source.pk)
        self.assertEqual(before["coverage"], "reply_not_yet_observable")
        self._finish(claim)
        snapshot, gateway = self._run(self.source, self._parsed(self.source))
        request = json.loads(gateway.call_args.args[1])
        context = request["response_review_context"]
        self.assertEqual(context["coverage"], "sent_receipts_observed")
        self.assertEqual(context["diagnostics"][0]["text"], "Confirmed reply")
        self.assertEqual(context["diagnostics"][0]["source_message_ids"], [self.source.pk])
        self.assertNotIn("Confirmed reply", [row["text"] for row in request["conversation"]])
        self.assertNotEqual(before["digest"], context["digest"])
        self.assertEqual(snapshot.conversion_audit["schema_version"], 1)

    def test_legacy_empty_to_new_ingress_persist_then_no_change_uses_one_gateway(self):
        legacy_fingerprint = analysis._required_state_fingerprint(self.client_row, self.source.pk)
        IgConversationAnalysisSnapshot.objects.create(client=self.client_row, last_analyzed_message=self.source,
            dedupe_key="legacy-empty", score_band="qualified", analysis_model="old-model",
            required_state_fingerprint=legacy_fingerprint, analysis_prompt_version=analysis.ANALYSIS_PROMPT_VERSION)
        self.assertEqual(analysis._required_state_fingerprint(self.client_row, self.source.pk), legacy_fingerprint)
        message = InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1", role="user", source="webhook", status="done",
            text="Хочу замовити для себе", mid="new-audit-user")
        snapshot, gateway = self._run(message, self._parsed(message, optional={"schema_version": 1,
            "motivation": [{"code": "self", "basis": "customer_stated", "confidence": "high",
                "evidence": [{"message_id": message.pk, "quote": "для себе"}]}]}))
        current = analysis._required_state_fingerprint(self.client_row, message.pk)
        self.assertEqual(current, snapshot.required_state_fingerprint)
        future = timezone.now() + timedelta(days=10)
        with patch.object(analysis.timezone, "now", return_value=future):
            self.assertEqual(analysis._required_state_fingerprint(self.client_row, message.pk), current)
        public_conversion_audit(snapshot, self.client_row, fresh=True)
        revision = IgConversationAnalysisJob.objects.get(client=self.client_row).revision
        analysis.schedule_analysis(self.client_row, message)
        self.assertEqual(IgConversationAnalysisJob.objects.get(client=self.client_row).revision, revision)
        with patch.object(analysis, "gemini_generate_json") as later:
            self.assertEqual(analysis.process_due_analysis(limit=1)["done"], 0)
        later.assert_not_called()
        gateway.assert_called_once()

    def test_malformed_optional_audit_keeps_baseline_and_stable_cursor(self):
        snapshot, _gateway = self._run(self.source, self._parsed(self.source, optional=["bad"]))
        self.assertEqual(snapshot.interaction_type, "product_interest")
        self.assertEqual(snapshot.conversion_audit["schema_version"], 1)
        self.assertEqual(snapshot.conversion_audit["nonconversion_reasons"], [])
        self.assertEqual(snapshot.required_state_fingerprint,
                         analysis._required_state_fingerprint(self.client_row, self.source.pk))

    def test_public_reader_revalidates_quotes_and_hides_raw_response_material(self):
        claim = self._send()
        self._finish(claim)
        snapshot, _gateway = self._run(self.source, self._parsed(self.source))
        stored = snapshot.conversion_audit["_response_review"]["diagnostics"][0]
        self.assertNotIn("text", stored)
        self.assertNotIn("provider_message_id", stored)
        snapshot.conversion_audit["motivation"] = [{"code": "gift", "basis": "customer_stated",
            "confidence": "high", "evidence": [{"message_id": self.source.pk, "quote": "invented gift"}]}]
        public = public_conversion_audit(snapshot, self.client_row, fresh=True)
        self.assertEqual(public["motivation"], [])
        diagnostic = public["_response_review"]["diagnostics"][0]
        self.assertNotIn("text", diagnostic)
        self.assertNotIn("provider_message_id", diagnostic)
        self.client_row.privacy_erasure_started_at = timezone.now()
        self.assertEqual(public_conversion_audit(snapshot, self.client_row, fresh=True), {})

    def test_wrong_namespace_mutated_source_reset_and_episode_cannot_lend_receipts(self):
        claim = self._send()
        self._finish(claim)
        self.assertEqual(len(build_reply_review_context(self.client_row, self.source.pk)["diagnostics"]), 1)
        original = self.source.text
        self.source.text = "edited source"
        self.source.save(update_fields=["text"])
        self.assertFalse(build_reply_review_context(self.client_row, self.source.pk)["diagnostics"])
        self.source.text = original
        self.source.save(update_fields=["text"])
        with patch("management.services.ig_analysis_reply_review.current_message_floor", return_value=self.source.pk + 1):
            self.assertFalse(build_reply_review_context(self.client_row, self.source.pk)["diagnostics"])
        episode = IgCommercialEpisode.objects.create(client=self.client_row, sequence=1,
            materialization_key="audit-new-episode", opened_watermark_message_id=0)
        self.client_row.current_commercial_episode = episode
        self.client_row.save(update_fields=["current_commercial_episode"])
        self.assertFalse(build_reply_review_context(self.client_row, self.source.pk)["diagnostics"])
        self.client_row.current_commercial_episode = None
        self.client_row.save(update_fields=["current_commercial_episode"])
        self.source.provider_namespace = "instagram_login:foreign-owner"
        self.source.save(update_fields=["provider_namespace"])
        self.assertFalse(build_reply_review_context(self.client_row, self.source.pk)["diagnostics"])

    def test_receipt_change_during_call_keeps_baseline_but_drops_stale_diagnosis(self):
        claim = self._send()
        analysis.schedule_analysis(self.client_row, self.source, now=timezone.now() - timedelta(minutes=1))
        def during(*args, **kwargs):
            self._finish(claim)
            return self._parsed(self.source)
        with patch.object(analysis, "gemini_generate_json", side_effect=during) as gateway:
            result = analysis.process_due_analysis(limit=1)
        self.assertEqual(result["done"], 1, result)
        gateway.assert_called_once()
        snapshot = IgConversationAnalysisSnapshot.objects.get(client=self.client_row)
        self.assertEqual(snapshot.conversion_audit["_response_review"]["coverage"], "response_context_changed")
        self.assertEqual(snapshot.conversion_audit["nonconversion_reasons"], [])
        self.assertEqual(public_conversion_audit(snapshot, self.client_row, fresh=True), {})
