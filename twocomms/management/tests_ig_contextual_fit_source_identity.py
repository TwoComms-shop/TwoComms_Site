"""A real human selector receipt binds a terse answer without rewriting it."""
import hashlib

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management import tests_ig_human_reply as human_fixtures
from management.models import InstagramBotMessage
from management.services.ig_commerce_source_identity import resolve_source_product_request
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_human_reply import create_human_reply_command, dispatch_human_reply_command


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class ContextualFitSourceIdentityTests(TransactionTestCase):
    _physical_http = human_fixtures.HumanReplyCommandTests._physical_http

    def setUp(self):
        human_fixtures.HumanReplyCommandTests.setUp(self)

    def _answer(self, text):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role="user", source="webhook", status="pending", text=text,
            provider_namespace=self.inbound.provider_namespace, provider_created_at=timezone.now(),
            mid=f"context-fit-answer-{InstagramBotMessage.objects.count()}")

    def _question(self):
        question = "Класика — звична посадка, oversize — вільніша. Яка посадка вам ближча — звична чи вільніша?"
        admitted = create_human_reply_command(self.customer.pk, actor=self.actor, text=question,
            context_message_id=self.inbound.pk)
        delivered = dispatch_human_reply_command(admitted.command.pk)
        self.assertEqual(delivered.state, "sent")
        self.assertTrue(delivered.provider_message_ids)
        return InstagramBotMessage.objects.get(client=self.customer, role="manager", text=question)

    def test_actual_human_question_receipt_then_zvychna_persists_classic_source_proof(self):
        question = self._question()
        source = self._answer("Звична")
        parsed = parse_turn(source.text)
        self.assertNotIn("fit", parsed.field_updates)
        request = resolve_source_product_request(self.customer, source, parsed)
        self.assertEqual(request.field_updates, {"fit": "classic"})
        proof = request.source_binding["contextual_fit_choice"]
        self.assertEqual(proof["question_message_id"], question.pk)
        self.assertEqual(proof["question_provider_message_id"], self.provider_mid)
        self.assertEqual(proof["source_digest"], hashlib.sha256("Звична".encode()).hexdigest())
        self.assertFalse(request.purchase_requested)
        self.assertIsNone(request.exact_product_id)
        with transaction.atomic():
            decision = apply_turn(self.customer, source, request, reply_payload={})
        self.assertEqual(decision.request_payload["field_updates"]["fit"], "classic")
        self.assertEqual(decision.request_payload["source_binding"]["contextual_fit_choice"], proof)
        source.refresh_from_db()
        self.assertEqual(source.text, "Звична")
        self.assertEqual(source.status, "pending")
        self.customer.refresh_from_db()
        self.assertIsNone(self.customer.current_product_id)
        self.assertTrue(self.customer.manager_takeover)
        self.assertTrue(self.customer.bot_paused)

    def test_contextless_terse_word_has_no_global_fit_alias(self):
        source = self._answer("Звична")
        request = resolve_source_product_request(self.customer, source, parse_turn(source.text))
        self.assertNotIn("fit", request.field_updates)
        self.assertNotIn("contextual_fit_choice", request.source_binding)

    def test_explicit_fit_keeps_its_own_source_without_contextual_override(self):
        self._question()
        source = self._answer("Оверсайз")
        request = resolve_source_product_request(self.customer, source, parse_turn(source.text))
        self.assertEqual(request.field_updates.get("fit"), "oversize")
        self.assertNotIn("contextual_fit_choice", request.source_binding)
