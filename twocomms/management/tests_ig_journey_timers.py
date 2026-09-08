from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from management import tests_ig_checkout_generation_schema as fixtures
from management.models import (IgCheckoutInvoiceGeneration, IgCheckoutProposal,
    IgClient, IgFunnelResetAudit, IgPaymentProjection, InstagramBotMessage)
from management.services.ig_journey_timers import invoice_timers
from orders.models import PaymentAttempt


class JourneyInvoiceTimerTests(TestCase):
    _proposal = fixtures.CheckoutGenerationSchemaTests._proposal
    _generation = fixtures.CheckoutGenerationSchemaTests._generation

    def setUp(self):
        fixtures.CheckoutGenerationSchemaTests.setUp(self)
        self.source = InstagramBotMessage.objects.create(client=self.client_row,
            role=InstagramBotMessage.Role.USER, text="Хочу оформити замовлення")
        self.episode.opened_watermark_message_id = self.source.pk
        self.episode.save(update_fields=["opened_watermark_message_id", "updated_at"])
        self.client_row.current_commercial_episode = self.episode
        self.client_row.save(update_fields=["current_commercial_episode", "updated_at"])
        self.proposal = self._proposal(assisted_checkout_v2=True,
            payment_policy=IgCheckoutProposal.PaymentPolicy.FULL_ONLY,
            expires_at=timezone.now() + timedelta(hours=12))
        self.now = timezone.now()
        self.due = self.now + timedelta(minutes=43)
        self.attempt = PaymentAttempt.objects.create(fingerprint="invoice-timer-attempt",
            full_name="Test", phone="test", city="test", np_office="test",
            pay_type="online_full", status=PaymentAttempt.Status.PROCESSING,
            monobank_invoice_id="invoice-timer-1", invoice_url="https://pay.invalid/private",
            invoice_expires_at=self.due, checkout_series_key="b" * 64,
            checkout_generation=1)
        self.generation = self._generation(self.proposal, 1,
            state=IgCheckoutInvoiceGeneration.State.INVOICE_CREATED, active_slot=1,
            payment_attempt=self.attempt, provider_invoice_id=self.attempt.monobank_invoice_id,
            expires_at=self.due, provider_started_at=self.now - timedelta(seconds=1),
            provider_completed_at=self.now)
        self.proposal.current_invoice_generation = self.generation
        self.proposal.status = self.proposal.Status.INVOICE_CREATED
        self.proposal.save(update_fields=["current_invoice_generation", "status", "updated_at"])
        self.deal.active_checkout_proposal = self.proposal
        self.deal.save(update_fields=["active_checkout_proposal", "updated_at"])

    def timers(self, *, now=None, client_id=None, episode_id=None):
        return invoice_timers(client_id or self.client_row.pk, episode_id or self.episode.pk,
            now=now or self.now)

    def test_current_timer_uses_recorded_dates_with_four_read_queries(self):
        with self.assertNumQueries(4):
            timers = self.timers()
        self.assertEqual(len(timers), 1)
        timer = timers[0]
        self.assertEqual(timer["started_at"], self.now.isoformat())
        self.assertEqual(timer["due_at"], self.due.isoformat())
        self.assertEqual(timer["status"], "running")
        self.assertEqual(timer["label"], "Строк рахунку")
        self.assertNotIn("private", str(timer))
        self.assertNotIn("invoice-timer-1", str(timer))

    def test_recorded_deadline_expiry_does_not_mutate_payment(self):
        self.assertEqual(self.timers(now=self.due)[0]["status"], "expired")
        self.generation.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(self.generation.state, "invoice_created")
        self.assertEqual(self.attempt.status, "processing")

    def test_foreign_or_noncurrent_episode_has_no_ring(self):
        other = IgClient.objects.create(igsid="invoice-timer-other")
        self.assertEqual(self.timers(client_id=other.pk), [])
        self.assertEqual(self.timers(episode_id=self.episode.pk + 1000), [])
        IgClient.objects.filter(pk=self.client_row.pk).update(current_commercial_episode=None)
        self.assertEqual(self.timers(), [])

    def test_inactive_generation_revision_or_provider_identity_is_not_current(self):
        for field, value in (("active_slot", None), ("proposal_revision", 99),
            ("provider_invoice_id", "different-provider-id"), ("provider", "other")):
            with self.subTest(field=field):
                old = getattr(self.generation, field)
                IgCheckoutInvoiceGeneration.objects.filter(pk=self.generation.pk).update(**{field: value})
                self.assertEqual(self.timers(), [])
                IgCheckoutInvoiceGeneration.objects.filter(pk=self.generation.pk).update(**{field: old})

    def test_failed_cancelled_or_ambiguous_invoice_is_not_reported_expired(self):
        states = ("planned", "provider_inflight", "provider_ambiguous", "ambiguity_review",
            "late_provider_review", "failed", "cancelled", "late_paid_review", "resource_review")
        for state in states:
            with self.subTest(state=state):
                IgCheckoutInvoiceGeneration.objects.filter(pk=self.generation.pk).update(state=state)
                self.assertEqual(self.timers(now=self.due + timedelta(seconds=1)), [])

    def test_settlement_truth_wins_over_elapsed_invoice_deadline(self):
        projection = IgPaymentProjection.objects.create(deal=self.deal, client=self.client_row,
            truth=self.deal.PaymentTruth.CONFIRMED, paid_at=self.now)
        self.assertEqual(self.timers(now=self.due), [])
        projection.truth = self.deal.PaymentTruth.PENDING
        projection.paid_at = None
        projection.needs_reconciliation = True
        projection.save()
        self.assertEqual(self.timers(), [])

    def test_settled_other_generation_suppresses_current_ring(self):
        self._generation(self.proposal, 2, winner_slot=1,
            state=IgCheckoutInvoiceGeneration.State.PAID_WINNER, paid_at=self.now)
        self.assertEqual(self.timers(), [])

    def test_attempt_settlement_or_identity_disagreement_is_not_a_countdown(self):
        for field, value in (("status", "paid"), ("checkout_series_key", "wrong"),
            ("checkout_generation", 9), ("invoice_expires_at", self.due + timedelta(minutes=1)),
            ("provider_recheck_state", "pending")):
            with self.subTest(field=field):
                old = getattr(self.attempt, field)
                PaymentAttempt.objects.filter(pk=self.attempt.pk).update(**{field: value})
                self.assertEqual(self.timers(), [])
                PaymentAttempt.objects.filter(pk=self.attempt.pk).update(**{field: old})

    def test_missing_or_invalid_actual_start_does_not_invent_lifetime(self):
        for value in (None, self.now + timedelta(seconds=1), self.due):
            with self.subTest(value=value):
                IgCheckoutInvoiceGeneration.objects.filter(pk=self.generation.pk).update(provider_completed_at=value)
                self.assertEqual(self.timers(), [])

    def test_reset_blocks_old_invoice_even_while_active_pointers_remain(self):
        IgFunnelResetAudit.objects.create(client=self.client_row,
            reset_after_message_id=self.source.pk, reason="timer reset")
        self.assertEqual(self.timers(), [])

    def test_real_owned_user_evidence_is_required(self):
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(role="manager")
        self.assertEqual(self.timers(), [])
        other = IgClient.objects.create(igsid="foreign-invoice-source")
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(role="user", client=other)
        self.assertEqual(self.timers(), [])

    def test_client_erasure_hides_timer(self):
        IgClient.objects.filter(pk=self.client_row.pk).update(privacy_erasure_started_at=self.now)
        self.assertEqual(self.timers(), [])
