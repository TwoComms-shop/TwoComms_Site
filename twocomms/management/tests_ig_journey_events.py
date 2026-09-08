"""Checkout semantic receipts preserve ownership and transaction boundaries."""
from copy import copy
from unittest.mock import patch

from django.test import TestCase

from management.models import (
    IgCheckoutProposal, IgCheckoutRevision, IgClient,
    IgCommercialEpisodeEvent, InstagramBotMessage,
)
from management.services.ig_checkout import create_or_update_proposal
from management.services.ig_journey_events import (
    JourneyEventRejected, record_validated_checkout_revision,
)
from productcolors.models import Color, ProductColorVariant
from storefront.models import Category, Product, ProductFitOption, ProductStatus


RECORDER = "management.services.ig_journey_events.record_validated_checkout_revision"


class ValidatedCheckoutJourneyEventTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name="Футболки", slug="journey-offer-shirts")
        self.product = Product.objects.create(
            title="Journey shirt", slug="journey-offer-shirt", category=category,
            price=950, status=ProductStatus.PUBLISHED,
        )
        ProductFitOption.objects.create(
            product=self.product, code="classic", label="Класичний", is_active=True,
        )
        color = Color.objects.create(name="Journey blue", primary_hex="#2255AA")
        self.variant = ProductColorVariant.objects.create(
            product=self.product, color=color, stock=5, sku="JOURNEY-BLUE",
        )
        self.buyer = IgClient.objects.create(igsid="journey-offer-buyer")
        self.other = IgClient.objects.create(igsid="journey-offer-other")
        self.message = InstagramBotMessage.objects.create(
            client=self.buyer, sender_id=self.buyer.igsid, role="user",
            text="PRIVATE CUSTOMER TEXT", status="done",
        )

    def create_offer(self, *, messages=None):
        return create_or_update_proposal(
            client=self.buyer, pay_type="online_full",
            item_specs=[{
                "product_id": self.product.pk, "color_variant_id": self.variant.pk,
                "qty": 1, "size": "M", "fit_option_code": "classic",
            }],
            evidence={"message_ids": messages if messages is not None else [self.message.pk]},
        )

    def test_validated_offer_records_one_exact_non_delivery_transition(self):
        proposal = self.create_offer()
        revision = proposal.revisions.get()
        event = IgCommercialEpisodeEvent.objects.get(event_type="semantic_transition")
        self.assertEqual(event.episode_id, proposal.commercial_episode_id)
        self.assertEqual((event.from_state, event.to_state, event.stage), ("", "", ""))
        self.assertEqual(event.source, "checkout_revision")
        self.assertEqual(event.evidence, {
            "schema_version": 1,
            "from_node": "configured_line", "to_node": "quoted_offer",
            "trigger": "validated_offer_created", "proposal_id": proposal.pk,
            "checkout_revision_id": revision.pk, "revision": revision.revision,
            "digest": revision.digest, "evidence_message_ids": [self.message.pk],
            "authority": "validated_checkout_revision",
        })
        self.assertNotIn(self.message.text, str(event.evidence))
        self.assertEqual(record_validated_checkout_revision(revision).pk, event.pk)
        self.assertEqual(self.create_offer().pk, proposal.pk)
        self.assertEqual(IgCommercialEpisodeEvent.objects.filter(event_type="semantic_transition").count(), 1)
        self.assertIsNone(proposal.payment_attempt_id)

    def test_foreign_evidence_and_tampered_revision_bindings_are_rejected(self):
        foreign = InstagramBotMessage.objects.create(
            client=self.other, sender_id=self.other.igsid, role="user", text="OTHER PRIVATE TEXT",
        )
        with patch(RECORDER):
            proposal = self.create_offer(messages=[foreign.pk])
        revision = proposal.revisions.get()
        with self.assertRaisesMessage(JourneyEventRejected, "checkout_evidence_owner_mismatch"):
            record_validated_checkout_revision(revision)
        for field, value in (("digest", "a" * 64), ("revision", 99), ("proposal_id", 999999)):
            tampered = copy(revision)
            setattr(tampered, field, value)
            with self.subTest(field=field), self.assertRaisesMessage(
                JourneyEventRejected, "checkout_revision_binding_changed",
            ):
                record_validated_checkout_revision(tampered)
        episode = proposal.commercial_episode
        episode.client = self.other
        episode.save(update_fields=["client"])
        with self.assertRaisesMessage(JourneyEventRejected, "checkout_episode_owner_mismatch"):
            record_validated_checkout_revision(revision)
        self.assertFalse(IgCommercialEpisodeEvent.objects.filter(event_type="semantic_transition").exists())

    def test_same_event_key_with_changed_evidence_is_rejected(self):
        with patch(RECORDER):
            proposal = self.create_offer()
        revision = proposal.revisions.get()
        IgCommercialEpisodeEvent.objects.create(
            episode=proposal.commercial_episode,
            dedupe_key=f"journey:checkout-revision:{revision.pk}:validated-offer",
            event_type="semantic_transition", source="checkout_revision",
            evidence={"schema_version": 1, "to_node": "settlement"},
        )
        with self.assertRaisesMessage(JourneyEventRejected, "journey_event_identity_conflict"):
            record_validated_checkout_revision(revision)
        self.assertEqual(IgCommercialEpisodeEvent.objects.filter(event_type="semantic_transition").count(), 1)

    def test_optional_database_failure_rolls_back_savepoint_not_checkout(self):
        def failed_projection(revision):
            event = record_validated_checkout_revision(revision)
            # A real failing SQL statement poisons the nested transaction until
            # the checkout hook exits its savepoint and handles the exception.
            IgCommercialEpisodeEvent.objects.create(
                episode_id=event.episode_id, dedupe_key=event.dedupe_key,
                event_type="semantic_transition",
            )

        with patch(RECORDER, side_effect=failed_projection), self.assertLogs(
            "management.services.ig_checkout", level="WARNING",
        ) as logs:
            proposal = self.create_offer()
        self.assertTrue(IgCheckoutProposal.objects.filter(pk=proposal.pk).exists())
        self.assertEqual(proposal.revisions.count(), 1)
        self.assertFalse(IgCommercialEpisodeEvent.objects.filter(event_type="semantic_transition").exists())
        self.assertIn("failure_type=IntegrityError", logs.output[0])
        self.assertNotIn(self.message.text, logs.output[0])
        self.buyer.refresh_from_db()
        self.assertEqual(self.buyer.stage, IgClient.Stage.CHECKOUT)

    def test_later_checkout_failure_rolls_back_revision_and_event_together(self):
        with patch(
            "management.services.bot_followups.schedule_proposal_expiry_event",
            side_effect=RuntimeError("later checkout failure"),
        ), self.assertRaisesMessage(RuntimeError, "later checkout failure"):
            self.create_offer()
        self.assertFalse(IgCheckoutProposal.objects.exists())
        self.assertFalse(IgCheckoutRevision.objects.exists())
        self.assertFalse(IgCommercialEpisodeEvent.objects.filter(event_type="semantic_transition").exists())
