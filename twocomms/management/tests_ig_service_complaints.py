"""Own complaint truth, bounded read guards and exact deliberate resolution."""
from copy import deepcopy
from datetime import timedelta
import os
import re
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (IgClient, IgCustomerTurn, IgFollowUpTask,
    IgFunnelResetAudit, IgTurnMessage, InstagramBotMessage, InstagramBotSettings)
from management.services.ig_service_complaints import (HOLD_REASON, TASK_REASON,
    classify_service_complaint, promotion_service_hold_reason, revision_service_complaint)
from management.services.ig_turn_revisions import (claim_revision_preparation,
    create_collecting_revision, seal_revision)
from management import tests_ig_revision_live as revision_fixture_module

ACTUAL_DELIVERY_COMPLAINT = "Доставка коштувала 90 а взяли 120\nТак 30 грн не багато, але на ровному місці\nОсь такі перші враження"


class ServiceComplaintClassifierTests(SimpleTestCase):
    def test_actual_fee_and_negative_first_impression_in_three_languages(self):
        for text in (
            ACTUAL_DELIVERY_COMPLAINT,
            "Delivery was 90, but I was charged 120. Only 30 extra, but a negative first impression.",
            "Доставка стоила 90, а взяли 120. Первые впечатления неприятные.",
            "Ви казали доставка 90 грн, а заплатила 120. Перше враження погане.",
            "Вы обещали доставку 90, а взяли 120. Первое впечатление негативное.",
            "You quoted shipping at 90 but I paid 120. My first impression is not good.",
            "За доставку писали 90, а виявилось 120.",
            "Ви казали доставка 90грн, а заплатив 120грн",
            "Delivery was 90.50, but I was charged 120.75, why?",
            "Доставка коштувала 90,50 а взяли 120,75",
        ):
            with self.subTest(text=text):
                result = classify_service_complaint(text)
                self.assertEqual(result["kind"], "delivery_fee_dispute")
                self.assertIs(result["customer_reported"], True)
                self.assertIs(result["monetary_authority"], False)
                self.assertNotIn("amount", result)

    def test_defect_missing_wrong_item_and_actual_dissatisfaction(self):
        for text in ("Моя футболка пошкоджена", "Заказ не пришел", "My parcel has not arrived",
            "I haven't received my order", "Received the wrong size shirt", "Я розчарована товаром",
            "My first impression of my order is bad", "The shirt is not damaged, but my parcel never arrived",
            "I'm disappointed with your support service"):
            with self.subTest(text=text):
                self.assertEqual(classify_service_complaint(text)["kind"], "service_complaint")

    def test_reported_friend_clause_does_not_erase_separate_own_missing_order(self):
        for text in ("My friend received a damaged shirt. My order never arrived",
            "Мій друг отримав пошкоджену футболку. Моя посилка не прийшла",
            "Мой друг получил поврежденную футболку. Мой заказ не пришел"):
            with self.subTest(text=text):
                self.assertEqual(classify_service_complaint(text)["kind"], "service_complaint")

    def test_polite_questions_about_actual_past_service_remain_own_complaints(self):
        for text in ("Can you check why my parcel never arrived?",
            "Could you help with the damaged shirt I received?",
            "Чи можете перевірити чому моя посилка не прийшла?",
            "Чи можете допомогти з пошкодженою футболкою, яку я отримав?",
            "Можете проверить почему мой заказ не пришел?",
            "Можете помочь с поврежденной футболкой, которую я получил?"):
            with self.subTest(text=text):
                self.assertEqual(classify_service_complaint(text)["kind"], "service_complaint")

    def test_single_quoted_other_story_does_not_erase_separate_own_concern_or_contraction(self):
        for text in ("'My parcel never arrived'. My shirt arrived damaged",
            "'My friend received a damaged shirt'. I haven't received my order",
            "'Моя посилка не прийшла'. Моя футболка пошкоджена"):
            with self.subTest(text=text):
                self.assertEqual(classify_service_complaint(text)["kind"], "service_complaint")

    def test_local_negation_does_not_erase_actual_defect_in_another_clause(self):
        for text in ("Не скаржусь, але у замовленні дірка", "I'm not complaining, but my shirt has a hole"):
            with self.subTest(text=text):
                self.assertEqual(classify_service_complaint(text)["kind"], "service_complaint")

    def test_questions_negation_reported_quotes_and_hypotheticals_are_not_complaints(self):
        for text in ("Скільки доставка: 90 чи 120?", "How much does shipping cost?",
            "Чому доставка коштує 120?", "Проблем немає", "No issues, the shirt is not damaged",
            "Это не жалоба, просто спрашиваю о доставке", "I'm not complaining about shipping",
            "If my parcel never arrives, what is your policy?", "Якщо футболка пошкоджена, що робити?",
            "My friend received a damaged shirt", '"My parcel never arrived"',
            "> Ви казали доставка 90, а взяли 120", "Цитата: посилка не прийшла",
            "What is your return policy?", "Returning customer asking about prices",
            "Do you sell damaged shirts?",
            "Ви казали доставка 90, я заплатив 90, все добре",
            "You quoted 90 shipping and I paid 90, thanks",
            "You quoted shipping 90-120 and I paid 120, thank you",
            "Якщо доставка 90 а взяли 120, що робити?",
            "What should I do if my parcel never arrives?", "Is my shirt damaged?",
            "Could my shirt be damaged?", "Can my parcel never arrive?",
            "'My parcel never arrived'", "'I haven't received my order'",
            "A friend received a damaged shirt", "Я незадоволений погодою",
            "I'm disappointed about the weather", "I'm disappointed about my job interview",
            "Я розчарована", "My first impression is bad",
            "Доставка коштувала 90,00 а взяли 90,00",
            "Delivery was 90.50 and I paid 90.50",
            "Delivery was NaN but I paid NaN",
            "Delivery was 99999999999999999999 but I paid 88888888888888888888",
            "आकार के बारे में एक प्रश्न", None, 17, "x" * 8001):
            with self.subTest(text=text):
                self.assertEqual(classify_service_complaint(text), {})


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class ServiceComplaintSourceGuardTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        environment.start()
        self.addCleanup(environment.stop)
        self.now = timezone.now()
        self.settings_row = InstagramBotSettings.objects.create(is_enabled=True, ig_user_id="service-owner")
        self.customer = IgClient.objects.create(igsid="service-customer")
        self.source = self.message("You said shipping was 90 but I paid 120.")

    def message(self, text, **extra):
        values = dict(client=self.customer, sender_id=self.customer.igsid, role="user",
            source="webhook", mid=f"service-mid-{InstagramBotMessage.objects.count() + 1}",
            provider_namespace="instagram_login:service-owner", provider_created_at=self.now,
            text=text, status="pending")
        values.update(extra)
        return InstagramBotMessage.objects.create(**values)

    def revision(self):
        turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=self.source,
            window_started_at=self.now, window_deadline=self.now)
        IgTurnMessage.objects.create(turn=turn, message=self.source, ordinal=1, role="user")
        revision = create_collecting_revision(turn, [self.source], bypass_quiet=True, now=self.now).revision
        claim = claim_revision_preparation(revision.pk, now=self.now)
        self.assertTrue(claim.token, claim.reason)
        sealed = seal_revision(revision.pk, claim.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        return sealed.revision

    def task(self, revision, *, status="skipped", **changes):
        proof = revision_service_complaint(revision)
        self.assertTrue(proof)
        values = dict(client=self.customer, kind="manager_task", reason=TASK_REASON,
            status=status, due_at=self.now, manager_context={"service_complaint": proof,
                "latest_revision_id": revision.pk, "sources": proof["source_refs"]})
        values.update(changes)
        return IgFollowUpTask.objects.create(**values)

    def guard(self):
        statements = []
        def read_only(execute, sql, params, many, context):
            statements.append(sql)
            if re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|ALTER|CREATE|DROP)\b|FOR\s+UPDATE", sql, re.I):
                raise AssertionError("Complaint guard attempted a mutation or lock")
            return execute(sql, params, many, context)
        with connection.execute_wrapper(read_only), patch("management.services.instagram_bot._provider_http") as provider:
            result = promotion_service_hold_reason(self.customer, now=self.now)
        provider.assert_not_called()
        self.assertTrue(statements)
        return result

    def test_fresh_actual_owned_complaint_holds_before_analysis_or_case_exists(self):
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_actual_sealed_revision_exposes_only_own_source_proof(self):
        revision = self.revision()
        proof = revision_service_complaint(revision)
        self.assertEqual(proof["client_id"], self.customer.pk)
        self.assertEqual(proof["source_refs"], [{"message_id": self.source.pk,
            "source_digest": revision.sources.get().source_digest}])
        self.assertFalse(proof["monetary_authority"])
        self.assertNotIn("order_id", proof)

    def test_verbatim_actual_incident_is_owned_in_sealed_revision_and_preanalysis_guard(self):
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text=ACTUAL_DELIVERY_COMPLAINT)
        self.source.refresh_from_db()
        revision = self.revision()
        self.assertEqual(revision_service_complaint(revision)["kind"], "delivery_fee_dispute")
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_wrong_sender_role_namespace_missing_mid_and_future_time_do_not_hold(self):
        for fields in ({"sender_id": "someone-else"}, {"role": "manager"},
            {"provider_namespace": "instagram_login:another-owner"}, {"mid": ""},
            {"provider_created_at": self.now + timedelta(seconds=1)}, {"status": "failed"}):
            with self.subTest(fields=fields):
                original = {key: getattr(self.source, key) for key in fields}
                InstagramBotMessage.objects.filter(pk=self.source.pk).update(**fields)
                self.assertEqual(self.guard(), "")
                InstagramBotMessage.objects.filter(pk=self.source.pk).update(**original)

    def test_foreign_client_and_stale_customer_statement_cannot_hold_this_owner(self):
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Thank you")
        other = IgClient.objects.create(igsid="service-other")
        self.message("My parcel never arrived", client=other, sender_id=other.igsid)
        self.assertEqual(self.guard(), "")
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="My parcel never arrived",
            provider_created_at=self.now - timedelta(hours=25))
        self.assertEqual(self.guard(), "")

    def test_neutral_source_before_task_materialization_does_not_clear_fresh_complaint(self):
        self.message("Everything is fine, thanks")
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_valid_unresolved_case_survives_neutral_customer_and_manager_explanations(self):
        revision = self.revision()
        self.task(revision)
        self.message("Thank you for explaining")
        self.message("We will check the charge", role="manager", sender_id="service-owner")
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_copied_context_without_canonical_owner_receipt_never_resolves_source(self):
        revision = self.revision()
        task = self.task(revision, status="completed",
            event_key=f"ig-revision-case:{self.customer.pk}:{revision.pk}:service_complaint_review")
        self.assertEqual(self.guard(), HOLD_REASON)
        task.status = "cancelled"
        task.save(update_fields=["status"])
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_completed_other_source_never_resolves_new_customer_complaint(self):
        self.task(self.revision(), status="completed")
        self.message("My order never arrived")
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_changed_source_and_forged_current_scope_task_preserve_conservative_hold(self):
        revision = self.revision()
        task = self.task(revision)
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(text="Everything is fine")
        self.assertEqual(revision_service_complaint(revision), {})
        self.assertEqual(self.guard(), HOLD_REASON)
        task.manager_context = {"service_complaint": {"client_id": self.customer.pk,
            "revision_id": revision.pk, "source_refs": [{"message_id": self.source.pk, "source_digest": "0" * 64}]}}
        task.save(update_fields=["manager_context"])
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_reset_and_privacy_fence_invalidate_current_source_and_old_task_scope(self):
        revision = self.revision()
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk, reason="new scope")
        self.assertEqual(revision_service_complaint(revision), {})
        self.assertEqual(self.guard(), "")
        self.message("My parcel never arrived")
        self.assertEqual(self.guard(), HOLD_REASON)
        self.customer.privacy_erasure_started_at = self.now
        self.customer.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.guard(), "")

    def test_revision_snapshot_digest_tampering_is_rejected_without_saving(self):
        revision = self.revision()
        revision.bundle_snapshot = deepcopy(revision.bundle_snapshot)
        revision.bundle_snapshot["sources"][0]["text"] = "My parcel never arrived"
        self.assertEqual(revision_service_complaint(revision), {})

    def test_bounded_pending_task_overflow_preserves_hold_instead_of_ignoring_older_case(self):
        revision = self.revision()
        self.task(revision)
        self.message("Thank you for explaining")
        for _ in range(20):
            IgFollowUpTask.objects.create(client=self.customer, kind="manager_task", reason=TASK_REASON,
                status="skipped", due_at=self.now, manager_context={"service_complaint": {"revision_id": -1}})
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_recent_source_overflow_cannot_hide_an_older_unowned_complaint(self):
        for _ in range(33):
            self.message("Thank you")
        self.assertEqual(self.guard(), HOLD_REASON)

    def test_many_task_proofs_are_batched_with_a_fixed_read_only_query_budget(self):
        revision = self.revision()
        for _ in range(20):
            self.task(revision, status="completed")
        self.message("Thank you")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.guard(), HOLD_REASON)
        self.assertLessEqual(len(queries), 9)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class ServiceComplaintCanonicalResolutionTests(TransactionTestCase):
    """Resolution authority comes from the actual revision manager-case writer."""
    reset_sequences = True
    setUp = revision_fixture_module.RevisionLiveTests.setUp
    _prepare = revision_fixture_module.RevisionLiveTests._prepare
    _generate = revision_fixture_module.RevisionLiveTests._generate
    _execute = revision_fixture_module.RevisionLiveTests._execute
    _replace_bundle = revision_fixture_module.RevisionLiveTests._replace_bundle

    def test_actual_canonical_owner_allows_exact_resolution_only(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        self.parsed = {"reply_text": "Пропонуємо бонус 10% на наступне замовлення.", "controls": []}
        outcome, _, http = self._execute()
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        self.assertEqual(http.call_count, 1)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["manager_handoff"]
        task = IgFollowUpTask.objects.get(pk=receipt["task_id"])
        self.assertEqual(promotion_service_hold_reason(self.customer), HOLD_REASON)
        task.status = "completed"
        task.save(update_fields=["status"])
        self.assertEqual(promotion_service_hold_reason(self.customer), "")
        task.status = "cancelled"
        task.save(update_fields=["status"])
        self.assertEqual(promotion_service_hold_reason(self.customer), "")
        self._message("My parcel never arrived", "separate-new-complaint")
        self.assertEqual(promotion_service_hold_reason(self.customer), HOLD_REASON)

    def test_actual_canonical_old_case_does_not_block_a_new_reset_forever(self):
        self._replace_bundle([ACTUAL_DELIVERY_COMPLAINT])
        self.parsed = {"reply_text": "Пропонуємо бонус 10% на наступне замовлення.", "controls": []}
        outcome, _, _ = self._execute()
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        self.assertEqual(promotion_service_hold_reason(self.customer), HOLD_REASON)
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk,
            reason="explicit new complaint scope")
        self.assertEqual(promotion_service_hold_reason(self.customer), "")

    def _message(self, text, mid):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            mid=mid, provider_namespace="instagram_login:owner-1", role="user", source="webhook",
            text=text, provider_created_at=timezone.now(), status="pending")
