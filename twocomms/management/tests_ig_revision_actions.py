import hashlib
import json
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    BotPolicyPublication,
    IgClient,
    IgCustomerTurn,
    IgFunnelStepEvent,
    IgTurnMessage,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_revision_actions import (
    apply_revision_selection_actions,
)
from management.services.ig_revision_authority import (
    CLAIM_CATALOG_CONFIGURATION,
    build_revision_authority_bindings,
    check_fact_bindings,
)
from management.services.ig_revision_execution import due_revision_ids
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_revisions import (
    claim_revision_preparation,
    claim_sealed_revision,
    create_collecting_revision,
    seal_revision,
)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class RevisionSelectionActionTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product

        snapshot = {"schema_version": 1, "instructions": []}
        snapshot_hash = hashlib.sha256(json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        self.publication = BotPolicyPublication.objects.create(
            version=1,
            kind=BotPolicyPublication.Kind.PUBLISH,
            schema_version=1,
            snapshot=snapshot,
            snapshot_hash=snapshot_hash,
            compiler_version="instruction-set-v1",
            instruction_count=0,
        )
        self.settings = InstagramBotSettings.objects.create(
            pk=1,
            is_enabled=True,
            reply_permission_epoch=4,
            active_instruction_publication=self.publication,
        )
        category = Category.objects.create(
            name="Revision action", slug="revision-action"
        )
        self.product = Product.objects.create(
            title="Revision action product",
            slug="revision-action-product",
            category=category,
            price=900,
            status="published",
        )
        color = Color.objects.create(name="Revision black", primary_hex="#111111")
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=color,
            price_override=900,
            is_default=True,
        )
        self.client_row = IgClient.objects.create(
            igsid="revision-action-client",
            reply_permission_epoch=3,
        )
        self.source = self._source("revision-action-source")
        self.turn = IgCustomerTurn.objects.create(
            client=self.client_row,
            primary_source_message=self.source,
            window_started_at=timezone.now(),
            window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(
            turn=self.turn,
            message=self.source,
            ordinal=1,
            role=self.source.role,
        )
        revision = create_collecting_revision(
            self.turn,
            [self.source],
            now=timezone.now(),
            bypass_quiet=True,
        ).revision
        prep = claim_revision_preparation(revision.pk)
        sealed = seal_revision(revision.pk, prep.token).revision
        claimed = claim_sealed_revision(sealed.pk)
        self.revision = claimed.revision
        self.revision_token = claimed.token
        self.publication_binding = PublicationBinding(
            self.publication.pk,
            self.publication.version,
            self.publication.snapshot_hash,
        )
        self.pre_action_authority = self._authority()
        proposal = {
            "schema_version": 1,
            "sources": [{
                "message_id": self.source.pk,
                "source_digest": self.revision.sources.get().source_digest,
                "ordinal": 1,
            }],
            "generation": {
                "request_id": "revision-action-request",
                "actual_model": "gemini-3.7-flash",
                "generated_at": timezone.now().isoformat(),
            },
            "authority": {
                "allowed_actions": list(
                    self.pre_action_authority.allowed_actions
                ),
                "fact_bindings": list(
                    self.pre_action_authority.fact_bindings
                ),
                "offer_bindings": list(
                    self.pre_action_authority.offer_bindings
                ),
                "authority_digest": self.pre_action_authority.authority_digest,
            },
        }
        self.revision.generation_proposal = proposal
        self.revision.generation_proposal_digest = hashlib.sha256(json.dumps(
            proposal,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        self.revision.generation_proposed_at = timezone.now()
        self.revision.save(update_fields=[
            "generation_proposal", "generation_proposal_digest",
            "generation_proposed_at", "updated_at",
        ])

    def _source(self, mid):
        return InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text="обираю цей варіант",
            mid=mid,
            status=InstagramBotMessage.Status.PENDING,
        )

    def _authority(self):
        return build_revision_authority_bindings(
            self.client_row,
            claims=(CLAIM_CATALOG_CONFIGURATION,),
            control={
                "product": self.product.pk,
                "variant": self.variant.pk,
                "qty": 2,
                "price": "0.01",
            },
            server_authorized_actions=("client_configuration_update",),
        )

    def _apply(self, **overrides):
        values = {
            "source_message_id": self.source.pk,
            "settings_id": self.settings.pk,
            "settings_permission_epoch": self.settings.reply_permission_epoch,
            "publication": self.publication_binding,
            "authority": self.pre_action_authority,
            "generation_proposal_digest": (
                self.revision.generation_proposal_digest
            ),
        }
        values.update(overrides)
        return apply_revision_selection_actions(
            self.revision.pk,
            self.revision_token,
            **values,
        )

    def test_bound_product_and_variant_apply_atomically_and_require_rebuild(self):
        original_authority = self._authority()
        result = self._apply(authority=original_authority)

        self.assertTrue(result.applied, result.reasons)
        self.assertFalse(result.authority_rebuild_required)
        self.assertIsNotNone(result.post_action_authority)
        self.assertEqual(
            result.post_action_claims, (CLAIM_CATALOG_CONFIGURATION,)
        )
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.current_product_id, self.product.pk)
        self.assertEqual(self.client_row.current_qty, 2)
        selection = self.client_row.sales_context["assisted_checkout_selection"]
        self.assertEqual(selection["product_id"], self.product.pk)
        self.assertEqual(selection["color_variant_id"], self.variant.pk)
        self.assertFalse(check_fact_bindings(
            original_authority.fact_bindings,
            revision=self.revision,
            client=self.client_row,
        ))
        self.assertTrue(self._authority().ready)
        self.revision.refresh_from_db()
        receipt = self.revision.action_receipts["client_configuration_update"]
        self.assertEqual(receipt["source_message_id"], self.source.pk)
        self.assertEqual(
            receipt["generation_proposal_digest"],
            self.revision.generation_proposal_digest,
        )

    def test_crash_replay_uses_receipt_without_second_domain_mutation(self):
        first = self._apply()
        self.assertTrue(first.applied, first.reasons)

        with patch(
            "management.services.ig_revision_actions.bot_orders.pin_product"
        ) as pin, patch(
            "management.services.ig_revision_actions.persist_control_selection"
        ) as persist:
            replay = self._apply()

        self.assertTrue(replay.applied, replay.reasons)
        self.assertTrue(replay.already_applied)
        self.assertIsNotNone(replay.post_action_authority)
        pin.assert_not_called()
        persist.assert_not_called()

    def test_receipt_replay_rejects_later_configuration_drift(self):
        first = self._apply()
        self.assertTrue(first.applied, first.reasons)
        IgClient.objects.filter(pk=self.client_row.pk).update(current_qty=3)

        with patch(
            "management.services.ig_revision_actions.bot_orders.pin_product"
        ) as pin, patch(
            "management.services.ig_revision_actions.persist_control_selection"
        ) as persist:
            replay = self._apply()

        self.assertEqual(replay.reasons, ("action_receipt_stale",))
        pin.assert_not_called()
        persist.assert_not_called()

    def test_source_outside_sealed_revision_cannot_mutate_selection(self):
        unrelated = self._source("revision-action-unrelated")

        result = self._apply(source_message_id=unrelated.pk)

        self.assertEqual(result.reasons, ("source_not_in_revision",))
        self.client_row.refresh_from_db()
        self.assertIsNone(self.client_row.current_product_id)

    def test_selection_error_rolls_back_product_pin_and_funnel_write(self):
        with patch(
            "management.services.ig_revision_actions.persist_control_selection",
            side_effect=RuntimeError("fixture failure"),
        ):
            result = self._apply()

        self.assertEqual(result.reasons, ("action_failed",))
        self.client_row.refresh_from_db()
        self.assertIsNone(self.client_row.current_product_id)
        self.assertFalse(
            IgFunnelStepEvent.objects.filter(
                episode__client=self.client_row
            ).exists()
        )

    def test_outer_transaction_is_rejected_before_mutation(self):
        authority = self._authority()
        with transaction.atomic():
            result = self._apply(authority=authority)

        self.assertEqual(result.reasons, ("caller_transaction_active",))
        self.client_row.refresh_from_db()
        self.assertIsNone(self.client_row.current_product_id)

    def test_due_selector_excludes_permission_epoch_mismatch(self):
        other_client = IgClient.objects.create(
            igsid="revision-action-stale", reply_permission_epoch=8
        )
        source = InstagramBotMessage.objects.create(
            client=other_client,
            sender_id=other_client.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text="late",
            mid="revision-action-stale-source",
            status=InstagramBotMessage.Status.PENDING,
        )
        turn = IgCustomerTurn.objects.create(
            client=other_client,
            primary_source_message=source,
            window_started_at=timezone.now(),
            window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(
            turn=turn, message=source, ordinal=1, role=source.role
        )
        revision = create_collecting_revision(
            turn, [source], now=timezone.now(), bypass_quiet=True
        ).revision
        IgClient.objects.filter(pk=other_client.pk).update(reply_permission_epoch=9)

        self.assertNotIn(revision.pk, due_revision_ids())
