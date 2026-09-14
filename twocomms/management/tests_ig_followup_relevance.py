from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    BotPolicyPublication, IgClient, IgCommercialEpisode, IgConversationRouteDecision, IgCustomerTurn,
    IgCustomerTurnRevision, IgDeal, IgFollowUpTask, IgRevisionDeliveryEffect, IgTurnRevisionSource,
    InstagramBotMessage, InstagramBotSettings,
)
from management.services import bot_followups as policy
from management.services.ig_revision_followups import _schedule
from management.services.ig_turn_intent import (
    build_turn_intent, ordinary_next_send_at, purpose_blockers,
    revalidate_followup_intent, validate_turn_response,
)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class FollowupRelevanceTests(TransactionTestCase):
    def setUp(self):
        self.client_row = IgClient.objects.create(igsid="purpose-test")
        self.now = datetime(2026, 9, 14, 10, tzinfo=dt_timezone.utc)

    def message(self, text, **kwargs):
        return InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid, role="user", text=text, **kwargs)

    def revision(self, messages):
        sources = SimpleNamespace(select_related=lambda *args: SimpleNamespace(order_by=lambda *args: [SimpleNamespace(message=row) for row in messages]))
        return SimpleNamespace(pk=123, sources=sources, snapshot_digest="a"*64)

    def schedule(self, message, *, sent_at=None, revision=None):
        row = SimpleNamespace(pk=45, group="substantive_text", terminal_at=sent_at or self.now)
        revision = revision or self.revision([message])
        with transaction.atomic(), patch.object(policy, "_client_allows_followup", return_value=(True, "")), patch.object(policy, "_update_client_next"):
            return _schedule(self.client_row, revision, [row], self.now, self.now)

    def test_radio_share_has_no_sales_even_with_old_product_context(self):
        old = self.message("Яка ціна?")
        media = self.message("")
        # A fresh accepted community decision outranks old retail history.
        journal = SimpleNamespace(pk=7, revision_id=123, watermark_message_id=media.pk, interpretation={"intents":[{"kind":"community", "operation":"open", "evidence_message_ids":[media.pk]}]}, active_intents=[{"kind":"catalog"}])
        with patch("management.services.ig_turn_intent.IgConversationRouteDecision.objects") as manager:
            manager.filter.return_value.order_by.return_value.first.return_value = journal
            decision = build_turn_intent(self.client_row, self.revision([media]))
            task, reason = self.schedule(media)
        self.assertEqual(decision["purpose"], "community")
        self.assertFalse(decision["commerce_evidence_refs"])
        self.assertEqual(validate_turn_response(decision, "Хочете замовити футболку?"), "current_purpose_disallows_sales")
        self.assertIsNone(task)
        self.assertEqual(reason, "current_purpose_not_followup_eligible")
        self.assertFalse(IgFollowUpTask.objects.exists())

    def test_price_uses_sent_answer_three_hours_and_one_durable_cycle(self):
        message = self.message("Яка ціна?")
        InstagramBotMessage.objects.filter(pk=message.pk).update(provider_created_at=self.now-timedelta(hours=1))
        message.refresh_from_db()
        task, reason = self.schedule(message)
        self.assertEqual(reason, "normal_followup_scheduled")
        self.assertEqual(task.policy_started_at, self.now)
        self.assertEqual(task.due_at, self.now+timedelta(hours=3))
        self.assertEqual(task.meta_window_deadline, self.now+timedelta(hours=22))
        task.status = "sent"
        task.save(update_fields=["status"])
        again, reason = self.schedule(message, sent_at=self.now+timedelta(minutes=2))
        self.assertEqual(again.pk, task.pk)
        self.assertEqual(reason, "ordinary_cycle_already_reserved")
        self.assertEqual(IgFollowUpTask.objects.count(), 1)
        self.assertFalse(policy._schedule_next_policy_step(task, self.client_row, now=self.now))
        self.assertTrue(policy._complete_policy_after_send(task, self.client_row))
        self.client_row.refresh_from_db()
        self.assertFalse(self.client_row.lost_reason)
        text = policy.compose_followup(task, now=self.now)
        self.assertNotIn("онлайн", text)
        self.assertNotIn("замовлення", text)

    def test_missing_size_is_not_selection_permission(self):
        message = self.message("Привіт!")
        task, reason = self.schedule(message)
        self.assertIsNone(task)
        self.assertEqual(reason, "current_purpose_not_followup_eligible")
        selection = self.message("Допоможіть підібрати розмір")
        task, _ = self.schedule(selection)
        self.assertEqual(task.due_at, self.now+timedelta(minutes=90))

    def test_quiet_hours_do_not_use_emergency_slot(self):
        # 20:30 Kyiv means the optional contact waits for the next 10:00.
        late = datetime(2026, 9, 14, 17, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(ordinary_next_send_at(late), datetime(2026, 9, 15, 7, tzinfo=dt_timezone.utc))

    def test_unknown_cycle_and_legacy_ladder_are_not_replayed(self):
        message = self.message("Яка ціна?")
        task, _ = self.schedule(message)
        task.status = "unknown"
        task.save(update_fields=["status"])
        again, reason = self.schedule(message)
        self.assertEqual(again.pk, task.pk)
        self.assertEqual(reason, "ordinary_cycle_already_reserved")
        legacy = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now, reason="first_reply_silence", kind="qualification")
        self.assertEqual(revalidate_followup_intent(legacy, self.now), "legacy_ordinary_followup_unbound")

    def test_true_payment_prize_case_is_preserved_and_blocks(self):
        message = self.message("Яка ціна?")
        case = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now, reason="prize_review:one", kind="manager_task", status="skipped", manager_approval_status="pending")
        task, reason = self.schedule(message)
        self.assertIsNone(task)
        self.assertEqual(reason, "pending_manager_case")
        case.refresh_from_db()
        self.assertEqual(case.status, "skipped")

    def test_exact_earlier_technical_debt_does_not_block_independent_price(self):
        old_message = self.message("Привіт!")
        price = self.message("Яка ціна?")
        case = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now, reason="revision_case:execution_debt", kind="manager_task", status="skipped", manager_approval_status="pending", event_payload={"revision_id": 90, "source_message_ids": [old_message.pk]})
        old = SimpleNamespace(pk=90, action_receipts={}, sources=SimpleNamespace(select_related=lambda *args: SimpleNamespace(order_by=lambda *args: [SimpleNamespace(message_id=old_message.pk, message=old_message)])), delivery_effects=SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(exists=lambda: False)))
        with patch("management.services.ig_turn_intent.IgCustomerTurnRevision.objects") as manager:
            manager.filter.return_value.first.return_value = old
            task, reason = self.schedule(price)
        self.assertEqual(reason, "normal_followup_scheduled")
        self.assertIsNotNone(task)
        case.refresh_from_db()
        self.assertEqual(case.status, "skipped")
        # An ambiguous receipt from the same old source still blocks.
        old.delivery_effects = SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(exists=lambda: True))
        with patch("management.services.ig_turn_intent.IgCustomerTurnRevision.objects") as manager:
            manager.filter.return_value.first.return_value = old
            self.assertEqual(purpose_blockers(self.client_row, build_turn_intent(self.client_row, self.revision([price])), revision=self.revision([price])), "pending_manager_case")

    def test_new_meaningful_purchase_opens_cycle_and_thanks_does_not(self):
        initial = self.message("Яка ціна?")
        first = build_turn_intent(self.client_row, self.revision([initial]))
        thanks = self.message("Дякую!")
        quiet = build_turn_intent(self.client_row, self.revision([thanks]))
        self.assertFalse(quiet["cycle_key"])
        new = self.message("Яка ціна на іншу футболку?")
        fresh = build_turn_intent(self.client_row, self.revision([new]))
        self.assertNotEqual(first["cycle_key"], fresh["cycle_key"])

    def test_prior_real_unanswered_question_is_not_hidden_by_later_price(self):
        old_message = self.message("Коли відправите моє замовлення?")
        price = self.message("Яка ціна?")
        IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now, reason="revision_case:execution_debt", kind="manager_task", status="skipped", event_payload={"revision_id": 90, "source_message_ids": [old_message.pk]})
        old = SimpleNamespace(pk=90, action_receipts={}, sources=SimpleNamespace(select_related=lambda *args: SimpleNamespace(order_by=lambda *args: [SimpleNamespace(message_id=old_message.pk, message=old_message)])), delivery_effects=SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(exists=lambda: False)))
        with patch("management.services.ig_turn_intent.IgCustomerTurnRevision.objects") as manager:
            manager.filter.return_value.first.return_value = old
            task, reason = self.schedule(price)
        self.assertIsNone(task)
        self.assertEqual(reason, "pending_manager_case")

    def test_new_image_after_answer_does_not_inherit_old_price_interest(self):
        prior = self.message("Яка ціна?", provider_created_at=self.now-timedelta(hours=5))
        InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid, role="model", text="Вартість — 1090 грн.")
        photo = self.message("", provider_created_at=self.now)
        decision = build_turn_intent(self.client_row, self.revision([photo]))
        self.assertFalse(decision["commerce_evidence_refs"])
        self.assertEqual(decision["purpose"], "unknown")

    def test_requested_screenshot_carries_actual_unresolved_price_question(self):
        prior = self.message("Яка ціна?", provider_created_at=self.now-timedelta(minutes=10))
        InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid, role="model", text="Надішліть модель або скрин, будь ласка.")
        photo = self.message("", provider_created_at=self.now)
        decision = build_turn_intent(self.client_row, self.revision([photo]))
        self.assertEqual(decision["purpose"], "price_inquiry")
        self.assertEqual(decision["commerce_evidence_refs"], [prior.pk])

    def durable_revision(self, message, kind=None, *, episode_id=None):
        from management.services.ig_revision_outbox import _digest
        turn = IgCustomerTurn.objects.create(client=self.client_row, primary_source_message=message,
            window_started_at=self.now, window_deadline=self.now)
        snapshot = {"sources": [{"message_id": message.pk, "role": "user", "text": message.text}]}
        revision = IgCustomerTurnRevision.objects.create(client=self.client_row, turn=turn,
            revision=IgCustomerTurnRevision.objects.filter(client=self.client_row).count()+1,
            active_slot=None, quiet_started_at=self.now, quiet_deadline=self.now,
            quiet_cap_at=self.now, overall_deadline=self.now + timedelta(minutes=30),
            bundle_snapshot=snapshot, snapshot_digest=_digest(snapshot))
        IgTurnRevisionSource.objects.create(revision=revision, message=message, ordinal=1,
            role="user", text=message.text, source_digest=_digest(snapshot["sources"][0]))
        if kind:
            number = IgConversationRouteDecision.objects.count() + 1
            IgConversationRouteDecision.objects.create(client=self.client_row, revision=revision,
                decision_key=str(number).zfill(64), sequence=number, reset_floor=1,
                watermark_message_id=message.pk, input_digest=revision.snapshot_digest,
                interpretation_digest="b"*64, decision_digest="c"*64,
                source_binding={"source_refs": {"commercial_episode_id": episode_id}},
                interpretation={"intents": [{"kind": kind, "operation": "open", "evidence_message_ids": [message.pk]}]},
                active_intents=[{"key": f"{kind}:none", "kind": kind, "subtype": "none"}],
                occurred_at=self.now)
        return revision

    def test_owned_question_survives_url_and_isolated_quote(self):
        for text in ("Яка ціна? https://example.test/shirt", '«Купуйте нашу рекламу» А яка ціна футболки?',
                     '> Яка ціна реклами?\nА яка ціна футболки?'):
            with self.subTest(text=text):
                row = self.message(text)
                decision = build_turn_intent(self.client_row, self.revision([row]))
                self.assertEqual(decision["purpose"], "price_inquiry")
                self.assertEqual(decision["commerce_evidence_refs"], [row.pk])
        for text in ('«Яка ціна?»', '"Хочу купити футболку"', '> Яка ціна? https://example.test', 'https://example.test/shirt'):
            with self.subTest(text=text):
                row = self.message(text)
                self.assertFalse(build_turn_intent(self.client_row, self.revision([row]))["commerce_evidence_refs"])

    def test_nonretail_handoff_and_optout_do_not_get_rejected_as_sales(self):
        decision = {"allowed_response_acts": ["answer_current_question"]}
        for action in ("manager_escalation_intent", "spam_transition", "prize_review_case_create", "order_fulfillment"):
            self.assertEqual(validate_turn_response(decision, "Передам ваше питання команді.", [action]), "")
        self.assertEqual(validate_turn_response(decision, "Добре", ["checkout_proposal_create"]), "current_purpose_disallows_sales")
        self.assertEqual(validate_turn_response(decision, "Хочете замовити футболку?", ["manager_escalation_intent"]), "current_purpose_disallows_sales")
        support = {**decision, "purpose": "support"}
        self.assertEqual(validate_turn_response(support, "Могу помочь с доставкой.", ["order_fulfillment"]), "")
        self.assertEqual(validate_turn_response(support, "Хотите купить футболку?"), "current_purpose_disallows_sales")

    def test_explicit_customer_order_requests_grant_retail_discussion(self):
        for text in (
            "Беру. Оформлюйте замовлення.", "Оформіть, будь ласка, замовлення.", "Замовляю футболку.",
            "Беру. Оформите заказ.", "Оформите, пожалуйста, заказ.", "Заказываю футболку.",
            "Please place my order.", "I'd like to order this shirt.", "Complete my purchase, please.",
            '«Не оформлюйте замовлення» — це старе повідомлення. Оформлюйте замовлення.',
            "'I don't want to order' was my earlier reply. Please place my order.",
        ):
            with self.subTest(text=text):
                row = self.message(text)
                decision = build_turn_intent(self.client_row, self.revision([row]))
                self.assertEqual(decision["purpose"], "retail")
                self.assertEqual(decision["commerce_evidence_refs"], [row.pk])
                self.assertEqual(validate_turn_response(decision, "Перевірю оформлення.", ["checkout_proposal_create"]), "")

    def test_negated_and_quoted_ordering_never_grants_sales_acts(self):
        for text in (
            "Не хочу замовляти.", "Не оформлюйте замовлення.", "Не хочу зараз купувати футболку.",
            "Не хочу заказывать.", "Не хочу покупать футболку.", "Не оформляйте заказ.", "Не хочу вообще ничего заказывать.",
            "I don't want to order.", "Please do not place my order.", "I do not want an order.",
            "I wouldn't like to order.", "I am not ready to buy.",
            '«Беру. Оформлюйте замовлення.»', '"Беру. Оформите заказ."',
            "'Please place my order.'", "'I'd like to order this shirt.'", "> Please place my order.",
        ):
            with self.subTest(text=text):
                row = self.message(text)
                decision = build_turn_intent(self.client_row, self.revision([row]))
                self.assertFalse(decision["commerce_evidence_refs"])
                self.assertNotIn("retail_consultation", decision["allowed_response_acts"])
                self.assertEqual(validate_turn_response(decision, "Оформимо замовлення?", ["checkout_proposal_create"]), "current_purpose_disallows_sales")

    def test_later_refusal_overrides_bundle_and_catalog_topic_without_erasing_interest(self):
        first = self.message("Хочу замовити футболку")
        refusal = self.message("Не хочу заказывать")
        revision = self.durable_revision(refusal, "catalog")
        decision = build_turn_intent(self.client_row, revision)
        self.assertEqual(decision["purpose"], "purchase_refusal")
        self.assertFalse(decision["commerce_evidence_refs"])
        self.assertEqual(decision["standing_interest"][0]["kind"], "catalog")
        bundled = build_turn_intent(self.client_row, self.revision([first, refusal]))
        self.assertFalse(bundled["commerce_evidence_refs"])
        self.assertNotIn("retail_consultation", bundled["allowed_response_acts"])
        renewed = self.message("Беру. Оформлюйте замовлення.")
        renewed_decision = build_turn_intent(self.client_row, self.revision([refusal, renewed]))
        self.assertEqual(renewed_decision["commerce_evidence_refs"], [renewed.pk])

    def test_fit_question_and_bot_order_copy_do_not_create_customer_order_intent(self):
        prior = self.message("Яка ціна?", provider_created_at=self.now)
        self.durable_revision(prior, "catalog")
        InstagramBotMessage.objects.create(client=self.client_row, sender_id=self.client_row.igsid,
            role="model", text="Please place my order.")
        row = self.message("Оверсайз?", provider_created_at=self.now + timedelta(minutes=1))
        decision = build_turn_intent(self.client_row, self.revision([row]))
        self.assertFalse(decision["commerce_evidence_refs"])
        self.assertNotIn("retail_consultation", decision["allowed_response_acts"])

    def test_proven_disjoint_service_debt_allows_catalog_but_is_preserved(self):
        old_message = self.message("Коли відправите моє замовлення?")
        old = self.durable_revision(old_message, "support")
        price = self.message("Яка ціна іншої футболки?")
        current = self.durable_revision(price, "catalog")
        case = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            reason="revision_case:execution_debt", kind="manager_task", status="skipped",
            event_payload={"revision_id": old.pk, "source_message_ids": [old_message.pk]})
        decision = build_turn_intent(self.client_row, current)
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=current), "")
        case.refresh_from_db()
        self.assertEqual(case.status, "skipped")
        # No accepted scope on the new question restores conservative blocking.
        unclassified = self.message("Яка ціна?")
        unclassified_revision = self.durable_revision(unclassified)
        self.assertEqual(purpose_blockers(self.client_row, build_turn_intent(self.client_row, unclassified_revision), revision=unclassified_revision), "pending_manager_case")

    def test_prize_case_requires_proven_separate_episode(self):
        old_episode = IgCommercialEpisode.objects.create(client=self.client_row, sequence=1, open_slot=None, materialization_key="prize-old")
        new_episode = IgCommercialEpisode.objects.create(client=self.client_row, sequence=2, materialization_key="prize-new")
        old_message = self.message("Це мій призовий сертифікат")
        self.durable_revision(old_message, "catalog", episode_id=old_episode.pk)
        price = self.message("Яка ціна іншої футболки для нового замовлення?")
        current = self.durable_revision(price, "catalog", episode_id=new_episode.pk)
        case = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            reason="prize_review:one", kind="manager_task", status="skipped",
            event_payload={"schema_version": "ig-prize-case-v1", "case_kind": "prize_review",
                           "initial_source_message_id": old_message.pk, "programme_id": "one", "programme_version": 1},
            manager_context={"schema_version": "ig-prize-case-v1", "programme_id": "one", "programme_version": 1,
                             "evidence": [{"source_message_id": old_message.pk}]})
        decision = build_turn_intent(self.client_row, current)
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=current), "")
        decision["source_scope"]["commercial_episode_id"] = old_episode.pk
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=current), "pending_manager_case")
        case.refresh_from_db()
        self.assertEqual(case.status, "skipped")

    def test_shipping_and_payment_review_stay_scoped_to_their_purchase(self):
        old_deal = IgDeal.objects.create(client=self.client_row, payment_truth="pending")
        old_episode = IgCommercialEpisode.objects.create(client=self.client_row, deal=old_deal,
            sequence=1, open_slot=None, materialization_key="shipping-old")
        new_episode = IgCommercialEpisode.objects.create(client=self.client_row, sequence=2, materialization_key="shipping-new")
        price = self.message("Яка ціна футболки для нового замовлення?")
        current = self.durable_revision(price, "catalog", episode_id=new_episode.pk)
        case = IgFollowUpTask.objects.create(client=self.client_row, deal=old_deal, due_at=self.now,
            reason="revision_case:paid_fulfillment", kind="manager_task", status="skipped",
            event_payload={"deal_id": old_deal.pk}, manager_context={"deal_id": old_deal.pk})
        decision = build_turn_intent(self.client_row, current)
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=current), "")
        decision["source_scope"]["commercial_episode_id"] = old_episode.pk
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=current), "pending_manager_case")
        case.status = "completed"
        case.save(update_fields=["status"])
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=current), "payment_verification_pending")

    def ordinary_task(self):
        from management.services.ig_revision_outbox import _digest
        snapshot = {"schema_version": 1, "instructions": []}
        publication = BotPolicyPublication.objects.create(version=1, kind="publish", schema_version=1,
            snapshot=snapshot, snapshot_hash=_digest(snapshot), compiler_version="test", instruction_count=0)
        settings = InstagramBotSettings.objects.create(pk=1, is_enabled=True,
            active_instruction_publication=publication)
        self.client_row.last_message_at = self.now
        self.client_row.save(update_fields=["last_message_at"])
        message = self.message("Яка ціна?", provider_created_at=self.now)
        revision = self.durable_revision(message)
        decision = build_turn_intent(self.client_row, revision)
        task = IgFollowUpTask.objects.create(client=self.client_row, due_at=self.now,
            kind="thinking", reason="ordinary_price_inquiry", meta_window_deadline=self.now+timedelta(hours=20),
            event_payload={"origin": "ordinary_intent_followup", "revision_id": revision.pk,
                "snapshot_digest": revision.snapshot_digest, "settings_id": settings.pk,
                "settings_permission_epoch": settings.reply_permission_epoch,
                "publication_id": publication.pk, "publication_hash": publication.snapshot_hash,
                "source_message_ids": [message.pk], "cycle_key": decision["cycle_key"],
                "purpose": decision["purpose"], "client_permission_epoch": self.client_row.reply_permission_epoch,
                "product_id": None})
        return task, settings

    def test_inbound_during_transport_preparation_prevents_physical_followup(self):
        from management.services.instagram_bot import ProviderDeliveryReceipt
        task, settings = self.ordinary_task()
        physical_sends = []

        def transport(*args, **kwargs):
            # All worker checks have passed; this models token lookup / planning.
            self.message("Дякую, більше не потрібно")
            with kwargs["permission_boundary_factory"]() as permission:
                self.assertTrue(permission)
                with kwargs["provider_request_boundary_factory"](delivered_chunk_count=0, planned_chunk_count=1) as allowed:
                    if allowed:
                        physical_sends.append(args[2])
                    return ProviderDeliveryReceipt(bool(allowed), "" if allowed else "cancelled", allowed.reason, "mid" if allowed else "")

        with patch.object(policy, "_client_allows_followup", return_value=(True, "")), patch("management.services.instagram_bot.send_text", side_effect=transport):
            self.assertEqual(policy.process_due_followups(settings, now=self.now, limit=1), 0)
        self.assertEqual(physical_sends, [])
        task.refresh_from_db()
        self.assertEqual(task.status, "skipped")
        self.assertEqual(task.skip_reason, "new_customer_statement")

    def test_cached_ordinary_sales_claim_is_rebuilt_and_only_one_send_occurs(self):
        from management.services.instagram_bot import ProviderDeliveryReceipt
        task, settings = self.ordinary_task()
        task.message_text = "Ваше замовлення чекає! Є знижка 50%!"
        task.save(update_fields=["message_text"])
        physical_sends = []

        def transport(*args, **kwargs):
            with kwargs["permission_boundary_factory"]() as permission:
                self.assertTrue(permission)
                with kwargs["provider_request_boundary_factory"](delivered_chunk_count=0, planned_chunk_count=1) as allowed:
                    self.assertTrue(allowed, allowed.reason)
                    physical_sends.append(args[2])
                    return ProviderDeliveryReceipt(True, "", "", "ordinary-safe-mid")

        with patch.object(policy, "_client_allows_followup", return_value=(True, "")), patch("management.services.instagram_bot.send_text", side_effect=transport):
            self.assertEqual(policy.process_due_followups(settings, now=self.now, limit=1), 1)
            self.assertEqual(policy.process_due_followups(settings, now=self.now, limit=1), 0)
        self.assertEqual(physical_sends, [policy.compose_followup(task, now=self.now)])
        task.refresh_from_db()
        self.assertEqual(task.status, "sent")
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.client_row).count(), 1)

    def test_language_change_cannot_rewrite_prepared_transport_text(self):
        task, _settings = self.ordinary_task()
        task.status, task.claim_token, task.claim_until = "processing", "owner", self.now+timedelta(minutes=4)
        task.message_text = prepared = policy.compose_followup(task)
        task.save(update_fields=["status", "claim_token", "claim_until", "message_text"])
        IgClient.objects.filter(pk=self.client_row.pk).update(language="en")
        with patch.object(policy, "_client_allows_followup", return_value=(True, "")):
            with policy._ordinary_followup_provider_boundary(task.pk, "owner", prepared_text=prepared, checked_at=self.now) as allowed:
                self.assertFalse(allowed)
                self.assertEqual(allowed.reason, "ordinary_followup_copy_changed")

    def test_product_change_after_worker_checks_is_read_at_provider_boundary(self):
        from storefront.models import Category, Product
        task, _settings = self.ordinary_task()
        task.status, task.claim_token, task.claim_until = "processing", "owner", self.now+timedelta(minutes=4)
        task.save(update_fields=["status", "claim_token", "claim_until"])
        # Keep the worker's client object stale while changing durable focus.
        category = Category.objects.create(name="Followup race", slug="followup-race")
        product = Product.objects.create(title="New selection", slug="followup-race-shirt", category=category, price=900)
        IgClient.objects.filter(pk=self.client_row.pk).update(current_product=product)
        self.assertIsNone(task.client.current_product_id)
        with patch.object(policy, "_client_allows_followup", return_value=(True, "")):
            with policy._ordinary_followup_provider_boundary(task.pk, "owner", checked_at=self.now) as allowed:
                self.assertFalse(allowed)
                self.assertEqual(allowed.reason, "followup_product_changed")

    def test_reclaimed_task_cannot_be_cancelled_by_old_provider_boundary(self):
        task, _settings = self.ordinary_task()
        task.status, task.claim_token, task.claim_until = "processing", "new-owner", self.now+timedelta(minutes=4)
        task.save(update_fields=["status", "claim_token", "claim_until"])
        with policy._ordinary_followup_provider_boundary(task.pk, "old-owner", checked_at=self.now) as allowed:
            self.assertFalse(allowed)
            self.assertEqual(allowed.reason, "followup_claim_changed")
        task.refresh_from_db()
        self.assertEqual(task.claim_token, "new-owner")
        self.assertEqual(task.status, "processing")

    def test_payment_pending_at_provider_boundary_blocks_optional_contact(self):
        task, _settings = self.ordinary_task()
        task.status, task.claim_token, task.claim_until = "processing", "owner", self.now+timedelta(minutes=4)
        task.save(update_fields=["status", "claim_token", "claim_until"])
        IgDeal.objects.create(client=self.client_row, payment_truth="pending")
        with patch.object(policy, "_client_allows_followup", return_value=(True, "")):
            with policy._ordinary_followup_provider_boundary(task.pk, "owner", checked_at=self.now) as allowed:
                self.assertFalse(allowed)
                self.assertEqual(allowed.reason, "payment_verification_pending")

    def test_unknown_holds_its_own_obligation_but_not_proven_separate_topic(self):
        old_message = self.message("Коли відправите замовлення?")
        old = self.durable_revision(old_message, "support")
        price = self.message("Яка ціна нової футболки?")
        current = self.durable_revision(price, "catalog")
        effect = IgRevisionDeliveryEffect.objects.create(revision=old, source_message=old_message,
            effect_key="unknown-service", group="substantive_text", kind="text", order_index=0,
            part_index=0, part_count=1, plan_digest="a"*64, payload_digest="a"*64,
            recipient_igsid=self.client_row.igsid, provider_namespace="instagram_login:test",
            settings_id_snapshot=1, settings_permission_epoch=0, client_permission_epoch=0,
            revision_snapshot_digest=old.snapshot_digest, publication_id=1, publication_version=1,
            publication_hash="b"*64, authority_context_digest="c"*64, state="unknown")
        decision = build_turn_intent(self.client_row, current)
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=current), "")
        # A recovery revision of the same sealed obligation cannot use a new
        # route label as permission to send another physical answer.
        recovered = SimpleNamespace(pk=current.pk, turn_id=current.turn_id, snapshot_digest=old.snapshot_digest)
        self.assertEqual(purpose_blockers(self.client_row, decision, revision=recovered), "unknown_delivery_pending")
        effect.refresh_from_db()
        self.assertEqual(effect.state, "unknown")
