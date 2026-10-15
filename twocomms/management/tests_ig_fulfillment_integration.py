"""Real source, authenticated manager, frozen context and physical SQL guards.

No catalogue/source/money/permission authority is replaced. Restock evidence
uses the existing native outbox fixture; only Gemini's physical call is mocked.
"""
from copy import deepcopy
from datetime import timedelta
import json
import os
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import Client, RequestFactory, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from management.ig_fulfillment_review_models import (
    IgFulfillmentReviewRequest as Request, IgFulfillmentReviewDecision as Decision,
)
from management.ig_restock_subscription_models import (
    IgRestockInvitation, IgRestockReceipt, IgRestockAnswer, IgRestockSubscription,
    IgRestockAvailabilityEvent,
)
from management.models import IgBotNotification, IgClient, IgFollowUpTask, InstagramBotMessage
from management.services import ig_fulfillment_reviews as review
from management.services import ig_restock_subscriptions as restock
from management.services.ig_admin_state_capture import current_admin_state
from management.services.ig_client_state_card import assemble_client_state
from management.services.ig_fulfillment_context import (
    SLOT, fulfillment_slot, retained_fulfillment_frame, validate_retained_fulfillment,
)
from management.services.ig_fulfillment_privacy import purge_fulfillment_data
from management.services.ig_reply_authority import build_reply_truth_context
from management.services.ig_reply_truth import validate_reply_truth
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    IG_TURN_CONTEXT_MODE="unified", IG_CLIENT_STATE_PROMPT_TOKENS=20000,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test",
    ROOT_URLCONF="management.urls")
class FulfillmentNativeIntegrationTests(_RestockFixture, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        self.manager = get_user_model().objects.create_superuser(
            username="fulfillment-integration-manager", email="manager@example.test", password="test")
        self.client = Client(HTTP_HOST="management.twocomms.shop")
        self.client.force_login(self.manager)

    def endpoint(self, owner=None):
        return reverse("management_bot_fulfillment_review_api", args=[(owner or self.client_row).pk])

    def case(self, text="Потрібен подарунок до 20.10.2026. Встигнемо?", *, at=None):
        source = self.source(text, at=at)
        revision, token = self.revision(source)
        result = review.prepare_fulfillment_review(revision, settings_obj=self.settings,
            now=source.provider_created_at)
        self.assertTrue(result["ready"], result)
        return Request.objects.get(pk=result["request_id"]), revision, token

    def decision_payload(self, request):
        return {"request_id": str(request.pk), "expected_request_digest": request.envelope_hmac,
            "operation_id": str(uuid.uuid4()), "decisions_by_line": [{
                "line_id": line["line_id"], "availability": "available",
                "checked_quantity": line["target"]["quantity"], "supply_basis": "accessible_unrecorded",
                "source_location": "Менеджер перевірив доступний товар поза складським обліком",
                "preparation": {"min_days": 7, "max_days": 10, "unit": "calendar"},
            } for line in request.envelope["lines"]]}

    def decide(self, request, *, at=None, payload=None):
        with patch("django.utils.timezone.now", return_value=at or self.now):
            response = self.client.post(self.endpoint(request.client),
                json.dumps(payload or self.decision_payload(request)), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"], response.json())
        return Decision.objects.get(pk=response.json()["decision_id"])

    def capture(self, *, at=None):
        result = current_admin_state(self.client_row.pk, now=at or self.now)
        self.assertEqual(result.status, "captured", result)
        return result.state

    def truth(self, state):
        return build_reply_truth_context(self.client_row, captured_client_state=state)

    def prepared(self, revision, token, *, at=None):
        from management.services.ig_revision_live import RevisionGenerationBoundary
        from management.services.ig_revision_media import collect_revision_media
        from management.services.ig_turn_integration import prepare_revision_turn_context
        collection = collect_revision_media(revision.pk, token)
        boundary = RevisionGenerationBoundary(revision, token, self.settings, self.binding, has_images=False)
        prepared = prepare_revision_turn_context(revision, generation_boundary=boundary,
            collection=collection, settings_row=self.settings, publication=self.binding, now=at or self.now)
        return prepared, boundary, collection

    def test_authenticated_decision_captures_real_case_without_minting_recorded_stock(self):
        request, _, _ = self.case()
        pending = self.capture().as_dict()["slots"][SLOT]
        self.assertEqual(pending["status"], "confirmed", pending)
        self.assertEqual(pending["value"]["status"], "pending")
        decision = self.decide(request)
        slot = self.capture().as_dict()["slots"][SLOT]
        self.assertEqual(slot["status"], "confirmed", slot)
        self.assertEqual(slot["value"]["decision"]["id"], str(decision.pk))
        self.assertTrue(slot["value"]["customer_requirements"][0]["gift"])
        self.assertEqual(slot["value"]["decision"]["lines"][0]["preparation"]["max_days"], 10)
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual((self.rule.stock, self.variant.stock), (0, 0))
        request.task.refresh_from_db()
        self.assertTrue(request.task.manager_context["business_debt_open"])
        context = self.truth(self.capture())
        self.assertIsNone(context.explicitly_qualified_standard_dispatch_days)
        self.assertFalse(validate_reply_truth("Зазвичай відправляємо після оплати за 1–3 дні.", context=context).valid)
        self.assertFalse(validate_reply_truth("Доставка за 7–10 днів.", context=context).valid)

    def test_native_restock_consent_is_same_source_bound_card_and_prompt_frame_within_budget(self):
        invitation, subscription = self.accepted()
        at = subscription.accepted_answer.answered_at
        with CaptureQueriesContext(connection) as queries:
            captured = current_admin_state(self.client_row.pk, now=at)
        self.assertEqual(captured.status, "captured", captured)
        self.assertLessEqual(captured.read_queries, 96)
        self.assertTrue(all(row["sql"].lstrip().split(None, 1)[0].upper() in
            {"SELECT", "SET", "START", "BEGIN", "SAVEPOINT", "RELEASE", "COMMIT", "ROLLBACK"} for row in queries))
        slot = captured.state.as_dict()["slots"][SLOT]
        interest = slot["value"]["restock_interest"]
        self.assertEqual(interest["status"], "accepted", slot)
        refs = {ref["id"] for ref in slot["source_refs"]}
        self.assertIn(subscription.accepted_answer.source_id, refs)
        self.assertIn(invitation.request_source_id, refs)
        frame = retained_fulfillment_frame(captured.state)
        self.assertIsNotNone(frame)
        self.assertEqual(frame["slots"][SLOT]["value"]["restock_interest"], interest)

    def test_get_is_read_only_and_has_no_customer_transport_or_new_case(self):
        request, _, _ = self.case()
        before = (Request.objects.count(), Decision.objects.count(), IgFollowUpTask.objects.count())
        with CaptureQueriesContext(connection) as queries, patch(
                "management.services.instagram_bot._provider_http", side_effect=AssertionError("GET cannot send")) as http:
            response = self.client.get(self.endpoint(), {"request_id": str(request.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["review"]["current_request"]["id"], str(request.pk))
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertTrue(queries.captured_queries)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") and "FOR UPDATE" not in row["sql"].upper()
            for row in queries.captured_queries), queries.captured_queries)
        self.assertEqual(before, (Request.objects.count(), Decision.objects.count(), IgFollowUpTask.objects.count()))
        http.assert_not_called()

    def test_client_detail_names_manager_case_and_does_not_offer_generic_policy_restart(self):
        request, _, _ = self.case()
        url = reverse("management_bot_client_detail_api", args=[self.client_row.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        row = next(item for item in response.json()["followups"] if item["id"] == request.task_id)
        self.assertEqual(row["status_label"], "Потребує перевірки")
        self.assertEqual(row["reason_label"], "Перевірка наявності та строків")
        self.assertEqual(row["continue_url"], "")
        self.decide(request)
        response = self.client.get(url)
        updates = [item for item in response.json()["followups"] if item["reason"] == review.UPDATE_REASON]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["reason_label"], "Результат перевірки для клієнта")
        self.assertEqual(updates[0]["continue_url"], "")

    def test_pii_viewer_can_read_status_but_cannot_take_manager_decisions(self):
        from django.contrib.auth.models import Permission
        request, _, _ = self.case()
        viewer = get_user_model().objects.create_user(username="fulfillment-status-viewer", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(content_type__app_label="management",
            codename="view_ig_conversation_pii"))
        client = Client(HTTP_HOST="management.twocomms.shop")
        client.force_login(viewer)
        self.assertEqual(client.get(self.endpoint()).status_code, 200)
        response = client.post(self.endpoint(), json.dumps(self.decision_payload(request)), content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Decision.objects.exists())

    def test_api_permission_foreign_case_csrf_and_digest_cas_do_not_write_decisions(self):
        request, _, _ = self.case()
        payload = self.decision_payload(request)
        outsider = get_user_model().objects.create_user(username="fulfillment-no-capabilities")
        unauthorized = Client(HTTP_HOST="management.twocomms.shop")
        unauthorized.force_login(outsider)
        for response in (unauthorized.get(self.endpoint()), unauthorized.post(self.endpoint(), json.dumps(payload), content_type="application/json")):
            self.assertEqual(response.status_code, 403, response.content)
        foreign = IgClient.objects.create(igsid="foreign-fulfillment-owner")
        response = self.client.post(self.endpoint(foreign), json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 404, response.content)
        strict = Client(enforce_csrf_checks=True, HTTP_HOST="management.twocomms.shop")
        strict.force_login(self.manager)
        self.assertEqual(strict.post(self.endpoint(), json.dumps(payload), content_type="application/json").status_code, 403)
        payload["expected_request_digest"] = "0" * 64
        response = self.client.post(self.endpoint(), json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["reason"], "request_digest_changed")
        self.assertFalse(Decision.objects.exists())

    def test_neutral_current_watermark_keeps_review_and_never_restores_one_to_three_days(self):
        request, _, _ = self.case()
        self.decide(request)
        before = self.capture().as_dict()
        later = self.now + timedelta(seconds=2)
        self.canonical("Дякую", at=later)
        after = self.capture(at=later).as_dict()
        self.assertNotEqual(before["source_watermark"], after["source_watermark"])
        slot = after["slots"][SLOT]
        self.assertEqual(slot["status"], "confirmed", slot)
        self.assertEqual(slot["value"]["current_request"]["id"], str(request.pk))
        self.assertEqual(slot["value"]["status"], "reviewed")
        self.assertFalse(validate_reply_truth("Обычно отправляем после оплаты за 1–3 дня.", context=self.truth(after)).valid)

    def test_unknown_exact_purchase_holds_timing_but_previous_cart_does_not(self):
        request, _, _ = self.case()
        self.decide(request)
        captured = self.capture().as_dict()
        projection = deepcopy(captured["slots"][SLOT]["value"])
        projection["current_request"]["sources"][0]["provider_created_at"] = (self.now + timedelta(seconds=1)).isoformat()
        unknown = fulfillment_slot(projection, boundary=captured["boundary"], captured_at=self.now)
        self.assertTrue(unknown["timing_review_required"])
        state = assemble_client_state(boundary=captured["boundary"], captured_at=self.now,
            components={"source_cart": captured["source_cart"], "slots": {SLOT: unknown}})
        self.assertTrue(state.as_dict()["diagnostics"]["fulfillment_timing_hold"])
        self.assertIsNone(self.truth(state).explicitly_qualified_standard_dispatch_days)
        self.canonical("розмір L", at=self.now + timedelta(seconds=2))
        next_capture = self.capture(at=self.now + timedelta(seconds=2)).as_dict()
        old = fulfillment_slot(projection, boundary=next_capture["boundary"], captured_at=self.now + timedelta(seconds=2))
        self.assertFalse(old["timing_review_required"])
        self.assertFalse(next_capture["diagnostics"].get("fulfillment_timing_hold", False))

    def test_pending_capture_stays_pending_when_manager_decides_later(self):
        request, revision, token = self.case()
        prepared, _, _ = self.prepared(revision, token)
        frame = retained_fulfillment_frame(prepared.state)
        serialized = json.dumps(frame, sort_keys=True)
        self.assertEqual(frame["slots"][SLOT]["value"]["status"], "pending")
        later = self.now + timedelta(seconds=3)
        self.decide(request, at=later)
        self.assertEqual(json.dumps(retained_fulfillment_frame(prepared.state), sort_keys=True), serialized)
        self.assertEqual(validate_retained_fulfillment(revision, frame, now=later), "")
        historical = review.fulfillment_review_projection(self.client_row.pk, now=self.now,
            current_source_cart=prepared.state.as_dict()["source_cart"])
        self.assertEqual(historical["status"], "pending")
        self.assertIsNone(historical["decision"])
        current = self.capture(at=later).as_dict()["slots"][SLOT]
        self.assertEqual(current["value"]["status"], "reviewed", current)

    def test_gemini_uses_one_captured_case_and_manifest_without_live_recapture(self):
        from management.services import instagram_bot as bot
        from management.services.call_ai_analysis import CallAIAnalysisError
        request, revision, token = self.case()
        decision = self.decide(request)
        prepared, boundary, collection = self.prepared(revision, token)
        frame = retained_fulfillment_frame(prepared.state)
        seen = []
        def physical(payload, **kwargs):
            seen.append((deepcopy(payload), deepcopy(kwargs["request_policy_manifest"])))
            raise CallAIAnalysisError("offline integration transport")
        with patch("management.services.call_ai_analysis.gemini_generate_text", side_effect=physical), patch(
                "management.services.ig_fulfillment_reviews.fulfillment_review_projection",
                side_effect=AssertionError("Gemini cannot recapture manager case")):
            bot.gemini_generate(self.settings, list(prepared.history), client=self.client_row,
                routing_decision=prepared.context.decision, generation_boundary=boundary,
                turn_note=prepared.context.turn_note, memory_note=prepared.context.memory_note,
                context_note=prepared.context.context_note, captured_client_state=prepared.state,
                captured_dynamic_notes=prepared.dynamic_notes, captured_turn_text=prepared.current_text,
                captured_policy_tags=prepared.policy_inputs.tags,
                captured_knowledge_language=prepared.policy_inputs.knowledge_language,
                request_context_metadata=prepared.request_metadata, failure_context={},
                turn_media_binding=collection.binding, turn_media_context=[],
                deadline_at=revision.overall_deadline - timedelta(seconds=5))
        self.assertEqual(len(seen), 1)
        text = seen[0][0]["system_instruction"]["parts"][0]["text"]
        self.assertIn(str(request.pk), text)
        self.assertIn(str(decision.pk), text)
        self.assertIn('"max_days":10', text.replace(" ", ""))
        manifest = seen[0][1]["request_context"]["fulfillment_review_snapshot"]
        self.assertEqual(manifest["decision_version"], 1)
        self.assertEqual(manifest["capture_digest"], frame["slots"][SLOT]["value"]["capture_digest"])
        self.assertEqual(retained_fulfillment_frame(prepared.state), frame)

    def deliver_manager_card(self, request):
        from management.services import instagram_bot as bot
        notification = IgBotNotification.objects.get(dedupe_key=f"ig-fulfillment-review:{request.pk}")
        with patch("management.services.instagram_bot._http", return_value=(200,
                json.dumps({"ok": True, "result": {"message_id": 9401}}))) as physical:
            self.assertTrue(bot._deliver_manager_notification(notification.dedupe_key))
        physical.assert_called_once()
        notification.refresh_from_db()
        self.assertEqual(notification.status, "sent")
        self.assertIsNotNone(notification.sent_at)
        self.assertEqual(str(notification.payload["main_delivery_message_id"]), "9401")
        return notification

    def manager_callback(self, request, action, *, message_id=9401, callback_id="native-fulfillment-callback"):
        from accounts.models import UserProfile
        from management.views import management_bot_webhook
        profile, _ = UserProfile.objects.get_or_create(user=self.manager)
        profile.tg_manager_chat_id = 777
        profile.save(update_fields=["tg_manager_chat_id"])
        payload = {"callback_query": {"id": callback_id,
            "data": review.build_fulfillment_callback(request, action), "from": {"id": 777},
            "message": {"chat": {"id": 777, "type": "private"}, "message_id": message_id}}}
        inbound = RequestFactory().post("/management/tg-manager/webhook/synthetic-fulfillment-token/",
            json.dumps(payload), content_type="application/json")
        with patch("management.views.requests.post", return_value=SimpleNamespace(json=lambda: {"ok": True})) as physical:
            response = management_bot_webhook(inbound, "synthetic-fulfillment-token")
        self.assertEqual(response.status_code, 200, response.content)
        physical.assert_called_once()
        return json.loads(response.content), physical.call_args.kwargs["json"]["text"]

    @patch.dict(os.environ, {"MANAGEMENT_TG_BOT_TOKEN": "synthetic-fulfillment-token",
        "MANAGEMENT_TG_ADMIN_CHAT_ID": "777", "MANAGER_TG_BOT_TOKEN": ""})
    def test_actual_manager_card_form_decision_native_update_and_history_recovery_send_once(self):
        from management.services.ig_fulfillment_delivery import process_due_fulfillment_updates
        request, _, _ = self.case()
        self.deliver_manager_card(request)
        result, reply = self.manager_callback(request, "7")
        self.assertTrue(result["ready"], result)
        self.assertIn("картці клієнта", reply)
        self.assertFalse(Decision.objects.exists(), "The seven-day preset cannot guess a count or production/dispatch phase")
        self.decide(request)
        task = IgFollowUpTask.objects.get(client=self.client_row, reason=review.UPDATE_REASON)
        admitted = review.validated_update_task(task, self.settings, now=self.now)
        self.assertTrue(admitted["ready"], admitted)
        self.assertEqual(admitted["decision"]["lines"][0]["preparation"]["max_days"], 10)
        physical_payloads, history_checkpoints = [], []
        original_create = InstagramBotMessage.objects.create
        def transport(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical_payloads.append(json.loads(data))
            return 200, json.dumps({"message_id": "native-fulfillment-manager-update"})
        def fail_history(**kwargs):
            if kwargs.get("role") == "model" and kwargs.get("source") == "followup":
                persisted = IgFollowUpTask.objects.get(pk=task.pk)
                history_checkpoints.append((persisted.status, persisted.provider_message_id, persisted.sent_message_id))
                self.assertEqual(persisted.status, "sent")
                self.assertEqual(persisted.provider_message_id, "native-fulfillment-manager-update")
                self.assertIsNone(persisted.sent_message_id)
                raise RuntimeError("retained native MID must survive history failure")
            return original_create(**kwargs)
        with patch("management.services.instagram_bot.get_page_token", return_value="synthetic-restock-test-token"), patch(
                "management.services.instagram_bot._provider_http", side_effect=transport) as physical:
            with patch.object(InstagramBotMessage.objects, "create", side_effect=fail_history):
                self.assertEqual(process_due_fulfillment_updates(self.settings, now=self.now), 1)
            task.refresh_from_db()
            self.assertEqual((task.status, task.provider_message_id, task.sent_message_id),
                ("sent", "native-fulfillment-manager-update", None))
            displayed = review.fulfillment_review_projection(self.client_row.pk, now=self.now)["customer_update"]
            self.assertEqual(displayed["state"], "sent")
            self.assertTrue(displayed["native_receipt_confirmed"])
            self.assertTrue(history_checkpoints)
            self.assertEqual(process_due_fulfillment_updates(self.settings, now=self.now + timedelta(seconds=1)), 0)
            self.assertEqual(process_due_fulfillment_updates(self.settings, now=self.now + timedelta(minutes=16)), 0)
        physical.assert_called_once()
        self.assertEqual(len(physical_payloads), 1)
        text = physical_payloads[0]["message"]["text"]
        self.assertIn("Менеджер перевірив", text)
        self.assertIn("підготовка 7–10", text)
        self.assertIn("Точний день отримання залежить від перевізника.", text)
        self.assertNotIn("1–3", text)
        self.assertNotIn("доставка 7–10", text.casefold())
        task.refresh_from_db()
        history = InstagramBotMessage.objects.get(pk=task.sent_message_id)
        self.assertEqual(history.provider_message_id, task.provider_message_id)
        self.assertEqual(history.text, text)
        self.assertEqual(InstagramBotMessage.objects.filter(client=self.client_row, role="model",
            provider_message_id=task.provider_message_id).count(), 1)
        self.rule.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual((self.rule.stock, self.variant.stock), (0, 0))
        request.task.refresh_from_db()
        self.assertTrue(request.task.manager_context["business_debt_open"])

    @patch.dict(os.environ, {"MANAGEMENT_TG_BOT_TOKEN": "synthetic-fulfillment-token",
        "MANAGEMENT_TG_ADMIN_CHAT_ID": "777", "MANAGER_TG_BOT_TOKEN": ""})
    def test_native_manager_callback_requires_its_actual_sent_telegram_message(self):
        request, _, _ = self.case()
        result, _ = self.manager_callback(request, "u")
        self.assertFalse(result["ready"], result)
        self.assertFalse(Decision.objects.exists())
        self.deliver_manager_card(request)
        result, _ = self.manager_callback(request, "u", message_id=9402, callback_id="wrong-manager-card")
        self.assertFalse(result["ready"], result)
        self.assertFalse(Decision.objects.exists())
        result, _ = self.manager_callback(request, "u", callback_id="actual-manager-card")
        self.assertTrue(result["ready"], result)
        decision = Decision.objects.get(request=request)
        self.assertEqual(decision.actor_id, self.manager.pk)
        self.assertEqual(decision.envelope["lines"][0]["availability"], "unavailable")
        self.assertIsNone(decision.envelope["lines"][0]["preparation"])
        self.assertIsNone(decision.envelope["lines"][0]["dispatch"])
        self.manager_callback(request, "u", callback_id="actual-manager-card")
        self.assertEqual(Decision.objects.filter(request=request).count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.client_row, reason=review.UPDATE_REASON).count(), 1)

    def accepted_evidence(self):
        inv = self.invitation(mid=f"native-card-{self.client_row.igsid}")
        result, source, _, _ = self.reply(inv)
        self.assertEqual(result.reason, "accept", result)
        return inv, IgRestockSubscription.objects.get(accepted_answer__source=source)

    def evidence_rows(self):
        inv, subscription = self.accepted_evidence()
        at = self.now + timedelta(seconds=2)
        from management.services.ig_restock_availability import observe_target
        observed = observe_target(inv.target, now=at).as_dict()
        with patch("django.utils.timezone.now", return_value=at):
            created = review.ensure_restock_manager_review(subscription, observed, now=at)
        self.assertIn("request_id", created, created)
        request = Request.objects.get(pk=created["request_id"])
        decision = self.decide(request, at=at)
        outcome = restock.reconcile_subscriptions(now=at)
        self.assertEqual(outcome["created"], 1, outcome)
        event = IgRestockAvailabilityEvent.objects.get(subscription=subscription)
        return [inv, inv.native_receipt, subscription.accepted_answer, event, request, decision], subscription

    @skipUnless(connection.vendor == "mysql", "Physical guard verification requires disposable native MariaDB")
    def test_six_immutable_tables_reject_physical_rewrite_delete_and_uppercase_hmac(self):
        rows, _ = self.evidence_rows()
        self.assertEqual(len(rows), 6)
        for row in rows:
            table = connection.ops.quote_name(row._meta.db_table)
            pk_col = connection.ops.quote_name(row._meta.pk.column)
            pk = row._meta.pk.get_db_prep_value(row.pk, connection)
            columns = [field.column for field in row._meta.local_concrete_fields]
            selected = ["UPPER(envelope_hmac)" if name == "envelope_hmac" else connection.ops.quote_name(name) for name in columns]
            statements = (
                f"UPDATE {table} SET envelope_hmac=envelope_hmac WHERE {pk_col}=%s",
                f"DELETE FROM {table} WHERE {pk_col}=%s",
                f"INSERT INTO {table} ({','.join(connection.ops.quote_name(name) for name in columns)}) SELECT {','.join(selected)} FROM {table} WHERE {pk_col}=%s",
            )
            for statement in statements:
                with self.subTest(model=type(row).__name__, statement=statement.split()[0]), self.assertRaisesMessage(DatabaseError, "Fulfilment immutable/source/privacy guard"):
                    with transaction.atomic(), connection.cursor() as cursor:
                        cursor.execute(statement, [pk])
            self.assertTrue(type(row).objects.filter(pk=row.pk).exists())

    @skipUnless(connection.vendor == "mysql", "Terminal state guard requires disposable native MariaDB")
    def test_terminal_subscription_cannot_be_reactivated_physically(self):
        _, subscription = self.accepted_evidence()
        IgRestockSubscription.objects.filter(pk=subscription.pk).update(state="unknown")
        with self.assertRaisesMessage(DatabaseError, "Fulfilment immutable/source/privacy guard"):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("UPDATE management_igrestocksubscription SET state='active' WHERE id=%s",
                    [subscription._meta.pk.get_db_prep_value(subscription.pk, connection)])
        subscription.refresh_from_db()
        self.assertEqual(subscription.state, "unknown")

    def test_privacy_purge_requires_fence_and_erases_only_exact_owner_before_source_cleanup(self):
        request, _, _ = self.case()
        decision = self.decide(request)
        inv, subscription = self.accepted_evidence()
        owner = self.client_row
        self.client_row = IgClient.objects.create(igsid="other-privacy-fulfillment", language="uk",
            reply_permission_epoch=3, last_user_message_at=self.now)
        self.canonical(f"добавьте {self.product.title}, количество 1, размер M, цвет black, крій classic")
        other_request, _, _ = self.case()
        other_inv, other_subscription = self.accepted_evidence()
        self.client_row = owner
        for atomic in (False, True):
            with self.subTest(atomic=atomic), self.assertRaises(ValueError):
                if atomic:
                    with transaction.atomic():
                        purge_fulfillment_data([owner.pk])
                else:
                    purge_fulfillment_data([owner.pk])
        # This committed fence is the real first phase; it is never rewound.
        IgClient.objects.filter(pk=owner.pk).update(privacy_erasure_started_at=self.now)
        with transaction.atomic():
            purge_fulfillment_data([owner.pk])
            purge_fulfillment_data([owner.pk])
        for row in (request, decision, inv, subscription):
            self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
        for row in (other_request, other_inv, other_subscription):
            self.assertTrue(type(row).objects.filter(pk=row.pk).exists())
        # Evidence no longer protects these source owners. The real privacy
        # collector owns remaining revision/task relations and their scrubbing.
        self.assertFalse(Request.objects.filter(client=owner).exists())
        self.assertFalse(IgRestockInvitation.objects.filter(client=owner).exists())
        self.assertTrue(InstagramBotMessage.objects.filter(client=owner).exists())
        from management.bot_views import _delete_direct_bot_records
        _delete_direct_bot_records(owner.igsid)
        self.assertFalse(InstagramBotMessage.objects.filter(client_id=owner.pk).exists())
        self.assertFalse(IgFollowUpTask.objects.filter(client_id=owner.pk).exists())
        self.assertTrue(Request.objects.filter(pk=other_request.pk).exists())
        self.assertTrue(IgRestockSubscription.objects.filter(pk=other_subscription.pk).exists())
