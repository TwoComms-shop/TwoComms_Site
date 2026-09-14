"""Source-backed preference rejection and correction regressions."""

import hashlib
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, InstagramBotMessage
from management.services.ig_commerce_projection import source_preferences_for
from management.services.ig_commerce_state import apply_turn
from management.services.ig_commerce_turns import parse_turn, understand_turn
from management.services.ig_commerce_types import CommerceTurnRequest


class PreferenceWithdrawalTests(TestCase):
    def setUp(self):
        self.client = IgClient.objects.create(igsid="withdrawal-customer", language="ru")
        self.sequence = 0

    def reduce(self, text, *, request=None, stale=False, **source_changes):
        self.sequence += 1
        source = InstagramBotMessage.objects.create(
            **{
                "client": self.client, "sender_id": self.client.igsid,
                "role": "user", "source": "webhook", "text": text,
                "mid": f"withdrawal-{self.sequence}",
                "provider_namespace": "instagram_login:fixture",
                "provider_created_at": timezone.now() - timedelta(days=int(stale)),
                **source_changes,
            },
        )
        decision = apply_turn(self.client, source, request or parse_turn(text), reply_payload={})
        return source, decision

    def preferences(self):
        return source_preferences_for(self.client).get("values", {})

    def seed(self):
        return self.reduce("Хочу чёрную футболку оверсайз")

    def test_rejection_removes_matching_fit_and_keeps_colour_garment_and_history(self):
        _, original = self.seed()
        old_snapshot = original.transition.next_snapshot
        source, decision = self.reduce("не хочу оверсайз")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.transition.action, "preference_withdrawn")
        self.assertEqual(self.preferences(), {"color": "black", "garment_type": "tshirt"})
        self.assertNotIn("fit_option_code", decision.session.lines[0])
        self.assertEqual(decision.result_payload["preference_withdrawal"], {
            "values": {"fit": "oversize"}, "source_message_id": source.pk,
            "source_digest": hashlib.sha256(source.text.encode()).hexdigest(),
        })
        original.transition.refresh_from_db()
        self.assertEqual(original.transition.next_snapshot, old_snapshot)
        replay = apply_turn(self.client, source, parse_turn(source.text), reply_payload={})
        self.assertEqual(replay.pk, decision.pk)

    def test_each_field_can_be_rejected_without_clearing_other_preferences(self):
        self.seed()
        self.reduce("не хочу чёрную")
        self.assertEqual(self.preferences(), {"fit_option_code": "oversize", "garment_type": "tshirt"})
        self.reduce("не хочу футболку")
        self.assertEqual(self.preferences(), {"fit_option_code": "oversize"})
        self.reduce("оверсайз не хочу")
        self.assertEqual(self.preferences(), {})

    def test_positive_correction_replaces_only_the_rejected_field(self):
        self.seed()
        source, _ = self.reduce("не оверсайз, а классика")
        projection = source_preferences_for(self.client)
        self.assertEqual(projection["values"], {
            "fit_option_code": "classic", "color": "black", "garment_type": "tshirt",
        })
        self.assertEqual(projection["evidence"]["fit_option_code"]["source_message_id"], source.pk)
        self.reduce("не чёрную, белую")
        self.assertEqual(self.preferences()["color"], "white")
        self.reduce("не футболку, а худи")
        self.assertEqual(self.preferences()["garment_type"], "hoodie")

    def test_generic_negative_question_quote_description_and_other_value_do_not_clear(self):
        self.seed()
        expected = self.preferences()
        for text in (
            "не хочу", "Нет", "не хочу оверсайз?", "«не хочу оверсайз»",
            "В описании: не оверсайз", "Это не оверсайз", "не хочу классику",
            "Оверсайз?", "классика или оверсайз?",
        ):
            with self.subTest(text=text):
                self.reduce(text)
                self.assertEqual(self.preferences(), expected)

    def test_stale_or_forged_or_noncustomer_withdrawal_cannot_clear(self):
        self.seed()
        expected = self.preferences()
        _, stale = self.reduce("не хочу оверсайз", stale=True)
        self.assertFalse(stale.accepted)
        self.assertTrue(stale.is_stale)
        self.assertEqual(self.preferences(), expected)
        self.reduce("Привет", request=CommerceTurnRequest(preference_withdrawals={"fit": "oversize"}))
        self.assertEqual(self.preferences(), expected)
        self.reduce("не хочу оверсайз", sender_id="someone-else")
        self.assertEqual(self.preferences(), expected)

    def test_withdrawn_old_proof_cannot_be_revived_by_legacy_value_write(self):
        self.seed()
        _, decision = self.reduce("не хочу оверсайз")
        session = decision.session
        session.lines[0]["fit_option_code"] = "oversize"
        session.save(update_fields=["lines"])
        self.assertNotIn("fit_option_code", self.preferences())

    def test_new_affirmation_after_withdrawal_has_fresh_proof(self):
        self.seed()
        self.reduce("не хочу оверсайз")
        source, _ = self.reduce("Оверсайз")
        projection = source_preferences_for(self.client)
        self.assertEqual(projection["values"]["fit_option_code"], "oversize")
        self.assertEqual(projection["evidence"]["fit_option_code"]["source_message_id"], source.pk)

    def test_multilingual_withdrawal_and_immutable_request(self):
        for text in ("не хочу оверсайз", "я не хочу оверсайз", "I don't want oversize", "no longer want oversize", "not oversize"):
            with self.subTest(text=text):
                request = understand_turn(text, model_payload={"fit": "oversize"})
                self.assertEqual(dict(request.preference_withdrawals), {"fit": "oversize"})
                self.assertNotIn("fit", request.field_updates)
                with self.assertRaises(TypeError):
                    request.preference_withdrawals["fit"] = "classic"
