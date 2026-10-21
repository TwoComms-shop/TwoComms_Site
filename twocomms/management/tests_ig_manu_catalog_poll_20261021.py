"""Signed current poll sources through native fit, catalogue and reorder owners."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_catalog_native_delivery as catalog_fixtures
from management import tests_ig_commerce_reorder as reorder_fixtures
from management.models import (
    IgClient, IgCommerceSelectionSession, IgCommerceTurnDecision, InstagramBotMessage, InstagramBotSettings,
)
from management.services.ig_catalog_choice_actions import consume_catalog_choice, native_choice_request
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_commerce_state import apply_turn, CommerceRevisionConflict
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_ingress_source_proof import (
    bind_polled_ingress_proof, build_polled_ingress_proof, is_current_customer_source,
)


def signed_poll(case, settings_row, source):
    """Use the existing provider-page admission signer without external I/O."""
    event = {"id": source.mid, "message": source.text, "from": {"id": source.sender_id},
        "to": {"data": [{"id": settings_row.ig_user_id}]},
        "created_time": source.provider_created_at.isoformat()}
    if source.reply_to_provider_message_id:
        event["reply_to"] = {"mid": source.reply_to_provider_message_id}
    if source.quick_reply_payload:
        event["quick_reply"] = {"payload": source.quick_reply_payload}
    proof = build_polled_ingress_proof(settings_row, event, conversation_id="offline-native-poll")
    case.assertTrue(proof)
    source.source = "poll"
    source.save(update_fields=["source"])
    with transaction.atomic():
        bind_polled_ingress_proof(source, proof)
    case.assertTrue(is_current_customer_source(source, namespace=source.provider_namespace))
    return source


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativePollFitChoiceTests(TestCase):
    def setUp(self):
        self.environment = self.enterContext(patch.dict("os.environ", {"IG_PROVIDER_TRANSPORT": "instagram_login"}))
        self.settings = InstagramBotSettings.objects.create(pk=1, ig_user_id="fit-poll-owner")
        self.customer = IgClient.objects.create(igsid="fit-poll-customer")
        self.namespace = "instagram_login:fit-poll-owner"
        self.question = InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="manager", source="echo", status="done", send_state="sent", mid="fit-question",
            provider_message_id="fit-question", provider_namespace=self.namespace,
            provider_created_at=timezone.now() - timedelta(seconds=20),
            text="Класика — звична посадка, oversize — вільніша. Яка посадка вам ближча — звична чи вільніша?")

    def answer(self, mid):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="pending", text="Звична", mid=mid,
            provider_namespace=self.namespace, provider_created_at=timezone.now())

    def test_signed_current_poll_fit_answer_reaches_native_canonical_preference(self):
        from management.services.ig_fit_consultation import contextual_fit_choice
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source

        source = signed_poll(self, self.settings, self.answer("fit-poll-current"))
        original = source.text
        proof = contextual_fit_choice(self.customer, source)
        self.assertEqual((proof["fit"], proof["question_message_id"]), ("classic", self.question.pk))
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.customer, source, expected_provider_namespace=self.namespace)
        self.assertTrue(result.ready, result.reason)
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        self.assertTrue(decision.accepted, decision.result_payload)
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual(session.lines[session.active_index]["fit_option_code"], "classic")
        source.refresh_from_db()
        self.assertEqual(source.text, original)

    def test_unsigned_history_or_tampered_poll_answer_cannot_acquire_fit_proof(self):
        from management.services.ig_fit_consultation import contextual_fit_choice

        source = signed_poll(self, self.settings, self.answer("fit-poll-negative"))
        for field, value in (("ingress_proof", {}), ("source", "history"),
                ("text", "Вільніша"), ("reply_to_provider_message_id", "different-question")):
            original = getattr(source, field)
            setattr(source, field, value)
            self.assertEqual(contextual_fit_choice(self.customer, source), {}, field)
            setattr(source, field, original)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class NativePollCatalogChoiceTests(TransactionTestCase):
    setUp = catalog_fixtures.CatalogNativeDeliveryTests.setUp
    response = catalog_fixtures.CatalogNativeDeliveryTests.response
    source_row = catalog_fixtures.CatalogNativeDeliveryTests.source_row
    seal = catalog_fixtures.CatalogNativeDeliveryTests.seal
    prepare = catalog_fixtures.CatalogNativeDeliveryTests.prepare
    plan = catalog_fixtures.CatalogNativeDeliveryTests.plan
    deliver = catalog_fixtures.CatalogNativeDeliveryTests.deliver
    action_source = catalog_fixtures.CatalogNativeDeliveryTests.action_source

    def invitation(self):
        prepared, response = self.prepare()
        self.plan(prepared, response)
        _sent, outcomes = self.deliver()
        self.assertEqual(outcomes["template"].state, "sent")
        return prepared

    def test_signed_poll_postback_uses_actual_sent_invitation_and_canonical_transition_then_replays(self):
        from management.services.ig_catalog_choice_presentations import _admitted_source_locale

        prepared = self.invitation()
        source = self.action_source(prepared, product_id=self.products[0].pk)
        source.reply_to_provider_message_id = self.revision.delivery_effects.get(group="template").provider_message_id
        source.save(update_fields=["reply_to_provider_message_id"])
        signed_poll(self, self.settings, source)
        with transaction.atomic():
            result = consume_catalog_choice(self.client_row, source)
        self.assertTrue(result["ready"], result)
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        proof = decision.request_payload["source_binding"]["native_catalog_choice"]
        self.assertEqual(proof["source"]["source"], "poll")
        self.assertTrue(proof["source"]["ingress_proof_digest"])
        self.client_row.refresh_from_db()
        capture = capture_current_selection_lines(self.client_row.pk)
        self.assertEqual(capture["status"], "captured", capture)
        self.assertEqual(capture["lines"][0]["fields"]["product_id"]["value"], self.products[0].pk)
        before = deepcopy(capture)
        with transaction.atomic():
            replay = consume_catalog_choice(self.client_row, source)
        self.assertTrue(replay["ready"], replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=source).count(), 1)
        self.assertIsNotNone(native_choice_request(self.client_row, source, historical=True))
        self.assertEqual(capture_current_selection_lines(self.client_row.pk), before)
        self.seal(source)
        self.assertEqual(_admitted_source_locale(self.revision, reset_floor=1), prepared.manifest["scope"]["locale"])

    def test_unsigned_history_and_content_or_button_forgery_cannot_apply_a_catalogue_choice(self):
        prepared = self.invitation()
        for case in ("unsigned", "history", "quick_reply", "reply_to", "signature"):
            with self.subTest(case=case):
                source = self.action_source(prepared, product_id=self.products[0].pk)
                if case != "unsigned":
                    signed_poll(self, self.settings, source)
                else:
                    source.source = "poll"
                if case == "history":
                    source.source = "history"
                elif case == "quick_reply":
                    source.quick_reply_payload = self.action_source(prepared, product_id=self.products[1].pk).quick_reply_payload
                elif case == "reply_to":
                    source.reply_to_provider_message_id = "forged-reference"
                elif case == "signature":
                    source.ingress_proof = {**source.ingress_proof, "signed": source.ingress_proof["signed"] + "forged"}
                source.save(update_fields=["source", "quick_reply_payload", "reply_to_provider_message_id", "ingress_proof"])
                with transaction.atomic():
                    result = consume_catalog_choice(self.client_row, source)
                self.assertFalse(result["ready"], result)
                self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())
        self.assertFalse(IgCommerceSelectionSession.objects.filter(client=self.client_row).exists())

    def test_poll_proof_revoked_during_invitation_read_cannot_create_transition(self):
        from management.services import ig_catalog_choice_actions as choices

        prepared = self.invitation()
        source = signed_poll(self, self.settings, self.action_source(prepared, product_id=self.products[0].pk))
        original = choices._delivery

        def revoked(*args, **kwargs):
            delivered = original(*args, **kwargs)
            InstagramBotMessage.objects.filter(pk=source.pk).update(ingress_proof={})
            return delivered

        with transaction.atomic(), patch.object(choices, "_delivery", side_effect=revoked):
            result = consume_catalog_choice(self.client_row, source)
        self.assertFalse(result["ready"], result)
        self.assertEqual(result["reason"], "catalog_choice_source_scope_changed")
        self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())

    def test_poll_receipt_replay_rechecks_proof_after_invitation_read_without_rewriting_transition(self):
        from management.services import ig_catalog_choice_actions as choices
        from management.models import IgSourceActionReceipt

        prepared = self.invitation()
        source = signed_poll(self, self.settings, self.action_source(prepared, product_id=self.products[0].pk))
        self.seal(source)
        with transaction.atomic():
            admitted = consume_catalog_choice(self.client_row, source, revision_id=self.revision.pk)
        self.assertTrue(admitted["ready"], admitted)
        receipt = IgSourceActionReceipt.objects.get(source_message=source, kind="postback")
        original_outcome, digest = deepcopy(receipt.outcome), receipt.outcome_digest
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        original_transition = deepcopy(decision.transition.next_snapshot)
        original = choices._delivery

        def revoked(*args, **kwargs):
            delivered = original(*args, **kwargs)
            InstagramBotMessage.objects.filter(pk=source.pk).update(ingress_proof={})
            return delivered

        with transaction.atomic(), patch.object(choices, "_delivery", side_effect=revoked):
            replay = consume_catalog_choice(self.client_row, source, revision_id=self.revision.pk)
        self.assertFalse(replay["ready"], replay)
        self.assertEqual(replay["reason"], "catalog_choice_receipt_changed")
        receipt.refresh_from_db()
        decision.transition.refresh_from_db()
        self.assertEqual((receipt.outcome, receipt.outcome_digest), (original_outcome, digest))
        self.assertEqual(decision.transition.next_snapshot, original_transition)
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=source).count(), 1)


@override_settings(GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativePollHistoricalReorderTests(TestCase):
    setUp = reorder_fixtures.HistoricalReorderTests.setUp
    source = reorder_fixtures.HistoricalReorderTests.source
    order = reorder_fixtures.HistoricalReorderTests.order
    counts = reorder_fixtures.HistoricalReorderTests.counts

    def test_signed_poll_reorder_copies_exact_owned_item_without_copying_paid_state(self):
        order = self.order()
        source = signed_poll(self, self.settings, self.source("Хочу ещё такую же"))
        decision = apply_turn(self.customer, source, parse_turn(source.text))
        self.assertTrue(decision.accepted, decision.result_payload)
        self.customer.refresh_from_db()
        capture = capture_current_selection_lines(self.customer.pk)
        self.assertEqual(capture["status"], "captured", capture)
        self.assertEqual(capture["lines"][0]["fields"]["size"]["value"], "M")
        self.assertEqual(capture["lines"][0]["fields"]["size"]["source"]["source_message_id"], source.pk)
        self.assertIsNone(capture["scope"]["order_id"])
        self.assertIsNone(self.customer.current_commercial_episode.intended_order_id)
        counts = self.counts()
        self.assertEqual(apply_turn(self.customer, source, parse_turn(source.text)).pk, decision.pk)
        self.assertEqual(self.counts(), counts)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "paid")
        self.http.assert_not_called()

    def test_unsigned_or_modified_poll_reorder_cannot_create_a_purchase_cycle(self):
        self.order()
        for case in ("unsigned", "history", "modified"):
            with self.subTest(case=case):
                source = self.source("Хочу ещё такую же")
                if case != "unsigned":
                    signed_poll(self, self.settings, source)
                source.source = "history" if case == "history" else "poll"
                if case == "modified":
                    source.text += " из заказа DIFFERENT"
                source.save(update_fields=["source", "text"])
                before = self.counts()
                with self.assertRaises(CommerceRevisionConflict):
                    apply_turn(self.customer, source, parse_turn(source.text))
                self.assertEqual(self.counts(), before)
