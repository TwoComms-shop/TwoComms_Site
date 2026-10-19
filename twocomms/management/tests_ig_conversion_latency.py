"""Local queue, scoped route and exact transport-tail regression contracts."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_revision_live as live
from management import tests_ig_revision_provider_execution as provider
from management.tests_gemini_accounting_shadow import SHADOW
from management.models import (GeminiRequest, GeminiRequestAttempt, IgBotNotification,
    IgClient, IgCustomerTurn, IgCustomerTurnRevision, IgFollowUpTask, IgProviderIncident,
    IgTurnMessage)
from management.services.ig_revision_execution import prepare_revision
from management.services.ig_revision_recovery import (EXECUTION_RESUME_KEY, TRANSPORT_TAIL_KEY,
    due_preparation_recovery_ids, due_recovery_revision_ids, execution_resume_is_current,
    effective_execution_receipt,
    prepare_outage_recovery, prepare_unstarted_preparation_recovery,
    reserve_proposal_transport_tail, schedule_revision_recovery)
from management.services.ig_turn_revisions import claim_revision_preparation, create_collecting_revision


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class PreparationQueueLatencyTests(TransactionTestCase):
    setUp = live.RevisionLiveTests.setUp
    _message = live.RevisionLiveTests._message

    def _slot(self, revision, now):
        token = f"worker-{revision.client_id}"
        IgClient.objects.filter(pk=revision.client_id).update(automation_lease_token=token,
            automation_lease_until=now + timedelta(seconds=180))
        return token

    def test_expired_claim_preserves_collecting_owner_and_spends_nothing(self):
        expired = self.revision.overall_deadline + timedelta(seconds=1)
        claim = claim_revision_preparation(self.revision.pk, now=expired)
        self.assertEqual(claim.reason, "preparation_deadline_exhausted")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.state, "collecting")
        self.assertEqual(self.revision.claim_token, "")
        self.assertIsNone(self.revision.lease_until)
        self.assertFalse(GeminiRequest.objects.exists())
        self.assertFalse(GeminiRequestAttempt.objects.exists())

    def test_five_queued_buyers_get_just_in_time_windows_without_manual_debt(self):
        roots = [self.revision]
        for number in range(2, 6):
            self.customer = IgClient.objects.create(igsid=f"1784000000000{number}", language="uk")
            source = self._message("Який розмір обрати?", f"queued-{number}")
            now = timezone.now()
            turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=source,
                window_started_at=now, window_deadline=now)
            IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
            roots.append(create_collecting_revision(turn, [source], bypass_quiet=True).revision)
        queued = max(row.overall_deadline for row in roots) + timedelta(seconds=1)
        for row in roots:
            self.assertEqual(schedule_revision_recovery(row.pk, now=queued).state, "waiting")
        self.assertEqual(len(due_preparation_recovery_ids(now=queued)), 5)
        self.assertEqual(due_recovery_revision_ids(now=queued), [])
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 5)
        for number, parent in enumerate(roots):
            # Each previous buyer may consume its complete 45-second slot.
            started = queued + timedelta(seconds=46 * number)
            token = self._slot(parent, started)
            outcome = prepare_unstarted_preparation_recovery(parent.pk,
                automation_lease_token=token, now=started)
            self.assertEqual(outcome.state, "spawned", outcome.reason)
            child = IgCustomerTurnRevision.objects.get(pk=outcome.child_id)
            self.assertEqual(child.overall_deadline, started + timedelta(seconds=45))
            self.assertEqual(list(child.sources.values_list("message_id", flat=True)),
                list(parent.sources.values_list("message_id", flat=True)))
            claim = claim_revision_preparation(child.pk, now=started)
            self.assertTrue(claim.token, claim.reason)
            self.assertGreater(claim.revision.lease_until, started)
            parent.refresh_from_db()
            self.assertEqual(parent.overall_deadline, roots[number].overall_deadline)
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 10)
        self.assertFalse(IgFollowUpTask.objects.exists())
        self.assertFalse(GeminiRequest.objects.exists())

    def test_second_queue_expiry_does_not_mint_another_generation_window(self):
        queued = self.revision.overall_deadline + timedelta(seconds=1)
        schedule_revision_recovery(self.revision.pk, now=queued)
        token = self._slot(self.revision, queued)
        result = prepare_unstarted_preparation_recovery(self.revision.pk,
            automation_lease_token=token, now=queued)
        child = IgCustomerTurnRevision.objects.get(pk=result.child_id)
        self.assertEqual(schedule_revision_recovery(child.pk,
            now=child.overall_deadline + timedelta(seconds=1)).reason, "preparation_recovery_exhausted")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 2)

    def test_missing_slot_and_new_source_cannot_recover(self):
        queued = self.revision.overall_deadline + timedelta(seconds=1)
        schedule_revision_recovery(self.revision.pk, now=queued)
        result = prepare_unstarted_preparation_recovery(self.revision.pk,
            automation_lease_token="not-owned", now=queued)
        self.assertEqual(result.reason, "preparation_worker_slot_missing")
        token = self._slot(self.revision, queued)
        self._message("Змінився запит", "new-context")
        result = prepare_unstarted_preparation_recovery(self.revision.pk,
            automation_lease_token=token, now=queued)
        self.assertEqual(result.reason, "recovery_newer_inbound")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    def test_changed_source_cannot_be_recaptured_as_old_queue_work(self):
        queued = self.revision.overall_deadline + timedelta(seconds=1)
        schedule_revision_recovery(self.revision.pk, now=queued)
        token = self._slot(self.revision, queued)
        self.source.text = "Відредаговане питання"
        self.source.save(update_fields=["text"])
        result = prepare_unstarted_preparation_recovery(self.revision.pk,
            automation_lease_token=token, now=queued)
        self.assertEqual(result.reason, "recovery_sources_invalidated")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)

    def test_permission_change_cancels_queue_admission(self):
        queued = self.revision.overall_deadline + timedelta(seconds=1)
        schedule_revision_recovery(self.revision.pk, now=queued)
        token = self._slot(self.revision, queued)
        IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1)
        result = prepare_unstarted_preparation_recovery(self.revision.pk,
            automation_lease_token=token, now=queued)
        self.assertEqual(result.reason, "recovery_permission_changed")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)


@override_settings(**SHADOW, IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class IndependentProjectLatencyTests(TransactionTestCase):
    setUp = provider.RevisionProviderExecutionTests.setUp
    _message = provider.RevisionProviderExecutionTests._message
    _prepare = provider.RevisionProviderExecutionTests._prepare
    _begin = provider.RevisionProviderExecutionTests._begin
    _attempt = provider.RevisionProviderExecutionTests._attempt
    _admit = provider.RevisionProviderExecutionTests._admit

    def test_ready_independent_project_ignores_other_cooldown_and_grouped_incident(self):
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution
        self.raw_plan = [self.raw_plan[0], dict(self.raw_plan[1], project_identity="independent-B")]
        self._admit()
        observer = self._begin()
        failed = self._attempt(observer, 0, code=429, failure="quota_429")
        GeminiRequestAttempt.objects.filter(pk=failed.attempt_id).update(provider_retry_after_seconds=300)
        observer.resolve_failure("quota_429")
        self.revision.refresh_from_db()
        now = self.revision.overall_deadline + timedelta(seconds=1)
        continuation = inspect_revision_provider_execution(self.revision, now=now)
        self.assertTrue(continuation.ready)
        self.assertIsNone(continuation.next_due_at)
        self.assertEqual(continuation.http_remaining, 7)
        scheduled = schedule_revision_recovery(self.revision.pk, now=now)
        self.assertEqual(scheduled.state, "waiting")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_due_at, now)
        IgProviderIncident.objects.create(role="chat", failure_class="quota", fingerprint="shared-A",
            active_fingerprint="shared-A", state="open", opened_at=now, last_failure_at=now)
        with patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no recovery I/O")):
            result = prepare_outage_recovery(self.revision.pk, now=now)
        self.assertEqual(result.state, "spawned", result.reason)
        child = IgCustomerTurnRevision.objects.get(pk=result.child_id)
        remaining = inspect_revision_provider_execution(child, now=now)
        self.assertEqual(remaining.http_remaining, 7)
        self.assertEqual(remaining.candidate_plan[0]["skip_reason"], "provider_candidate_wait")
        self.assertEqual(remaining.candidate_plan[1]["skip_reason"], "")

    def test_all_real_candidates_wait_for_their_scoped_retry_after(self):
        from management.services.ig_revision_provider_execution import inspect_revision_provider_execution
        self.raw_plan = self.raw_plan[:1]
        self._admit()
        observer = self._begin()
        failed = self._attempt(observer, 0, code=429, failure="quota_429")
        GeminiRequestAttempt.objects.filter(pk=failed.attempt_id).update(provider_retry_after_seconds=300)
        observer.resolve_failure("quota_429")
        self.revision.refresh_from_db()
        now = self.revision.overall_deadline + timedelta(seconds=1)
        continuation = inspect_revision_provider_execution(self.revision, now=now)
        self.assertFalse(continuation.ready)
        self.assertEqual(continuation.reason, "provider_wait")
        self.assertGreater(continuation.next_due_at, now)
        scheduled = schedule_revision_recovery(self.revision.pk, now=now)
        self.assertEqual(scheduled.state, "waiting")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.recovery_due_at, continuation.next_due_at)
        with patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no recovery I/O")) as http:
            result = prepare_outage_recovery(self.revision.pk, now=now)
            http.assert_not_called()
        self.assertEqual(result.reason, "recovery_not_due")
        self.assertEqual(IgCustomerTurnRevision.objects.count(), 1)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class ExactTransportTailLatencyTests(TransactionTestCase):
    setUp = live.RevisionLiveTests.setUp
    _message = live.RevisionLiveTests._message
    _prepare = live.RevisionLiveTests._prepare
    _generate = live.RevisionLiveTests._generate
    _execute = live.RevisionLiveTests._execute
    _replace_bundle = live.RevisionLiveTests._replace_bundle

    def _proposal(self):
        from management.services.ig_revision_live import execute_claimed_revision
        if not self.token:
            self._prepare()
        with (patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=self._generate),
              patch("management.services.instagram_bot.get_page_token", return_value="")):
            result = execute_claimed_revision(self.revision.pk, self.token, self.settings)
        self.assertEqual(result.reasons, ("provider_not_configured",))
        self.revision.refresh_from_db()
        from management.services.ig_revision_transport import prepare_text_effects
        text = "\n".join(f"Варіант {number}: зручна футболка для щоденного використання." for number in range(25))
        effects = prepare_text_effects(self.customer.igsid, text).effects
        self.assertEqual(len(effects), 3)
        return effects

    def test_exact_three_parts_get_finite_transport_budget_and_no_new_model(self):
        effects = self._proposal()
        now = self.revision.overall_deadline - timedelta(seconds=10)
        result = reserve_proposal_transport_tail(self.revision.pk, self.token,
            effects=effects, settings_id=self.settings.pk, now=now)
        self.assertEqual(result.state, "execution", result.reason)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[EXECUTION_RESUME_KEY]
        self.assertEqual(timezone.datetime.fromisoformat(receipt["expires_at"]), now + timedelta(seconds=41))
        self.assertEqual(self.revision.claim_token, self.token)
        self.assertTrue(execution_resume_is_current(self.revision, now=now + timedelta(seconds=40)))
        self.assertFalse(execution_resume_is_current(self.revision, now=now + timedelta(seconds=41)))
        again = reserve_proposal_transport_tail(self.revision.pk, self.token,
            effects=effects, settings_id=self.settings.pk, now=now + timedelta(seconds=1))
        self.assertEqual(again.state, "execution")
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.action_receipts[EXECUTION_RESUME_KEY], receipt)
        self.assertEqual(GeminiRequest.objects.count(), 1)
        self._message("Нова інформація", "tail-new-source")
        self.assertFalse(execution_resume_is_current(self.revision, now=now + timedelta(seconds=2)))

    def test_transport_plan_cannot_change_or_renew_expired_receipt(self):
        effects = self._proposal()
        now = self.revision.overall_deadline - timedelta(seconds=10)
        reserve_proposal_transport_tail(self.revision.pk, self.token, effects=effects, now=now)
        result = reserve_proposal_transport_tail(self.revision.pk, self.token,
            effects=effects[:2], now=now)
        self.assertEqual(result.reason, "proposal_transport_plan_changed")
        self.revision.refresh_from_db()
        self.assertFalse(execution_resume_is_current(self.revision,
            now=timezone.datetime.fromisoformat(self.revision.action_receipts[EXECUTION_RESUME_KEY]["expires_at"])))

    def test_approved_selection_action_does_not_invalidate_its_transport_tail(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product
        category = Category.objects.create(name="Tail", slug="tail")
        product = Product.objects.create(title="Tail product", slug="tail-product",
            category=category, price=900, status="published")
        color = Color.objects.create(name="Tail black", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(product=product, color=color,
            price_override=900, is_default=True)
        self.parsed["controls"] = [{"kind": "product", "value": str(product.pk)},
            {"kind": "color_variant_id", "value": str(variant.pk)}, {"kind": "qty", "value": "2"}]
        self._replace_bundle(["Хочу замовити дві футболки."])
        effects = self._proposal()
        self.assertIn("client_configuration_update", self.revision.action_receipts)
        result = reserve_proposal_transport_tail(self.revision.pk, self.token, effects=effects,
            now=self.revision.overall_deadline - timedelta(seconds=10))
        self.assertEqual(result.state, "execution", result.reason)

    def _gallery_with_text(self, count):
        gallery = [{"group": "catalog_media", "kind": "image", "payload": {
            "recipient": {"id": self.customer.igsid}, "message": {"attachment": {
                "type": "image", "payload": {"url": f"https://twocomms.test/item-{number}.jpg"}}}}}
            for number in range(count - 1)]
        return [*gallery, {"group": "substantive_text", "kind": "text", "payload": {
            "recipient": {"id": self.customer.igsid}, "message": {"text": "Ось доступні варіанти."}}}]

    def _assert_supported_plan_count(self, count):
        from management.services.instagram_bot import HTTP_TIMEOUT
        self._proposal()
        effects = self._gallery_with_text(count)
        deadline = self.revision.overall_deadline
        now = deadline - timedelta(seconds=10)
        with (patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no transport I/O")) as http,
              patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("no new generation")) as model):
            result = reserve_proposal_transport_tail(self.revision.pk, self.token, effects=effects, now=now)
            http.assert_not_called()
            model.assert_not_called()
        self.assertEqual(result.state, "execution", result.reason)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts[EXECUTION_RESUME_KEY]
        expires = now + timedelta(seconds=count * HTTP_TIMEOUT + 5)
        self.assertEqual(receipt["transport_tail"]["effect_count"], count)
        self.assertEqual(timezone.datetime.fromisoformat(receipt["expires_at"]), expires)
        self.assertGreaterEqual(self.revision.lease_until, expires)
        self.assertEqual(self.revision.overall_deadline, deadline)
        self.assertTrue(execution_resume_is_current(self.revision, now=expires - timedelta(seconds=1)))
        self.assertFalse(execution_resume_is_current(self.revision, now=expires))
        self.assertEqual(GeminiRequest.objects.count(), 1)

    def test_five_effect_gallery_and_text_preserve_supported_plan(self):
        self._assert_supported_plan_count(5)

    def test_sixteen_effect_canonical_maximum_has_finite_execution_lease(self):
        from management.services.ig_revision_outbox import MAX_EFFECTS
        self.assertEqual(MAX_EFFECTS, 16)
        self._assert_supported_plan_count(MAX_EFFECTS)

    def test_seventeen_effects_cannot_expand_canonical_outbox_limit(self):
        self._proposal()
        with (patch("management.services.instagram_bot._provider_http", side_effect=AssertionError("no transport I/O")) as http,
              patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=AssertionError("no new generation")) as model):
            result = reserve_proposal_transport_tail(self.revision.pk, self.token,
                effects=self._gallery_with_text(17), now=self.revision.overall_deadline - timedelta(seconds=10))
            http.assert_not_called()
            model.assert_not_called()
        self.assertEqual(result.reason, "proposal_transport_plan_invalid")
        self.revision.refresh_from_db()
        self.assertNotIn(EXECUTION_RESUME_KEY, self.revision.action_receipts)
        self.assertFalse(self.revision.delivery_effects.exists())
        self.assertEqual(GeminiRequest.objects.count(), 1)

    def _plan_effects(self, effects, now):
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_outbox import PublicationBinding, plan_revision_effects
        reserved = reserve_proposal_transport_tail(self.revision.pk, self.token, effects=effects, now=now)
        self.assertEqual(reserved.state, "execution", reserved.reason)
        self.revision.refresh_from_db()
        authority = self.revision.generation_proposal["authority"]
        generation = self.revision.generation_proposal["generation"]
        plan = plan_revision_effects(self.revision.pk, self.token, source_message_id=self.source.pk,
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash),
            authority_context_digest=authority["authority_digest"], effects=effects,
            fact_bindings=authority["fact_bindings"], offer_bindings=authority["offer_bindings"],
            generation_request_id=generation["request_id"], generation_model=generation["actual_model"],
            fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now)
        self.assertTrue(plan.created, plan.reasons)
        return now

    def _planned_tail(self):
        effects = self._proposal()
        return self._plan_effects(effects, self.revision.overall_deadline - timedelta(seconds=10))

    def test_late_first_mid_completes_three_parts_without_replaying_sent(self):
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group
        clock = [self._planned_tail()]
        calls = []
        def transport(payload):
            calls.append(payload)
            clock[0] += timedelta(seconds=11)
            return ProviderPartResult("instagram_login:owner-1", http_status=200,
                provider_message_id=f"late-mid-{len(calls)}")
        with patch("management.services.ig_revision_outbox.timezone.now", side_effect=lambda: clock[0]):
            result = drain_group(self.revision.pk, self.token, "substantive_text", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(result.state, "sent", result.reason)
            self.assertEqual(result.attempted, 3)
            self.assertGreater(clock[0], self.revision.overall_deadline)
            replay = drain_group(self.revision.pk, self.token, "substantive_text", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(replay.attempted, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(list(self.revision.delivery_effects.values_list("state", flat=True)), ["sent"] * 3)
        self.assertEqual(GeminiRequest.objects.count(), 1)

    def test_new_inbound_between_parts_cancels_only_unstarted_tail(self):
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group
        clock = [self._planned_tail()]
        calls = []
        def transport(payload):
            calls.append(payload)
            clock[0] += timedelta(seconds=11)
            self._message("Зачекайте, хочу інший товар", "between-tail-parts")
            return ProviderPartResult("instagram_login:owner-1", http_status=200, provider_message_id="first-mid")
        with patch("management.services.ig_revision_outbox.timezone.now", side_effect=lambda: clock[0]):
            drain_group(self.revision.pk, self.token, "substantive_text", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.revision.delivery_effects.order_by("order_index").values_list("state", flat=True)),
            ["sent", "cancelled", "cancelled"])

    def test_unknown_first_part_blocks_tail_and_replay(self):
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group
        now = self._planned_tail()
        calls = []
        def transport(payload):
            calls.append(payload)
            return ProviderPartResult("instagram_login:owner-1", outcome="timeout")
        with patch("management.services.ig_revision_outbox.timezone.now", return_value=now):
            first = drain_group(self.revision.pk, self.token, "substantive_text", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(first.state, "unknown")
            replay = drain_group(self.revision.pk, self.token, "substantive_text", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(replay.attempted, 0)
            self.revision.refresh_from_db()
            self.assertTrue(execution_resume_is_current(self.revision, now=now))
        self.assertEqual(len(calls), 1)

    def test_unknown_gallery_does_not_block_independent_late_text_or_replay_image(self):
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group
        self._proposal()
        clock = [self._plan_effects(self._gallery_with_text(2),
            self.revision.overall_deadline - timedelta(seconds=1))]
        calls = []
        def transport(payload):
            calls.append(payload)
            clock[0] += timedelta(seconds=11)
            if "attachment" in payload["message"]:
                return ProviderPartResult("instagram_login:owner-1", outcome="timeout")
            return ProviderPartResult("instagram_login:owner-1", http_status=200, provider_message_id="late-text-mid")
        with patch("management.services.ig_revision_outbox.timezone.now", side_effect=lambda: clock[0]):
            gallery = drain_group(self.revision.pk, self.token, "catalog_media", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(gallery.state, "unknown")
            self.assertGreater(clock[0], self.revision.overall_deadline)
            text = drain_group(self.revision.pk, self.token, "substantive_text", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(text.state, "sent", text.reason)
            self.assertEqual(drain_group(self.revision.pk, self.token, "catalog_media", transport).attempted, 0)
            self.assertEqual(drain_group(self.revision.pk, self.token, "substantive_text", transport).attempted, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(self.revision.delivery_effects.order_by("order_index").values_list("state", flat=True)),
            ["unknown", "sent"])

    def test_inflight_gallery_does_not_block_independent_late_text(self):
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group
        from management.services.ig_revision_outbox import claim_next_effect, mark_provider_started
        self._proposal()
        now = self._plan_effects(self._gallery_with_text(2), self.revision.overall_deadline - timedelta(seconds=1))
        with patch("management.services.ig_revision_outbox.timezone.now", return_value=now):
            claim = claim_next_effect(self.revision.pk, self.token, "catalog_media")
            started = mark_provider_started(claim.effect.pk, claim.token, self.token,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings, now=now)
        self.assertEqual(started.reason, "provider_started")
        with patch("management.services.ig_revision_outbox.timezone.now", return_value=now + timedelta(seconds=2)):
            text = drain_group(self.revision.pk, self.token, "substantive_text",
                lambda payload: ProviderPartResult("instagram_login:owner-1", http_status=200,
                    provider_message_id="independent-text-mid"),
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
        self.assertEqual(text.state, "sent", text.reason)
        self.assertEqual(self.revision.delivery_effects.get(group="catalog_media").state, "provider_started")

    def test_definite_template_rejection_activates_late_fallback_once(self):
        from management.services.ig_message_templates import GenericTemplate, TemplateCard, prepare_template_effects
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group
        self._proposal()
        prepared = prepare_template_effects(self.customer.igsid,
            GenericTemplate(cards=(TemplateCard(title="Card", subtitle="Description"),),
                fallback_text="Ось текстовий варіант.", projection_text="Card"))
        clock = [self._plan_effects(prepared.effects, self.revision.overall_deadline - timedelta(seconds=1))]
        calls = []
        def transport(payload):
            calls.append(payload)
            clock[0] += timedelta(seconds=11)
            if "attachment" in payload["message"]:
                return ProviderPartResult("instagram_login:owner-1", http_status=400, outcome="explicit_rejected")
            return ProviderPartResult("instagram_login:owner-1", http_status=200, provider_message_id="fallback-mid")
        with patch("management.services.ig_revision_outbox.timezone.now", side_effect=lambda: clock[0]):
            primary = drain_group(self.revision.pk, self.token, "template", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(primary.state, "definite_failed")
            self.assertTrue(primary.fallback_ready)
            fallback = drain_group(self.revision.pk, self.token, "template_fallback", transport,
                fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
            self.assertEqual(fallback.state, "sent", fallback.reason)
            self.assertEqual(drain_group(self.revision.pk, self.token, "template", transport).attempted, 0)
            self.assertEqual(drain_group(self.revision.pk, self.token, "template_fallback", transport).attempted, 0)
        self.assertEqual(len(calls), 2)

    def test_current_ordinary_resume_upgrades_once_before_any_effect_is_planned(self):
        from management.services.ig_revision_live import _reclaim_execution
        from management.services.ig_revision_authority import check_fact_bindings, check_offer_bindings
        from management.services.ig_revision_delivery import ProviderPartResult, drain_group
        self._proposal()
        now = self.revision.overall_deadline + timedelta(seconds=1)
        self.assertEqual(schedule_revision_recovery(self.revision.pk, now=now).state, "execution")
        self.revision.refresh_from_db()
        ordinary = self.revision.action_receipts[EXECUTION_RESUME_KEY].copy()
        with patch("management.services.ig_revision_live.timezone.now", return_value=now):
            claimed, self.token = _reclaim_execution(self.revision.pk)
        self.assertIsNotNone(claimed)
        effects = self._gallery_with_text(5)
        clock = [self._plan_effects(effects, now)]
        self.revision.refresh_from_db()
        tail = effective_execution_receipt(self.revision)
        self.assertIn(TRANSPORT_TAIL_KEY, self.revision.action_receipts)
        self.assertEqual(self.revision.action_receipts[EXECUTION_RESUME_KEY], ordinary)
        self.assertEqual(tail["upgraded_from_digest"], ordinary["digest"])
        self.assertEqual(timezone.datetime.fromisoformat(tail["expires_at"]), now + timedelta(seconds=65))
        calls = []
        def transport(payload):
            calls.append(payload)
            clock[0] += timedelta(seconds=11)
            return ProviderPartResult("instagram_login:owner-1", http_status=200,
                provider_message_id=f"resume-mid-{len(calls)}")
        with patch("management.services.ig_revision_outbox.timezone.now", side_effect=lambda: clock[0]):
            for group in ("catalog_media", "substantive_text"):
                result = drain_group(self.revision.pk, self.token, group, transport,
                    fact_checker=check_fact_bindings, offer_checker=check_offer_bindings)
                self.assertEqual(result.state, "sent", result.reason)
                self.assertEqual(drain_group(self.revision.pk, self.token, group, transport).attempted, 0)
            again = reserve_proposal_transport_tail(self.revision.pk, self.token, effects=effects, now=clock[0])
        self.assertEqual(again.state, "execution")
        self.assertGreater(clock[0], timezone.datetime.fromisoformat(ordinary["expires_at"]))
        self.assertEqual(len(calls), 5)
        self.revision.refresh_from_db()
        self.assertEqual(effective_execution_receipt(self.revision), tail)
        self.assertEqual(GeminiRequest.objects.count(), 1)

    def test_expired_ordinary_resume_cannot_receive_a_fresh_transport_timer(self):
        from management.services.ig_revision_live import _reclaim_execution
        self._proposal()
        now = self.revision.overall_deadline + timedelta(seconds=1)
        self.assertEqual(schedule_revision_recovery(self.revision.pk, now=now).state, "execution")
        self.revision.refresh_from_db()
        ordinary = self.revision.action_receipts[EXECUTION_RESUME_KEY].copy()
        with patch("management.services.ig_revision_live.timezone.now", return_value=now):
            claimed, token = _reclaim_execution(self.revision.pk)
        self.assertIsNotNone(claimed)
        with (patch("management.services.instagram_bot._provider_http") as http,
              patch("management.services.call_ai_analysis.gemini_generate_text") as generate):
            result = reserve_proposal_transport_tail(self.revision.pk, token,
                effects=self._gallery_with_text(5),
                now=timezone.datetime.fromisoformat(ordinary["expires_at"]))
            http.assert_not_called()
            generate.assert_not_called()
        self.assertEqual(result.reason, "proposal_execution_resume_expired")
        self.revision.refresh_from_db()
        self.assertNotIn(TRANSPORT_TAIL_KEY, self.revision.action_receipts)
        self.assertEqual(self.revision.action_receipts[EXECUTION_RESUME_KEY], ordinary)
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_unchanged_manager_case_replay_has_no_new_generation_or_notification(self):
        self._replace_bundle(["Покличте менеджера, будь ласка."])
        self.parsed["reply_text"] = "Передам ваш запит менеджеру."
        self.parsed["controls"] = [{"kind": "manager", "value": True}]
        first, generated, sent = self._execute()
        self.assertEqual(first.state, "completed", first.reasons)
        self.assertEqual(generated.call_count, 1)
        counts = (IgFollowUpTask.objects.count(), IgBotNotification.objects.count(), GeminiRequest.objects.count())
        with patch("management.services.instagram_bot._deliver_manager_notification") as notify:
            for _ in range(3):
                result, generated, sent = self._execute()
                generated.assert_not_called()
                sent.assert_not_called()
            notify.assert_not_called()
        self.assertEqual((IgFollowUpTask.objects.count(), IgBotNotification.objects.count(), GeminiRequest.objects.count()), counts)
