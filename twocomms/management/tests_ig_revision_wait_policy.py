"""Source/timing/receipt policy regressions; no provider or customer transport."""
from datetime import datetime, timedelta, timezone as utc
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import GeminiRequest, IgCustomerTurn, IgCustomerTurnRevision, IgRevisionDeliveryEffect, IgTurnMessage
from management.services.ig_revision_conversation_context import (
    TIMING_POLICY_VERSION, _important_issue, _nonquiet_seconds, conversation_timing_guidance,
    response_delay_apology_context,
)
from management.services.ig_revision_holding import _neutral_current_request, record_technical_holding
from management.services.ig_revision_outbox import _digest
from management.services.ig_revision_recovery import wait_notification_receipts
from management.services.ig_response_debt import (
    park_manual_revision, record_reply_debt, record_response_coverage,
    resolve_delivered_reply_debt, response_coverage,
)
from management.services.ig_turn_revisions import create_collecting_revision


class NonquietPolicyTests(SimpleTestCase):
    def test_resolved_negated_quoted_or_reported_issue_is_not_unresolved(self):
        for text in (
            "Thank you, refund is completed and received money.",
            "Кошти вже повернули, дякую за оплату.",
            "Оплата вже підтверджена, все добре.",
            "Payment is not missing and there are no problems.",
            "Платіж не пропав, немає проблем.",
            "I do not need a refund.",
            "В рекламі написано: оплату не підтверджено.",
            "Мені написали: оплата не підтверджена.",
            'Це цитата: "Оплата не підтверджена".',
            "> Refund not received.\nWhat does this quoted sentence mean?",
            "Спочатку оплату не підтвердили. Тепер оплату підтверджено.",
            "Моє замовлення вже доставлене, дякую.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_important_issue({"text": text}), "")
        for text in ("Refund not received.", "Кошти ще не повернули.", "Оплата не підтверджена."):
            with self.subTest(text=text):
                self.assertEqual(_important_issue({"text": text}), "unresolved_payment")

    def test_overnight_and_dst_are_excluded_in_kyiv_policy(self):
        for start, end in (
            (datetime(2026, 10, 5, 18, tzinfo=utc.utc), datetime(2026, 10, 6, 7, tzinfo=utc.utc)),
            (datetime(2026, 10, 24, 18, tzinfo=utc.utc), datetime(2026, 10, 25, 8, tzinfo=utc.utc)),
        ):
            with self.subTest(start=start):
                self.assertEqual(_nonquiet_seconds(start, end), 0)

    def test_dst_day_uses_actual_ordinary_window(self):
        self.assertEqual(_nonquiet_seconds(
            datetime(2026, 10, 25, 0, tzinfo=utc.utc),
            datetime(2026, 10, 25, 23, tzinfo=utc.utc)), 10.5 * 3600)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionWaitPolicyTests(TransactionTestCase):
    reset_sequences = True
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute

    def _issue(self, text="Оплата не підтверджена", *, start=None):
        self.source.text = text
        self.source.provider_created_at = start or datetime(2026, 10, 5, 7, tzinfo=utc.utc)
        self.source.save(update_fields=["text", "provider_created_at"])
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()

    def _effect(self, *, state="sent", text="Передала питання команді.", purpose="technical_holding", group="technical_holding", recipient=None, part_count=1):
        payload = {"message": {"text": text}}
        return IgRevisionDeliveryEffect.objects.create(
            revision=self.revision, source_message=self.source, effect_key=f"wait-policy:{self.revision.pk}",
            actor="bot", purpose=purpose, group=group, kind="text", order_index=0, part_index=0,
            part_count=part_count, plan_digest="a" * 64, payload=payload, payload_digest=_digest(payload),
            recipient_igsid=recipient or self.customer.igsid, provider_namespace="instagram_login:owner-1",
            settings_id_snapshot=1, settings_permission_epoch=self.settings.reply_permission_epoch,
            client_permission_epoch=self.customer.reply_permission_epoch,
            revision_snapshot_digest=self.revision.snapshot_digest, publication_id=self.publication.pk,
            publication_version=self.publication.version, publication_hash=self.publication.snapshot_hash,
            authority_context_digest="a" * 64, state=state, provider_message_id="receipt" if state == "sent" else "",
            terminal_at=timezone.now() if state == "sent" else None,
        )

    def _related(self, text="Оплата досі не підтверджена", *, linked=True):
        original_mid = self.source.mid
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(active_slot=None)
        self.source = self._message(text, f"related-{self.revision.pk}")
        self.source.provider_created_at = datetime(2026, 10, 5, 12, tzinfo=utc.utc)
        self.source.reply_to_provider_message_id = original_mid if linked else ""
        self.source.save(update_fields=["provider_created_at", "reply_to_provider_message_id"])
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()

    def test_threshold_30min_2h_5h_6h_and_20h_requires_nonquiet_time(self):
        self._issue()
        for hours, eligible in ((0.5, False), (2, False), (5, False), (6, True), (20, True)):
            with self.subTest(hours=hours):
                facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 7, tzinfo=utc.utc) + timedelta(hours=hours))
                self.assertEqual(facts["delay_apology_eligible"], eligible)
                self.assertEqual(facts["issue_source_message_id"], self.source.pk)
                self.assertEqual(facts["timing_policy_version"], TIMING_POLICY_VERSION)

    def test_repeat_keeps_original_important_source_age(self):
        self._issue()
        original_id = self.source.pk
        self._related()
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertTrue(facts["delay_apology_eligible"])
        self.assertEqual(facts["issue_source_message_id"], original_id)

    def test_unlinked_new_payment_does_not_borrow_age_from_old_issue(self):
        self._issue()
        self._related("Новий платіж не підтверджений", linked=False)
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertEqual(facts["reason"], "below_nonquiet_threshold")
        self.assertEqual(facts["issue_source_message_id"], self.source.pk)

    def test_reset_boundary_excludes_old_notification(self):
        self._issue()
        self._effect()
        self._related()
        with patch("management.services.ig_conversation_routes.conversation_route_reset_floor", return_value=self.source.pk):
            self.assertFalse(any(item["notification"] for item in wait_notification_receipts(self.revision)))

    def test_foreign_recipient_is_not_owned_notification_proof(self):
        self._issue()
        self._effect(recipient="different-customer")
        self._related()
        self.assertFalse(any(item["notification"] for item in wait_notification_receipts(self.revision)))

    def test_permission_epoch_rotation_does_not_forget_prior_sent_notice(self):
        self._issue()
        self._effect()
        self.customer.reply_permission_epoch += 1
        self.customer.save(update_fields=["reply_permission_epoch"])
        self._related()
        self.assertTrue(any(item["notification"] for item in wait_notification_receipts(self.revision)))

    def test_current_owner_pause_does_not_grant_apology_permission(self):
        self._issue()
        self.customer.manager_takeover = True
        facts = response_delay_apology_context(self.revision)
        self.assertEqual(facts["reason"], "issue_owner_paused")

    def test_current_vacancy_manager_and_complaint_are_not_important_issue(self):
        self._issue("Чекаю на відповідь щодо вакансії від менеджера")
        self.assertFalse(response_delay_apology_context(self.revision)["delay_apology_eligible"])
        self.assertFalse(_neutral_current_request(self.customer, self.revision))

    def test_recovery_flag_alone_is_not_eligibility(self):
        self._issue("Почему вы не отвечаете?")
        self.assertFalse(response_delay_apology_context(self.revision)["delay_apology_eligible"])

    def test_pending_completed_refund_does_not_create_six_hour_unresolved_issue(self):
        self._issue("Thank you, refund is completed and received money.")
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertEqual(facts["reason"], "important_source_unknown")
        self.assertFalse(facts["delay_apology_eligible"])

    def test_pending_ukrainian_completed_refund_does_not_create_unresolved_issue(self):
        self._issue("Кошти вже повернули, дякую за оплату.")
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertEqual(facts["reason"], "important_source_unknown")
        self.assertFalse(facts["delay_apology_eligible"])

    def test_unknown_timestamp_and_policy_omit_apology(self):
        self._issue()
        with override_settings(IG_REVISION_DELAY_APOLOGY_TIMING_POLICY="contradictory"):
            self.assertEqual(response_delay_apology_context(self.revision)["reason"], "timing_policy_unknown")
        with patch("management.services.ig_revision_conversation_context.parse_datetime", return_value=None):
            self.assertEqual(response_delay_apology_context(self.revision)["reason"], "issue_timestamp_unknown")

    def test_threshold_is_configurable_and_useful_answer_never_requires_apology(self):
        self._issue()
        now = datetime(2026, 10, 5, 12, tzinfo=utc.utc)
        with override_settings(IG_REVISION_DELAY_APOLOGY_NONQUIET_HOURS=5):
            self.assertTrue(response_delay_apology_context(self.revision, now=now)["delay_apology_eligible"])
        with patch("management.services.ig_revision_conversation_context.timezone.now", return_value=now):
            guidance = conversation_timing_guidance(self.revision)
        self.assertIn('"delay_apology_eligible":false', guidance)
        self.assertIn("never requires one", guidance)

    def test_sent_and_unknown_apology_suppress_across_related_inbound(self):
        self._issue()
        effect = self._effect(state="unknown", text="Извините за долгое ожидание. Перевірмо оплату.")
        self._related()
        for state in ("unknown", "sent"):
            effect.state = state
            effect.save(update_fields=["state"])
            with self.subTest(state=state):
                facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
                self.assertEqual(facts["reason"], "already_apologized_for_wait")
                self.assertTrue(any(item["notification"] for item in wait_notification_receipts(self.revision)))

    def test_nonleading_sent_unknown_delay_apology_suppresses_in_all_languages(self):
        self._issue()
        self._effect(state="unknown", text="Перевірмо оплату. Вибачте за довге очікування.")
        self._related()
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertEqual(facts["reason"], "already_apologized_for_wait")
        from management.services.ig_revision_conversation_context import _DELIVERED_DELAY_APOLOGY
        for text in ("Перевірмо оплату. Вибачте за довге очікування.",
                     "Проверим оплату. Извините за долгое ожидание.",
                     "Let's check the payment. Sorry for the long wait."):
            self.assertIsNotNone(_DELIVERED_DELAY_APOLOGY.search(text))
        self.assertIsNone(_DELIVERED_DELAY_APOLOGY.search("Перевірмо оплату. Вибачте за помилку з товаром."))

    def _assert_prior_sentence_apology_suppression(self, text):
        self._issue()
        effect = self._effect(state="unknown", text="Перевірмо оплату. " + text)
        self._related()
        for state in ("unknown", "sent"):
            effect.state = state
            effect.save(update_fields=["state"])
            facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
            self.assertEqual(facts["reason"], "already_apologized_for_wait")

    def test_ukrainian_sentence_apology_is_suppressed_after_sent_unknown(self):
        self._assert_prior_sentence_apology_suppression("Вибачте, що вам довелося довго чекати на відповідь.")

    def test_russian_sentence_apology_is_suppressed_after_sent_unknown(self):
        self._assert_prior_sentence_apology_suppression("Простите, что так долго отвечали.")

    def test_english_sentence_apology_is_suppressed_after_sent_unknown(self):
        self._assert_prior_sentence_apology_suppression("Sorry to have kept you waiting.")

    def test_whole_generation_boundary_removes_ineligible_nonleading_apology(self):
        self._issue("Хочу замовити худі")
        clean = "Яку модель худі ви хочете замовити?"
        self.parsed = {"reply_text": clean + " Вибачте за довге очікування.", "controls": []}
        _result, generate, send = self._execute()
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(send.call_count, 1)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.generation_proposal["response"]["reply_text"], clean)
        self.assertEqual(self.revision.delivery_effects.get().payload["message"]["text"], clean)

    def test_whole_guard_rejects_apology_only_and_keeps_genuine_error(self):
        from dataclasses import replace
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_guard import ProviderResponseGuard
        from management.services.ig_revision_conversation_context import normalize_response_delay_apology

        self._issue("Хочу замовити худі")
        guard = ProviderResponseGuard(context_factory=lambda _control, _reply: ReplyTruthContext(),
            response_normalizer=lambda response: replace(response,
                reply_text=normalize_response_delay_apology(response.reply_text, self.revision)))
        self.assertFalse(guard.validate({"reply_text": "Sorry to have kept you waiting.", "controls": []}).valid)
        error = "Вибачте за помилку з товаром. Підкажіть номер замовлення."
        self.assertTrue(guard.validate({"reply_text": error, "controls": []}).valid)
        self.assertEqual(guard.response.reply_text, error)

    def test_whole_guard_removes_repeat_apology_after_useful_answer(self):
        from dataclasses import replace
        from management.services.ig_reply_truth import ReplyTruthContext
        from management.services.ig_response_guard import ProviderResponseGuard
        from management.services.ig_revision_conversation_context import normalize_response_delay_apology

        self._issue()
        self._effect(state="unknown", text="Перевірмо оплату. Вибачте за довге очікування.")
        self._related()
        guard = ProviderResponseGuard(context_factory=lambda _control, _reply: ReplyTruthContext(),
            response_normalizer=lambda response: replace(response,
                reply_text=normalize_response_delay_apology(response.reply_text, self.revision)))
        clean = "Яку модель ви хочете замовити?"
        self.assertTrue(guard.validate({"reply_text": clean + " Простите, что так долго отвечали.", "controls": []}).valid)
        self.assertEqual(guard.response.reply_text, clean)

    def test_neutral_ack_does_not_forbid_one_later_appropriate_apology(self):
        self._issue()
        self._effect()
        self._related()
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertTrue(facts["delay_apology_eligible"])
        self.assertIsNotNone(facts["prior_notification_receipt"])

    def test_new_answer_boundary_does_not_inherit_old_apology(self):
        self._issue()
        self._effect(text="Извините за долгое ожидание. Перевірмо оплату.", purpose="normal_reply", group="substantive_text")
        self._related()
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertEqual(facts["reason"], "below_nonquiet_threshold")
        self.assertIsNone(facts["prior_apology_receipt"])

    def test_unrelated_complete_answer_does_not_close_important_wait(self):
        self._issue()
        original_source_id, original_mid = self.source.pk, self.source.mid
        self._effect(text="Извините за долгое ожидание. Перевірмо оплату.")
        self._related("Розмір L", linked=False)
        self._effect(text="Ви обрали L.", purpose="normal_reply", group="substantive_text")
        IgCustomerTurnRevision.objects.filter(pk=self.revision.pk).update(active_slot=None)
        self.source = self._message("Оплата досі не підтверджена", "repeat-important-a")
        self.source.provider_created_at = datetime(2026, 10, 5, 12, tzinfo=utc.utc)
        self.source.reply_to_provider_message_id = original_mid
        self.source.save(update_fields=["provider_created_at", "reply_to_provider_message_id"])
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        IgTurnMessage.objects.create(turn=self.turn, message=self.source, ordinal=1, role="user")
        self.revision = create_collecting_revision(self.turn, [self.source], bypass_quiet=True).revision
        self._prepare()
        facts = response_delay_apology_context(self.revision, now=datetime(2026, 10, 5, 13, tzinfo=utc.utc))
        self.assertEqual(facts["issue_source_message_id"], original_source_id)
        self.assertEqual(facts["nonquiet_elapsed_seconds"], 6 * 3600)
        self.assertEqual(facts["reason"], "already_apologized_for_wait")
        self.assertTrue(any(item["notification"] for item in wait_notification_receipts(self.revision)))

    def test_neutral_branch_rejects_substantive_ambiguous_question(self):
        self._issue("Що ви можете порадити?")
        self.assertFalse(_neutral_current_request(self.customer, self.revision))

    def test_true_no_action_ack_is_admissible(self):
        self._issue("Дякую!")
        self.assertTrue(_neutral_current_request(self.customer, self.revision))

    def _failed_graph(self):
        GeminiRequest.objects.create(request_id=f"wait-policy-{self.revision.pk}",
            logical_turn_id=f"ig-revision:{self.revision.pk}", client_id=self.customer.pk,
            lane="live", task_class="simple_live", terminal_resolution="failed",
            accounting_mode="shadow")

    def test_recorded_no_action_ack_has_no_future_promise(self):
        self._issue("Дякую!")
        self._failed_graph()
        decision = record_technical_holding(self.revision.pk, self.token, settings_id=1, allow_neutral=True)
        self.assertTrue(decision.ready, decision.reason)
        self.assertEqual(decision.receipt["reply_text"], "Дякую за повідомлення.")
        self.assertEqual(decision.receipt["substantive_obligation"], "none")

    def test_unknown_notification_blocks_second_admission_on_related_root(self):
        self._issue()
        self._effect(state="unknown")
        self._related("Хочу замовити худі")
        self._failed_graph()
        decision = record_technical_holding(self.revision.pk, self.token, settings_id=1, allow_neutral=True)
        self.assertFalse(decision.ready)
        self.assertEqual(decision.reason, "holding_continuous_wait_already_notified")

    def test_partial_coverage_persists_and_cannot_settle_reply_debt(self):
        self._issue()
        coverage = {"covered": [f"{self.source.pk}:size"], "remaining": [f"{self.source.pk}:purchase_requested"],
                    "disposition": "recovery", "plan_digest": "b" * 64, "next_selector": "option:sleeve"}
        self.assertFalse(record_response_coverage(self.revision.pk, "wrong-token", coverage))
        self.assertTrue(record_response_coverage(self.revision.pk, self.token, coverage))
        self.revision.refresh_from_db()
        self.assertEqual(response_coverage(self.revision)["remaining"], coverage["remaining"])
        self.assertEqual(response_coverage(self.revision)["plan_digest"], "b" * 64)
        self.assertEqual(response_coverage(self.revision)["next_selector"], "option:sleeve")
        task = record_reply_debt(self.revision, "semantic_reply_incomplete")
        self.assertEqual(resolve_delivered_reply_debt(self.revision), 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")

    def test_missing_selector_wait_has_customer_owner_and_no_manager_alert(self):
        self._issue()
        self.assertTrue(record_response_coverage(self.revision.pk, self.token,
            {"covered": [], "remaining": ["purchase"], "disposition": "waiting_on_customer", "next_selector": "product"}))
        self._effect(text="Яку модель ви хочете замовити?", purpose="normal_reply")
        self.revision.refresh_from_db()
        self.assertIsNone(park_manual_revision(self.revision, "semantic_reply_incomplete"))
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts["response_debt"]["owner"], "customer")
        self.assertIsNone(self.revision.recovery_due_at)
        self.assertEqual(response_delay_apology_context(self.revision)["reason"], "waiting_on_customer")

    def test_full_sent_unrelated_question_cannot_grant_customer_wait_owner(self):
        self._issue("Хочу замовити худі")
        self.assertTrue(record_response_coverage(self.revision.pk, self.token,
            {"covered": [], "remaining": ["purchase"], "disposition": "waiting_on_customer", "next_selector": "product"}))
        self._effect(text="Модель обрана. Вам зручно завтра?", purpose="normal_reply")
        self.revision.refresh_from_db()
        task = park_manual_revision(self.revision, "semantic_reply_incomplete")
        self.assertIsNotNone(task)
        self.assertEqual(self.revision.action_receipts["response_debt"]["owner"], "manager")
        self.assertEqual(task.manager_context["disposition"], "manager_reply_uncovered")

    def test_changed_source_cannot_grant_customer_wait_despite_sent_question(self):
        self._issue("Хочу замовити худі")
        self.assertTrue(record_response_coverage(self.revision.pk, self.token,
            {"covered": [], "remaining": ["purchase"], "disposition": "waiting_on_customer", "next_selector": "product"}))
        self._effect(text="Яку модель ви хочете замовити?", purpose="normal_reply")
        self.source.text = "Потреба змінилася"
        self.source.save(update_fields=["text"])
        self.revision.refresh_from_db()
        task = park_manual_revision(self.revision, "semantic_reply_incomplete")
        self.assertIsNotNone(task)
        self.assertEqual(self.revision.action_receipts["response_debt"]["owner"], "manager")

    def test_failed_unknown_selector_question_keeps_manager_owner(self):
        self._issue()
        self.assertTrue(record_response_coverage(self.revision.pk, self.token,
            {"covered": [], "remaining": ["purchase"], "disposition": "waiting_on_customer", "next_selector": "product"}))
        effect = self._effect(state="unknown", text="Яку модель ви хочете замовити?", purpose="normal_reply")
        self.revision.refresh_from_db()
        for state in ("unknown", "definite_failed", "cancelled"):
            effect.state = state
            effect.save(update_fields=["state"])
            task = park_manual_revision(self.revision, "semantic_reply_incomplete")
            self.assertIsNotNone(task)
            self.assertEqual(self.revision.action_receipts["response_debt"]["owner"], "manager")

    def test_partially_sent_selector_question_keeps_manager_owner(self):
        self._issue()
        self.assertTrue(record_response_coverage(self.revision.pk, self.token,
            {"covered": [], "remaining": ["purchase"], "disposition": "waiting_on_customer", "next_selector": "product"}))
        self._effect(text="Яку модель ви хочете замовити?", purpose="normal_reply", part_count=2)
        self.revision.refresh_from_db()
        task = park_manual_revision(self.revision, "semantic_reply_incomplete")
        self.assertIsNotNone(task)
        self.assertEqual(self.revision.action_receipts["response_debt"]["owner"], "manager")

    def test_sent_partial_response_has_manager_reply_not_delivery_reconciliation(self):
        self._issue()
        self._effect()
        task = park_manual_revision(self.revision, "semantic_reply_incomplete")
        self.assertEqual(task.manager_context["disposition"], "manager_reply_uncovered")
