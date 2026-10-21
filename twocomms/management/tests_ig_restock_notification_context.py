"""Native ALL notification checkpoints agree in dedicated and shared readers."""
from copy import deepcopy
from collections import Counter
from datetime import timedelta
import hashlib
import json
import re
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from management.services import ig_restock_subscriptions as restock
from management.services import ig_restock_waiting_list as waiting
from management.services.ig_client_state_card import assemble_client_state, render_client_state_prompt
from management.services.ig_restock_notification_projection import notification_projection
from management.services.ig_restock_waiting_list_context import SUMMARY, capture_waiting_list_context
from management.tests_ig_restock_waiting_list import RestockWaitingListFixture


def _query_diagnostics(queries):
    """Compact failure evidence without dumping SQL literals or long columns."""
    tables, groups = Counter(), {}
    for query in queries:
        sql = query["sql"]
        referenced = tuple(re.findall(r'\b(?:FROM|JOIN)\s+["`]?([a-zA-Z_][\w.]*)', sql, re.I))
        tables.update(referenced)
        normalized = re.sub(r"'(?:''|[^'])*'", "?", sql)
        normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "?", normalized)
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:12]
        group = groups.setdefault(fingerprint, {"digest": fingerprint, "count": 0,
            "seconds": 0.0, "tables": referenced})
        group["count"] += 1
        group["seconds"] += float(query.get("time", 0) or 0)
    return {"count": len(queries), "tables": dict(tables.most_common()), "patterns": sorted(
        groups.values(), key=lambda row: (-row["count"], -row["seconds"], row["digest"]))[:12]}


@override_settings(IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class NativeRestockNotificationContextTests(RestockWaitingListFixture, TransactionTestCase):
    reset_sequences = True

    def notification_case(self, *, missing_history=False, unknown=False, native_ack=False):
        if native_ack:
            case = self.list_invitation(mixed=True)
            answer, ack_mid = self.native_click(case["invitation"], action="accept",
                reply_to=restock.Receipt.objects.get(invitation=case["invitation"]).provider_message_id,
                at=self.now + timedelta(seconds=1))
            case["subscription"] = restock.Subscription.objects.get(accepted_answer__source=answer)
            case["ack_mid"] = ack_mid
        else:
            case = self.accepted_list(mixed=True)
        _request, decision = self.physically_check(case)
        self.assertTrue(decision["ready"], decision)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))["created"], 1)
        if missing_history:
            with patch("management.services.ig_restock_subscription_delivery._history", return_value=False):
                count, physical = self.dispatch()
        else:
            count, physical = self.dispatch(unknown=unknown)
        self.assertEqual((count, len(physical)), (0 if unknown else 1, 1))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "unknown" if unknown else "sent")
        return case

    def native_click(self, invitation, *, action, reply_to, at):
        from management.services.ig_revision_live import execute_claimed_revision
        self.settings.ai_enabled = True
        self.settings.save(update_fields=["ai_enabled"])
        source = self.source(invitation.button_title if action == "accept" else "Скасувати",
            payload=invitation.accept_payload if action == "accept" else invitation.revoke_payload, at=at)
        source.reply_to_provider_message_id = reply_to
        source.save(update_fields=["reply_to_provider_message_id"])
        revision, token = self.revision(source)
        physical = []

        def provider(_settings, _url, *, data, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            physical.append(json.loads(data))
            return 200, json.dumps({"message_id": f"notification-context-{action}-{source.pk}-{len(physical)}"})

        with patch("django.utils.timezone.now", return_value=at), \
                patch("management.services.instagram_bot.get_page_token", return_value="synthetic-notification-context-token"), \
                patch("management.services.instagram_bot._provider_http", side_effect=provider), \
                patch("management.services.call_ai_analysis.gemini_generate_text",
                    side_effect=AssertionError("Native RS1 must bypass Gemini")):
            outcome = execute_claimed_revision(revision.pk, token, self.settings)
        self.assertEqual(outcome.state, "completed", outcome.reasons)
        self.assertEqual(len(physical), 1)
        return source, f"notification-context-{action}-{source.pk}-1"

    def loaded_graph(self, case):
        subscription = restock.Subscription.objects.select_related(
            "accepted_answer__invitation", "availability_event__task__sent_message").get(
                pk=case["subscription"].pk)
        accepted = subscription.accepted_answer
        event = subscription.availability_event
        task = event.task
        return {"invitation": accepted.invitation, "accepted": accepted, "subscription": subscription,
            "event": event, "task": task, "history": task.sent_message,
            "source_reason": "", "accepted_valid": True, "receipt_valid": True,
            "now": self.now + timedelta(seconds=5)}

    def pure_projection(self, graph):
        with self.assertNumQueries(0):
            return notification_projection(**graph)

    def assert_unconfirmed(self, graph):
        projected = self.pure_projection(graph)
        self.assertEqual(projected["state"], "unknown", projected)
        self.assertFalse(projected["native_receipt_confirmed"], projected)
        self.assertIsNone(projected["sent_at"], projected)

    def boundary(self, source, now):
        return {"client_id": self.client_row.pk, "episode_id": self.client_row.current_commercial_episode_id,
            "order_id": None, "line_id": "new-cart", "recipient_id": "self", "reset_floor": 1,
            "source_namespace": source.provider_namespace, "reset_id": None, "erasure_epoch": "",
            "source_watermark": {"message_id": source.pk, "event_at": now.isoformat()}}

    def assert_reader_parity(self, case, *, now=None, source=None, agreement="accepted"):
        now = now or self.now + timedelta(seconds=5)
        source = source or case["subscription"].accepted_answer.source
        boundary = self.boundary(source, now)
        with patch("django.utils.timezone.now", return_value=now), \
                patch("management.services.instagram_bot._provider_http") as provider, \
                patch("management.services.call_ai_analysis.gemini_generate_text") as gemini:
            with CaptureQueriesContext(connection) as dedicated_reads:
                dedicated = waiting.waiting_list_projection(self.client_row.pk, now=now)
            with CaptureQueriesContext(connection) as shared_reads:
                shared = waiting.waiting_list_projection(self.client_row.pk, now=now, limit=1,
                    include_manager_review=False, max_read_queries=24)
            self.assertLessEqual(len(shared_reads), 24)
            self.assertLessEqual(shared["diagnostics"]["read_queries"], 24)
            slots = capture_waiting_list_context(self.client_row.pk, boundary=boundary, captured_at=now)
        provider.assert_not_called()
        gemini.assert_not_called()
        diagnostics = {"dedicated": dedicated, "dedicated_sql": _query_diagnostics(dedicated_reads),
            "shared_sql": _query_diagnostics(shared_reads)}
        self.assertEqual(len(dedicated["lists"]), 1, diagnostics)
        self.assertEqual(len(shared["lists"]), 1, shared)
        self.assertEqual(dedicated["lists"][0]["notification"], shared["lists"][0]["notification"], diagnostics)
        notice = shared["lists"][0]["notification"]
        self.assertEqual(notice["state"], "sent", shared)
        self.assertTrue(notice["native_receipt_confirmed"], shared)
        self.assertEqual(shared["lists"][0]["agreement"]["state"], agreement)
        summary = slots[SUMMARY]["value"]["lists"][0]
        self.assertEqual((summary["state"], summary["notification"], summary["sent_at"]),
            (agreement, "sent", notice["sent_at"]))
        self.assertEqual(summary["physical"], "not_captured")
        self.assertEqual(summary["all_ready"], "unverified")
        self.assertEqual(summary["channel"], "not_evaluated")
        state = assemble_client_state(boundary=boundary, components={"slots": slots}, captured_at=now)
        prompt = render_client_state_prompt(state, budget=2400)
        self.assertFalse(prompt.oversized, prompt.as_dict())
        self.assertIn(SUMMARY, prompt.included)
        self.assertIn('"notification":"sent"', prompt.text)
        for secret in ("RS1:", "provider_message_id", "envelope_hmac"):
            self.assertNotIn(secret, json.dumps(shared))
        return notice

    def test_real_native_notification_reaches_dedicated_shared_and_admin_prompt(self):
        from management.services.ig_admin_state_capture import current_admin_state
        case = self.notification_case()
        graph = self.loaded_graph(case)
        self.assertTrue(restock._valid_answer(graph["accepted"], self.settings))
        self.assertTrue(restock._valid_receipt(graph["invitation"], graph["accepted"].receipt))
        expected = self.pure_projection(graph)
        self.assertEqual(expected["state"], "sent", expected)
        self.assertEqual(self.assert_reader_parity(case), expected)
        with patch("django.utils.timezone.now", return_value=graph["now"]), \
                patch("management.services.instagram_bot._provider_http") as provider, \
                patch("management.services.call_ai_analysis.gemini_generate_text") as gemini:
            admin = current_admin_state(self.client_row.pk, now=graph["now"])
        provider.assert_not_called()
        gemini.assert_not_called()
        self.assertEqual(admin.status, "captured", admin.as_dict())
        self.assertLessEqual(admin.read_queries, 96)
        summary = admin.state.as_dict()["slots"][SUMMARY]["value"]["lists"][0]
        self.assertEqual((summary["notification"], summary["sent_at"]), ("sent", expected["sent_at"]))
        prompt = render_client_state_prompt(admin.state, budget=2400)
        self.assertFalse(prompt.oversized, prompt.as_dict())
        self.assertIn(SUMMARY, prompt.included)
        self.assertIn('"notification":"sent"', prompt.text)

    def test_native_checkpoint_survives_interrupted_crm_history_without_new_send(self):
        case = self.notification_case(missing_history=True)
        graph = self.loaded_graph(case)
        self.assertIsNone(graph["history"])
        self.assertIsNone(graph["task"].sent_message_id)
        self.assertEqual(self.pure_projection(graph)["state"], "sent")
        self.assert_reader_parity(case)

    def test_optional_manager_read_failure_retains_proven_notification_and_stops_iteration(self):
        from management.services import ig_restock_waiting_list_review as review
        case = self.notification_case()
        graph = self.loaded_graph(case)
        expected = self.pure_projection(graph)
        self.assertEqual(expected["state"], "sent")
        class UnreadTail:
            def __getattribute__(self, name):
                raise AssertionError("Optional review failure must stop before the next invitation")
        original_filter = restock.Invitation.objects.filter
        with patch("management.services.instagram_bot._provider_http") as provider, \
                patch("management.services.call_ai_analysis.gemini_generate_text") as gemini, \
                patch.object(review, "projection", side_effect=waiting.WaitingListReadError(
                    "waiting_list_read_budget_exceeded")) as optional_review, \
                patch.object(restock.Invitation.objects, "filter") as invitations:
            def select_invitations(*args, **kwargs):
                if kwargs.get("target__schema") == waiting.SCHEMA:
                    # Only the enumeration has a sentinel tail. Every source,
                    # consent and native notification proof is the real graph.
                    enumerated = invitations.return_value
                    enumerated.annotate.return_value.select_related.return_value.order_by.return_value = [
                        graph["invitation"], UnreadTail()]
                    return enumerated
                return original_filter(*args, **kwargs)
            invitations.side_effect = select_invitations
            with CaptureQueriesContext(connection) as reads:
                projected = waiting.waiting_list_projection(self.client_row.pk, now=graph["now"], limit=2)
        provider.assert_not_called()
        gemini.assert_not_called()
        optional_review.assert_called_once()
        self.assertFalse(projected["coverage_complete"], projected)
        self.assertEqual(projected["reason"], "waiting_list_read_budget_exceeded", projected)
        self.assertTrue(projected["diagnostics"]["read_only"], projected)
        self.assertTrue(all(row["sql"].lstrip().upper().startswith("SELECT") for row in reads))
        self.assertEqual(len(projected["lists"]), 1, projected)
        row = projected["lists"][0]
        self.assertEqual(row["notification"], expected)
        self.assertEqual(row["agreement"]["state"], "accepted")
        self.assertTrue(row["agreement"]["invitation_receipt_confirmed"])
        self.assertEqual([ref["id"] for ref in row["source_refs"]],
            [graph["invitation"].request_source_id, graph["accepted"].source_id])
        self.assertEqual(row["manager_review"], {"ready": False, "request_id": None,
            "request_digest": None, "state": "unverified", "checked_at": None,
            "all_ready": False, "lines": []})

    def test_present_foreign_or_changed_history_cannot_lend_a_native_receipt(self):
        graph = self.loaded_graph(self.notification_case())
        self.assertIsNotNone(graph["history"])
        mutations = {"client_id": graph["history"].client_id + 1000, "sender_id": "foreign-recipient",
            "role": "manager", "source": "webhook", "status": "failed", "text": "Different notification",
            "provider_message_id": "foreign-mid", "provider_namespace": "instagram_login:foreign-owner"}
        for field, value in mutations.items():
            with self.subTest(field=field):
                corrupt = deepcopy(graph)
                setattr(corrupt["history"], field, value)
                self.assert_unconfirmed(corrupt)

    def test_real_unknown_delivery_never_becomes_sent_from_a_convenient_mid(self):
        case = self.notification_case(unknown=True)
        graph = self.loaded_graph(case)
        self.assert_unconfirmed(graph)
        graph["task"].provider_message_id = "plausible-but-unproven-mid"
        self.assert_unconfirmed(graph)
        with patch("management.services.instagram_bot._provider_http") as provider:
            projected = waiting.waiting_list_projection(self.client_row.pk, now=graph["now"],
                include_manager_review=False, max_read_queries=24)
        provider.assert_not_called()
        self.assertEqual(projected["lists"][0]["notification"]["state"], "unknown", projected)

    def test_loaded_relation_identity_is_required_without_lazy_sql(self):
        graph = self.loaded_graph(self.notification_case())
        for owner, relation in (("subscription", "availability_event"), ("event", "task"), ("task", "sent_message")):
            with self.subTest(owner=owner, relation=relation):
                corrupt = deepcopy(graph)
                corrupt[owner]._state.fields_cache.pop(relation)
                self.assert_unconfirmed(corrupt)
        corrupt = deepcopy(graph)
        corrupt["task"]._state.fields_cache["sent_message"] = deepcopy(corrupt["history"])
        self.assert_unconfirmed(corrupt)
        deferred = restock.Subscription.objects.only("id", "state", "last_reason").get(pk=graph["subscription"].pk)
        self.assert_unconfirmed({**graph, "subscription": deferred})

    def test_source_answer_receipt_and_signed_target_failures_are_unconfirmed(self):
        graph = self.loaded_graph(self.notification_case())
        for verdict, value in (("source_reason", "original_source_changed"), ("accepted_valid", False), ("receipt_valid", False)):
            with self.subTest(verdict=verdict):
                self.assert_unconfirmed({**graph, verdict: value})
        for name in ("invitation", "accepted", "event"):
            with self.subTest(signature=name):
                corrupt = deepcopy(graph)
                corrupt[name].envelope_hmac = "0" * 64
                self.assert_unconfirmed(corrupt)
        corrupt = deepcopy(graph)
        event = corrupt["event"]
        event.envelope["availability"]["target"]["items"][0]["quantity"] += 1
        event.envelope_hmac = restock._sign("event", event.envelope, event.key_id)
        self.assertTrue(restock._verified(event, "event"))
        self.assert_unconfirmed(corrupt)

    def test_wrong_purpose_or_incomplete_native_checkpoint_is_unconfirmed(self):
        graph = self.loaded_graph(self.notification_case())
        mutations = {"reason": "another-purpose", "kind": "manager_task", "trigger": "manual",
            "policy_version": "unknown.v1", "event_key": "foreign-event", "status": "ambiguous",
            "provider_message_id": "", "claim_token": "still-processing", "claim_until": graph["now"],
            "attempt_count": 2, "message_text": "Different exact list", "event_payload": {"purpose": "marketing"}}
        for field, value in mutations.items():
            with self.subTest(field=field):
                corrupt = deepcopy(graph)
                setattr(corrupt["task"], field, value)
                self.assert_unconfirmed(corrupt)

    def test_inverted_future_or_naive_native_times_are_unconfirmed(self):
        graph = self.loaded_graph(self.notification_case())
        for at in (graph["accepted"].answered_at - timedelta(seconds=1),
                graph["now"] + timedelta(seconds=1), graph["task"].sent_at.replace(tzinfo=None), None):
            with self.subTest(sent_at=at):
                corrupt = deepcopy(graph)
                corrupt["task"].sent_at = at
                self.assert_unconfirmed(corrupt)
        self.assert_unconfirmed({**graph, "now": graph["now"].replace(tzinfo=None)})

    def test_native_revocation_after_delivery_preserves_past_sent_in_both_readers(self):
        case = self.notification_case(native_ack=True)
        source, _mid = self.native_click(case["invitation"], action="revoke", reply_to=case["ack_mid"],
            at=self.now + timedelta(seconds=4))
        case["subscription"].refresh_from_db()
        self.assertEqual(case["subscription"].state, "sent")
        self.assert_reader_parity(case, source=source, agreement="revoked")

    def test_expired_agreement_preserves_historical_notification_without_current_window(self):
        case = self.notification_case()
        later = case["invitation"].expires_at + timedelta(seconds=1)
        self.assert_reader_parity(case, now=later, agreement="expired")

    def test_later_cart_stock_and_manager_revocation_preserve_only_historical_delivery(self):
        from management.services.ig_commerce_projection import capture_current_selection_lines
        case = self.notification_case()
        original = deepcopy(case["invitation"].target)
        source = self.canonical("измените позицию 2, количество 2", at=self.now + timedelta(seconds=4))
        cart = capture_current_selection_lines(self.client_row.pk, now=self.now + timedelta(seconds=5))
        self.assertEqual(cart["status"], "captured", cart)
        self.assertEqual([line["fields"]["quantity"]["value"] for line in cart["lines"]], [1, 2], cart)
        self.variant.stock = 9
        self.variant.save(update_fields=["stock"])
        self.rule.stock = 9
        self.rule.save(update_fields=["stock"])
        self.manager.is_active = False
        self.manager.save(update_fields=["is_active"])
        self.assert_reader_parity(case, source=source)
        case["invitation"].refresh_from_db()
        self.assertEqual(case["invitation"].target, original)
