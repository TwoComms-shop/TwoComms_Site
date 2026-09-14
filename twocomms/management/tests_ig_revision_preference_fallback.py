"""Failed-provider preference replies traverse the real revision outbox once."""
import json
from zoneinfo import ZoneInfo

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live_fixtures
from management.models import (
    GeminiRequest, GeminiRequestAttempt, IgCommerceTurnDecision,
    IgCustomerTurn, IgTurnMessage, InstagramBotMessage,
)
from management.services.ig_revision_commerce import reduce_revision_commerce
from management.services.ig_revision_input import record_source_preference_fallback
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_revisions import create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionPreferenceFallbackIntegrationTests(TransactionTestCase):
    reset_sequences = True
    _live_setup = live_fixtures.RevisionLiveTests.setUp
    _message = live_fixtures.RevisionLiveTests._message
    _prepare = live_fixtures.RevisionLiveTests._prepare
    _execute = live_fixtures.RevisionLiveTests._execute

    def setUp(self):
        self._live_setup()

    def _generate(self, *_args, **_kwargs):
        self.fail("An existing failed request must not start another provider generation")

    def _fit_fixture(self, *, fit_text="Оверсайз", media=False):
        sources = [self._message("Хочу оформить заказ", "fit-order"),
                   self._message("Рост 190, вес 100, чёрная футболка", "fit-body"),
                   self._message(fit_text, "fit-choice")]
        if media:
            sources[-1].attachment_media = [{
                "source_part_id": "mp1_" + "7" * 32, "original_index": 0,
                "type": "image", "status": "failed", "capture_terminal": True,
            }]
            sources[-1].save(update_fields=["attachment_media"])
        self.source = sources[-1]
        self.fit_sources = sources
        self.turn = IgCustomerTurn.objects.create(
            client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now(),
        )
        for ordinal, source in enumerate(sources, start=1):
            IgTurnMessage.objects.create(turn=self.turn, message=source, ordinal=ordinal, role="user")
        self.revision = create_collecting_revision(self.turn, sources, bypass_quiet=True).revision
        # Failed terminal media is source-bound and sealed without downloading.
        self._prepare()
        publication = PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash)
        reduced = reduce_revision_commerce(
            self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, publication=publication,
        )
        self.assertTrue(reduced.ready, reduced.reason)
        self.revision.refresh_from_db()
        from management.services.gemini_accounting_runtime import revision_request_execution
        from management.services.ig_revision_provider_execution import revision_provider_continuation
        from management.tests_gemini_accounting_shadow import _raw_plan
        frozen = revision_provider_continuation(
            self.revision.pk, self.token, settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch, candidate_plan=_raw_plan(),
        )
        self.assertTrue(frozen.ready, frozen.reason)
        with revision_request_execution(self.revision.pk, self.token, settings_id=self.settings.pk,
                                        settings_permission_epoch=self.settings.reply_permission_epoch):
            graph = GeminiRequest.objects.create(
                request_id=f"failed-fit-{self.revision.pk}", lane="live", task_class="simple_live",
                logical_turn_id=f"ig-revision:{self.revision.pk}",
                source_execution_key=f"ig-revision:{self.revision.pk}",
                client_id=self.customer.pk, source_message_id=self.source.pk,
                accounting_mode="shadow", terminal_resolution="failed", terminal_reason="provider_empty_exhausted",
            )
            GeminiRequestAttempt.objects.create(
                request_id=graph.request_id, request_graph=graph, role="chat", key_name="GEMINI_API",
                model="gemini-3.7-flash", outcome="empty_response", fsm_state="failed",
                provider_started_at=timezone.now(), dispatch_pacific_day=timezone.now().astimezone(ZoneInfo("America/Los_Angeles")).date(),
                logical_turn_id=graph.logical_turn_id,
                client_id=graph.client_id, source_message_id=graph.source_message_id,
                lane="live", attempt_index=1, candidate_index=1, accounting_mode="shadow",
            )
        self.failed_graph = graph
        return self._record()

    def _record(self):
        return record_source_preference_fallback(self.revision.pk, self.token, settings_id=self.settings.pk)

    def test_failed_actual_request_produces_source_bound_text_sent_without_model_winner(self):
        fallback = self._fit_fixture()
        self.assertTrue(fallback.ready, fallback.reason)
        self.assertEqual(fallback.origin, "source_preference_fallback")
        self.assertEqual(fallback.receipt["failed_request_id"], self.failed_graph.request_id)
        self.assertEqual(fallback.receipt["source_message_ids"], [source.pk for source in self.fit_sources])
        fit_decision = IgCommerceTurnDecision.objects.get(source_message=self.source)
        self.assertTrue(fit_decision.accepted)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.customer.refresh_from_db()
        self.revision.refresh_from_db()
        self.failed_graph.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.assertEqual(self.revision.generation_proposal, {})
        self.assertEqual(self.revision.generation_proposal_digest, "")
        self.assertEqual(self.failed_graph.terminal_resolution, "failed")
        self.assertIsNone(self.failed_graph.winner_attempt_id)
        self.assertFalse(GeminiRequestAttempt.objects.filter(winner_claimed=True).exists())
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "sent")
        self.assertEqual(effect.provider_message_id, "sent-1")
        self.assertEqual((effect.generation_request_id, effect.generation_model), ("", ""))
        text = effect.payload["message"]["text"]
        self.assertIn("оверсайз", text.casefold())
        self.assertIn("принт", text.casefold())
        self.assertNotRegex(text, r"\b(?:XL|XXL)\b|\d+\s*(?:грн|UAH)")
        self.assertEqual(text.count("?"), 1)
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", source="revision_reply").count(), 1)
        self.assertEqual(set(InstagramBotMessage.objects.filter(pk__in=[source.pk for source in self.fit_sources]).values_list("status", flat=True)), {"done"})

    def test_fallback_receipt_replay_and_completed_execution_never_duplicate_send(self):
        first = self._fit_fixture()
        self.assertTrue(first.ready, first.reason)
        replay = self._record()
        self.assertTrue(replay.ready, replay.reason)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt, first.receipt)
        result, _generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)
        again, generate, second_http = self._execute()
        generate.assert_not_called()
        second_http.assert_not_called()
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.delivery_effects.count(), 1)
        self.assertEqual(self.revision.delivery_effects.get().provider_message_id, "sent-1")
        self.assertEqual(InstagramBotMessage.objects.filter(role="model", source="revision_reply").count(), 1)

    def test_pause_after_fallback_admission_prevents_any_physical_send(self):
        fallback = self._fit_fixture()
        self.assertTrue(fallback.ready, fallback.reason)
        type(self.customer).objects.filter(pk=self.customer.pk).update(bot_paused=True)
        denied = self._record()
        self.assertFalse(denied.ready)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked", result.reasons)
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_permission_epoch_change_after_admission_prevents_any_physical_send(self):
        fallback = self._fit_fixture()
        self.assertTrue(fallback.ready, fallback.reason)
        type(self.customer).objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1)
        self.assertFalse(self._record().ready)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked", result.reasons)
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_processed_new_correction_after_admission_prevents_stale_fit_send(self):
        fallback = self._fit_fixture()
        self.assertTrue(fallback.ready, fallback.reason)
        correction = self._message("Не оверсайз, классика", "new-fit-correction")
        # Another consumer marking the new source done is not proof that the
        # old acknowledged preference still answers the current customer.
        InstagramBotMessage.objects.filter(pk=correction.pk).update(status="done", processed_at=timezone.now())
        result, generate, http = self._execute()
        self.assertEqual(result.state, "blocked", result.reasons)
        generate.assert_not_called()
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_media_source_cannot_be_covered_by_text_only_preference_fallback(self):
        fallback = self._fit_fixture(media=True)
        self.assertFalse(fallback.ready)
        self.assertEqual(fallback.reason, "fallback_media_not_supported")
        result, generate, http = self._execute()
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().purpose, "technical_holding")
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_negated_fit_source_never_authorizes_affirmative_fallback(self):
        fallback = self._fit_fixture(fit_text="не хочу оверсайз")
        self.assertTrue(fallback.ready, fallback.reason)
        result, generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.revision.delivery_effects.get().purpose, "normal_reply")
        self.assertIn("Какую посадку", self.revision.delivery_effects.get().payload["message"]["text"])
        self.assertNotIn("оверсайз", self.revision.delivery_effects.get().payload["message"]["text"].casefold())
        self.assertFalse(GeminiRequestAttempt.objects.filter(winner_claimed=True).exists())

    def test_unknown_fallback_send_is_preserved_without_resend_or_second_fallback(self):
        fallback = self._fit_fixture()
        self.assertTrue(fallback.ready, fallback.reason)
        first, generation, http = self._execute([TimeoutError()])
        self.assertEqual(first.state, "delivery_pending", first.reasons)
        generation.assert_not_called()
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "unknown")
        denied = self._record()
        self.assertFalse(denied.ready)
        self.assertEqual(denied.reason, "fallback_lineage_delivery_or_invalid")
        again, generate, retry_http = self._execute()
        self.assertEqual(again.state, "delivery_pending", again.reasons)
        generate.assert_not_called()
        retry_http.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.count(), 1)
        self.assertFalse(InstagramBotMessage.objects.filter(role="model", source="revision_reply").exists())
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "pending")

    def test_partial_sent_unknown_lineage_blocks_fallback_and_blind_successor(self):
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_outbox import plan_revision_effects
        from management.services.ig_turn_revisions import create_refresh_successor

        fallback = self._fit_fixture()
        self.assertTrue(fallback.ready, fallback.reason)
        acknowledgement, separator, question = fallback.receipt["reply_text"].partition(". ")
        self.assertTrue(separator)
        effects = [{"group": "substantive_text", "kind": "text", "payload": {
            "recipient": {"id": self.customer.igsid}, "message": {"text": text},
        }} for text in (acknowledgement + ".", question)]
        authority = fallback.receipt["authority"]
        planned = plan_revision_effects(
            self.revision.pk, self.token, source_message_id=self.source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash),
            authority_context_digest=authority["authority_digest"], effects=effects,
            fact_bindings=authority["fact_bindings"], offer_bindings=authority["offer_bindings"],
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings,
        )
        self.assertEqual(len(planned.effects), 2, planned.reasons)
        result, generate, http = self._execute([
            (200, json.dumps({"message_id": "fit-acknowledgement-sent"})), TimeoutError(),
        ])
        self.assertEqual(result.state, "delivery_pending", result.reasons)
        self.assertEqual(result.sent_parts, 1)
        generate.assert_not_called()
        self.assertEqual(http.call_count, 2)
        self.revision.refresh_from_db()
        self.assertEqual(list(self.revision.delivery_effects.order_by("order_index").values_list("state", flat=True)), ["sent", "unknown"])
        denied = self._record()
        self.assertFalse(denied.ready)
        self.assertEqual(denied.reason, "fallback_lineage_delivery_or_invalid")
        successor = create_refresh_successor(self.revision.pk, self.token, reason="fact_binding_stale")
        self.assertFalse(successor.created)
        self.assertIn(successor.reason, {"delivery_outcome_uncertain", "revision_not_current"})
        self.assertFalse(type(self.revision).objects.filter(parent=self.revision).exists())
        again, regeneration, retry_http = self._execute()
        regeneration.assert_not_called()
        retry_http.assert_not_called()
        self.assertEqual(self.revision.delivery_effects.count(), 2)
