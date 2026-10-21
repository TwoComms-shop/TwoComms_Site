"""Purpose ownership accepts authenticated current poll ingress, never its label."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from unittest import skipUnless
import uuid

from django.db import DatabaseError, connection, transaction
from django.test import TestCase, override_settings

from management.models import IgCommerceTurnDecision, IgFunnelResetAudit
from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
from management.ig_restock_subscription_models import IgRestockSubscription
from management.services.ig_ingress_source_proof import bind_polled_ingress_proof, build_polled_ingress_proof
from management.services.ig_purpose_sources import owned_source
from management.services import ig_fulfillment_reviews as fulfillment
from management.services import ig_restock_subscriptions as restock
from management.tests_ig_restock_subscriptions import _RestockFixture


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class SignedPollPurposeTests(_RestockFixture, TestCase):
    def signed_poll(self, text, *, payload="", at=None):
        source = self.source(text, payload=payload, at=at)
        event = {"id": source.mid, "message": source.text,
            "from": {"id": source.sender_id}, "to": {"data": [{"id": self.settings.ig_user_id}]},
            "created_time": source.provider_created_at.isoformat(), "quick_reply": {"payload": payload}}
        proof = build_polled_ingress_proof(self.settings, event, conversation_id="native-purpose-poll", now=self.now)
        self.assertTrue(proof)
        source.source = "poll"
        source.save(update_fields=["source"])
        with transaction.atomic():
            bind_polled_ingress_proof(source, proof)
        return source

    def poll_goods_selection(self):
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from management.services.ig_commerce_projection import source_preferences_for
        source = self.signed_poll(f"replace first position with https://twocomms.shop/product/{self.product.slug}/ quantity 1 size M color black fit classic")
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.client_row, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        decision = source.commerce_turn_decision
        self.assertTrue(decision.accepted, (decision.request_payload, decision.result_payload))
        self.client_row.refresh_from_db()
        source.status = "done"
        source.save(update_fields=["status"])
        preferences = source_preferences_for(self.client_row)
        self.assertTrue(preferences)
        self.assertEqual({preferences["evidence"][key]["source_message_id"]
            for key in ("product_id", "color", "size", "fit_option_code", "quantity")}, {source.pk})
        return source, preferences

    def test_signed_poll_sealed_revision_creates_actual_fulfillment_review_without_provider_io(self):
        source = self.signed_poll("I want this shirt for my boyfriend. I leave tomorrow at 10 AM.")
        revision, _ = self.revision(source)
        owned, reason = owned_source(revision, source, self.settings)
        self.assertEqual(reason, "")
        self.assertEqual(owned[1].pk, source.pk)
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = fulfillment.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        provider.assert_not_called()
        self.assertTrue(result["ready"], result)
        request = IgFulfillmentReviewRequest.objects.get(pk=result["request_id"])
        self.assertIn(source.pk, [row["message_id"] for row in request.envelope["sources"]])
        requirement = next(row for row in request.envelope["customer_requirements"] if row["source_id"] == source.pk)
        self.assertEqual(requirement["recipient_hint"], "boyfriend")
        self.assertEqual(requirement["deadline_kind"], "departure")
        self.assertTrue(requirement["deadline_hard"])
        self.assertEqual(request.envelope["client_epoch"], 3)

    def test_poll_label_unsigned_forged_and_history_sources_cannot_borrow_sealed_revision(self):
        source = self.signed_poll("Can you dispatch this shirt tomorrow?")
        revision, _ = self.revision(source)
        original = {"ingress_proof": deepcopy(source.ingress_proof), "source": source.source,
            "text": source.text, "sender_id": source.sender_id, "provider_namespace": source.provider_namespace,
            "provider_created_at": source.provider_created_at, "role": source.role, "status": source.status}
        for changes in ({"ingress_proof": {}},
                {"ingress_proof": {**original["ingress_proof"], "signed": original["ingress_proof"]["signed"] + "forged"}},
                {"source": "poll_history"}, {"source": "history"}, {"role": "manager"},
                {"sender_id": "foreign-customer"}, {"provider_namespace": "instagram_login:foreign-owner"},
                {"text": "I consent to something else"},
                {"provider_created_at": source.provider_created_at - timedelta(days=5)}, {"status": "failed"}):
            with self.subTest(changes=changes):
                for key, value in {**original, **changes}.items():
                    setattr(source, key, value)
                source.save(update_fields=list(original))
                owned, reason = owned_source(revision, source, self.settings)
                self.assertIsNone(owned)
                self.assertIn(reason, {"source_not_owned", "source_changed"})

    def test_signed_poll_still_requires_current_epoch_reset_and_immutable_sealed_content(self):
        source = self.signed_poll("Can you dispatch this shirt tomorrow?")
        revision, _ = self.revision(source)
        self.client_row.reply_permission_epoch += 1
        self.client_row.save(update_fields=["reply_permission_epoch"])
        self.assertEqual(owned_source(revision, source, self.settings)[1], "source_changed")
        self.client_row.reply_permission_epoch -= 1
        self.client_row.save(update_fields=["reply_permission_epoch"])
        reset = IgFunnelResetAudit.objects.create(client=self.client_row, reset_after_message_id=source.pk,
            reason="native-purpose-poll-reset")
        self.assertEqual(owned_source(revision, source, self.settings)[1], "source_changed")
        reset.delete()
        snapshot = deepcopy(revision.bundle_snapshot)
        snapshot["sources"][0]["text"] = "fabricated frozen claim"
        revision.bundle_snapshot = snapshot
        with self.assertRaisesMessage(ValueError, "sealed turn revision snapshot is immutable"):
            revision.save(update_fields=["bundle_snapshot"])
        self.assertEqual(owned_source(revision, source, self.settings)[1], "")

    def test_signed_poll_native_restock_acceptance_preserves_actual_consent(self):
        invitation = self.invitation()
        source = self.signed_poll(invitation.button_title, payload=invitation.accept_payload,
            at=self.now + timedelta(seconds=1))
        revision, _ = self.revision(source)
        with patch("management.services.instagram_bot._provider_http") as provider:
            result = restock.handle_reply(source, revision=revision, settings_obj=self.settings,
                now=source.provider_created_at)
        provider.assert_not_called()
        self.assertEqual(result.reason, "accept", result)
        subscription = IgRestockSubscription.objects.get(accepted_answer__source=source)
        self.assertEqual(subscription.accepted_answer.envelope["source"]["message_id"], source.pk)
        self.assertEqual(subscription.accepted_answer.kind, "accept")

    def test_signed_poll_goods_reach_native_invitation_consent_and_subscription(self):
        selection, preferences = self.poll_goods_selection()
        self.assertIsNotNone(restock._selection_sources(preferences, self.client_row, selection.provider_namespace))
        with patch("management.services.instagram_bot._provider_http") as provider:
            invitation = self.invitation()
            answer = self.signed_poll(invitation.button_title, payload=invitation.accept_payload,
                at=self.now + timedelta(seconds=1))
            revision, _ = self.revision(answer)
            result = restock.handle_reply(answer, revision=revision, settings_obj=self.settings,
                now=answer.provider_created_at)
        provider.assert_not_called()
        self.assertEqual(result.reason, "accept", result)
        subscription = IgRestockSubscription.objects.get(accepted_answer__source=answer)
        self.assertEqual(subscription.accepted_answer.invitation_id, invitation.pk)
        sources = invitation.envelope["selection_sources"]["sources"]
        self.assertEqual([row["id"] for row in sources], [selection.pk])

    def test_unsigned_or_historical_goods_selection_never_authorizes_native_invitation(self):
        source, preferences = self.poll_goods_selection()
        proof = deepcopy(source.ingress_proof)
        for changes in ({"source": "poll", "ingress_proof": {}},
                {"source": "poll", "ingress_proof": {**proof, "signed": proof["signed"] + "forged"}},
                {"source": "poll_history", "ingress_proof": proof}):
            with self.subTest(source=changes["source"]):
                for key, value in changes.items():
                    setattr(source, key, value)
                source.save(update_fields=list(changes))
                self.assertIsNone(restock._selection_sources(preferences, self.client_row, source.provider_namespace))
                result, _, _, _ = self.request()
                self.assertFalse(result.invitation_id, result)
        self.assertFalse(IgRestockSubscription.objects.exists())

    def test_unauthenticated_poll_goods_never_create_canonical_selection_decisions(self):
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from management.models import IgCommerceSelectionSession
        before = IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1).snapshot()
        for kind in ("unsigned", "forged", "poll_history"):
            with self.subTest(kind=kind):
                source = self.signed_poll(f"add https://twocomms.shop/product/{self.product.slug}/ quantity 1 size M color black fit classic")
                if kind == "poll_history":
                    source.source = "poll_history"
                else:
                    source.ingress_proof = ({} if kind == "unsigned" else
                        {**source.ingress_proof, "signed": source.ingress_proof["signed"] + "forged"})
                source.save(update_fields=["source", "ingress_proof"])
                with transaction.atomic():
                    result = reduce_inbound_commerce_source(self.client_row, source,
                        expected_provider_namespace=source.provider_namespace)
                self.assertFalse(result.ready)
                self.assertEqual(result.reason, "commerce_source_owner_changed")
                self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())
                self.assertEqual(IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1).snapshot(), before)

    def test_poll_proof_revoked_between_projection_reads_invalidates_capture(self):
        from management.models import InstagramBotMessage
        from management.services import ig_commerce_projection as projection
        source, _ = self.poll_goods_selection()
        self.assertEqual(projection.capture_current_selection_lines(self.client_row.pk, now=self.now)["status"], "captured")
        original = projection.source_preferences_for
        mutated = False
        def revoke_after_projection(*args, **kwargs):
            nonlocal mutated
            result = original(*args, **kwargs)
            if not mutated:
                # Fixture-only concurrent-write injection between the native
                # projection's first and final source observations.
                InstagramBotMessage.objects.filter(pk=source.pk).update(ingress_proof={})
                mutated = True
            return result
        with patch.object(projection, "source_preferences_for", side_effect=revoke_after_projection):
            result = projection._capture_current_selection_lines(self.client_row.pk, now=self.now)
        self.assertTrue(mutated)
        self.assertEqual((result["status"], result["reason"]), ("conflict", "selection_capture_changed"))

    def test_signed_poll_full_cart_purchase_and_neutral_ack_keep_native_semantics(self):
        from management.services.ig_revision_commerce import reduce_inbound_commerce_source
        from management.models import IgCommerceSelectionSession
        self.poll_goods_selection()
        sibling = self.signed_poll(f"add https://twocomms.shop/product/{self.products[1].slug}/ quantity 1 size L color pink fit classic")
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.client_row, sibling,
                expected_provider_namespace=sibling.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        self.assertTrue(IgCommerceTurnDecision.objects.get(source_message=sibling).accepted)
        source = self.signed_poll("Buy all items in my cart")
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.client_row, source,
                expected_provider_namespace=source.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        decision = IgCommerceTurnDecision.objects.get(source_message=source)
        self.assertTrue(decision.accepted, decision.result_payload)
        self.assertTrue(decision.result_payload["source_facts"]["full_cart_purchase_requested"])
        before = IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1).snapshot()
        ack = self.signed_poll("Thank you!")
        with transaction.atomic():
            result = reduce_inbound_commerce_source(self.client_row, ack,
                expected_provider_namespace=ack.provider_namespace)
        self.assertTrue(result.ready, result.reason)
        self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=ack).exists())
        self.assertEqual(IgCommerceSelectionSession.objects.get(client=self.client_row, open_slot=1).snapshot(), before)

    def test_authorized_manager_followup_revalidates_historical_poll_without_granting_head_admission(self):
        from django.contrib.auth import get_user_model
        from management.models import IgCustomerTurnRevision, IgFollowUpTask
        from management.services import ig_restock_followup as followup
        source = self.signed_poll("Чи є ця позиція в наявності?")
        revision, _ = self.revision(source)
        prepared = fulfillment.prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(prepared["ready"], prepared)
        request = IgFulfillmentReviewRequest.objects.get(pk=prepared["request_id"])
        manager = get_user_model().objects.create_superuser(username="poll-followup-manager",
            email="poll-manager@example.test", password="test")
        checked = fulfillment.apply_fulfillment_decision(request.pk, actor=manager,
            expected_request_digest=request.envelope_hmac, operation_id=uuid.uuid4(),
            decisions_by_line=[{"line_id": row["line_id"], "availability": "wait"}
                for row in request.envelope["lines"]], now=self.now)
        self.assertTrue(checked["ready"], checked)
        task = IgFollowUpTask.objects.get(event_key=f"ig-fulfillment-update:{request.pk}:{checked['version']}")
        source.status = "done"
        source.save(update_fields=["status"])
        IgCustomerTurnRevision.objects.filter(pk=revision.pk).update(state="processed", active_slot=None,
            claim_token="", lease_until=None)
        neutral = self.source("Дякую!", at=self.now + timedelta(seconds=1))
        current, _ = self.revision(neutral)
        neutral.status = "done"
        neutral.save(update_fields=["status"])
        IgCustomerTurnRevision.objects.filter(pk=current.pk).update(state="processed", claim_token="", lease_until=None)
        self.assertEqual(owned_source(revision, source, self.settings)[1], "revision_not_current")
        self.assertIsNotNone(owned_source(revision, source, self.settings, require_head=False)[0])
        projection = fulfillment.validated_update_task(task, self.settings, now=self.now + timedelta(seconds=2))
        self.assertTrue(projection["ready"], projection)
        with transaction.atomic(), patch("management.services.instagram_bot._provider_http") as provider:
            offer = followup.prepare_task_offer(task, projection, self.settings,
                text="Менеджер перевірив: позиція очікується.", now=self.now + timedelta(seconds=2))
        provider.assert_not_called()
        self.assertTrue(offer["ready"], offer)
        invitation = restock.Invitation.objects.get(request_source=source)
        self.assertIn("poll_admission", invitation.envelope["source"])


@skipUnless(connection.vendor == "mysql", "Native restock trigger guards require MariaDB")
@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class SignedPollNativeGuardTests(_RestockFixture, TestCase):
    signed_poll = SignedPollPurposeTests.signed_poll

    def poll_invitation(self):
        from management.ig_restock_subscription_models import IgRestockInvitation
        source = self.signed_poll("Повідомте, коли ця позиція буде в наявності")
        result, _, revision, _ = self.request(source=source)
        self.assertTrue(result.invitation_id, result)
        invitation = IgRestockInvitation.objects.get(pk=result.invitation_id)
        self.assertIn("poll_admission", invitation.envelope["source"])
        return source, revision, invitation

    def clone(self, original, *, missing_witness=False):
        values = {field.attname: deepcopy(getattr(original, field.attname))
            for field in original._meta.concrete_fields if field.name not in {"id", "created_at"}}
        if isinstance(original.pk, uuid.UUID):
            values["id"] = uuid.uuid4()
        if missing_witness:
            values["envelope"]["source"].pop("poll_admission", None)
        return type(original)(**values)

    def assert_native_rejects(self, row):
        with self.assertRaisesMessage(DatabaseError, "Fulfilment immutable/source/privacy guard"):
            with transaction.atomic():
                row.save(force_insert=True)

    def test_native_poll_invitation_requires_exact_current_signed_proof_witness(self):
        from django.core import signing
        from management.services.ig_ingress_source_proof import SALT, is_current_customer_source
        source, _, invitation = self.poll_invitation()
        self.assert_native_rejects(self.clone(invitation, missing_witness=True))
        # Fixture-only proof replacement simulates the gap between checked
        # Python ownership and the SQL insert. The replacement itself has a
        # valid HMAC, but the old signed-envelope witness must not authorize it.
        payload = signing.loads(source.ingress_proof["signed"], salt=SALT)
        payload["admitted_at"] = (self.now + timedelta(seconds=1)).isoformat()
        source.ingress_proof = {**source.ingress_proof,
            "signed": signing.dumps(payload, salt=SALT, compress=True)}
        source.save(update_fields=["ingress_proof"])
        self.assertTrue(is_current_customer_source(source))
        self.assert_native_rejects(self.clone(invitation))

    def test_native_poll_answer_requires_witness_and_rejects_revoked_proof(self):
        from management.ig_restock_subscription_models import IgRestockAnswer
        invitation = self.invitation()
        source = self.signed_poll(invitation.button_title, payload=invitation.accept_payload,
            at=self.now + timedelta(seconds=1))
        revision, _ = self.revision(source)
        result = restock.handle_reply(source, revision=revision, settings_obj=self.settings,
            now=source.provider_created_at)
        self.assertEqual(result.reason, "accept", result)
        answer = IgRestockAnswer.objects.get(source=source)
        self.assert_native_rejects(self.clone(answer, missing_witness=True))
        source.ingress_proof = {}
        source.save(update_fields=["ingress_proof"])
        self.assert_native_rejects(self.clone(answer))

    def test_forged_or_unsigned_poll_is_rechecked_immediately_before_seal(self):
        source, _, invitation = self.poll_invitation()
        original = deepcopy(source.ingress_proof)
        for proof in ({}, {**original, "signed": original["signed"] + "forged"}):
            with self.subTest(unsigned=not proof):
                source.ingress_proof = proof
                source.save(update_fields=["ingress_proof"])
                evidence = {key: deepcopy(value) for key, value in invitation.envelope.items()
                    if key not in {"version", "purpose", "columns"}}
                with self.assertRaisesMessage(ValueError, "restock_poll_source_changed"):
                    restock._seal(self.clone(invitation), "invitation", **evidence)

    def test_final_native_poll_recheck_locks_client_then_source_and_rejects_changed_content(self):
        from django.test.utils import CaptureQueriesContext
        source, _, invitation = self.poll_invitation()
        source.text = "Changed after original source admission"
        source.save(update_fields=["text"])
        evidence = {key: deepcopy(value) for key, value in invitation.envelope.items()
            if key not in {"version", "purpose", "columns"}}
        row = self.clone(invitation)
        with CaptureQueriesContext(connection) as reads:
            with self.assertRaisesMessage(ValueError, "restock_poll_source_changed"):
                restock._seal(row, "invitation", **evidence)
        locked = [query["sql"] for query in reads if "FOR UPDATE" in query["sql"].upper()]
        self.assertGreaterEqual(len(locked), 2)
        self.assertIn("FROM `management_igclient`", locked[0])
        self.assertIn("FROM `management_instagrambotmessage`", locked[1])
        self.assertTrue(row._state.adding)
