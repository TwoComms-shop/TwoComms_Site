from datetime import timedelta
import hashlib
import json

from django.test import TestCase
from django.utils import timezone

from management.models import (
    BotPolicyPublication,
    IgClient,
    IgCustomerTurn,
    IgRevisionDeliveryEffect,
    IgTurnMessage,
    IgWebhookInboxEvent,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_revision_outbox import (
    PublicationBinding,
    cancel_unstarted_effect,
    claim_next_effect,
    finish_effect,
    mark_provider_started,
    plan_revision_effects,
    pre_winner_readiness,
)
from management.services.ig_turn_revisions import (
    claim_revision_preparation,
    claim_sealed_revision,
    create_collecting_revision,
    seal_revision,
)


class RevisionOutboxTests(TestCase):
    def setUp(self):
        publication_snapshot = {"schema_version": 1, "instructions": []}
        self.publication = BotPolicyPublication.objects.create(
            version=1,
            kind=BotPolicyPublication.Kind.PUBLISH,
            schema_version=1,
            snapshot=publication_snapshot,
            snapshot_hash=hashlib.sha256(json.dumps(
                publication_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest(),
            compiler_version="instruction-set-v1",
            instruction_count=0,
        )
        self.settings = InstagramBotSettings.objects.create(
            pk=1,
            is_enabled=True,
            reply_permission_epoch=11,
            active_instruction_publication=self.publication,
        )
        self.client_row = IgClient.objects.create(
            igsid="outbox-client",
            reply_permission_epoch=7,
        )
        self.source = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            text="питання",
            mid="outbox-source",
            status=InstagramBotMessage.Status.PENDING,
        )
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
        self.revision, self.revision_token = self._claim_revision()
        self.binding = PublicationBinding(
            self.publication.pk,
            self.publication.version,
            self.publication.snapshot_hash,
        )

    def _claim_revision(self):
        now = timezone.now()
        revision = create_collecting_revision(
            self.turn,
            [self.source],
            now=now,
            bypass_quiet=True,
        ).revision
        preparation = claim_revision_preparation(revision.pk, now=now)
        sealed = seal_revision(revision.pk, preparation.token, now=now).revision
        claimed = claim_sealed_revision(sealed.pk, now=now)
        return claimed.revision, claimed.token

    def _payload(self, text):
        return {
            "recipient": {"id": self.client_row.igsid},
            "message": {"text": text},
        }

    def _plan(self, effects, **overrides):
        values = {
            "source_message_id": self.source.pk,
            "settings_id": self.settings.pk,
            "settings_permission_epoch": self.settings.reply_permission_epoch,
            "publication": self.binding,
            "authority_context_digest": "b" * 64,
            "effects": effects,
        }
        values.update(overrides)
        return plan_revision_effects(
            self.revision.pk,
            self.revision_token,
            **values,
        )

    def _start(self, group):
        claimed = claim_next_effect(
            self.revision.pk, self.revision_token, group
        )
        self.assertTrue(claimed.token, claimed.reason)
        started = mark_provider_started(
            claimed.effect.pk,
            claimed.token,
            self.revision_token,
        )
        self.assertEqual(started.reason, "provider_started")
        return claimed

    def test_three_part_resume_keeps_sent_first_and_unknown_stops_text_group(self):
        planned = self._plan([
            {"group": "substantive_text", "kind": "text", "payload": self._payload("one")},
            {"group": "substantive_text", "kind": "text", "payload": self._payload("two")},
            {"group": "substantive_text", "kind": "text", "payload": self._payload("three")},
        ])
        self.assertTrue(planned.created, planned.reasons)

        first = self._start("substantive_text")
        finish_effect(
            first.effect.pk,
            first.token,
            provider_namespace="instagram_login:owner-1",
            http_status=200,
            provider_message_id="provider-one",
        )
        second = self._start("substantive_text")
        finish_effect(
            second.effect.pk,
            second.token,
            provider_namespace="instagram_login:owner-1",
            http_status=503,
        )

        blocked = claim_next_effect(
            self.revision.pk, self.revision_token, "substantive_text"
        )
        states = list(
            self.revision.delivery_effects.order_by("part_index")
            .values_list("state", flat=True)
        )
        self.assertEqual(
            states,
            [
                IgRevisionDeliveryEffect.State.SENT,
                IgRevisionDeliveryEffect.State.UNKNOWN,
                IgRevisionDeliveryEffect.State.PLANNED,
            ],
        )
        self.assertFalse(blocked.token)
        self.assertEqual(blocked.reason, "group_blocked")
        self.source.refresh_from_db()
        self.assertEqual(self.source.send_state, "unknown")
        self.assertIsNone(self.source.send_started_at)
        self.assertEqual(self.source.delivery_provider_message_ids, ["provider-one"])
        self.assertIsNone(self.source.send_idempotency_key)

    def test_unknown_media_does_not_block_independent_text_group(self):
        self._plan([
            {
                "group": "catalog_media",
                "kind": "image",
                "payload": {
                    "recipient": {"id": self.client_row.igsid},
                    "message": {"attachment": {"type": "image", "payload": {"url": "https://twocomms.test/a.jpg"}}},
                },
            },
            {"group": "substantive_text", "kind": "text", "payload": self._payload("caption")},
        ])
        media = self._start("catalog_media")
        finish_effect(
            media.effect.pk,
            media.token,
            provider_namespace="instagram_login:owner-1",
            transport_outcome="timeout",
        )

        text = claim_next_effect(
            self.revision.pk, self.revision_token, "substantive_text"
        )

        self.assertTrue(text.token)
        self.assertEqual(text.effect.kind, "text")

    def test_plan_is_immutable_idempotent_and_same_payload_new_revision_is_distinct(self):
        specs = [
            {"group": "substantive_text", "kind": "text", "payload": self._payload("same")},
        ]
        first = self._plan(specs)
        replay = self._plan(specs)
        effect = first.effects[0]
        self.assertFalse(replay.created)
        self.assertEqual(replay.effects[0].pk, effect.pk)
        effect.payload = self._payload("changed")
        with self.assertRaises(ValueError):
            effect.save(update_fields=["payload", "updated_at"])
        with self.assertRaises(ValueError):
            IgRevisionDeliveryEffect.objects.filter(pk=effect.pk).update(
                payload=self._payload("changed-through-queryset")
            )

        self.revision, self.revision_token = self._claim_revision()
        second = self._plan(specs)
        self.assertTrue(second.created)
        self.assertNotEqual(effect.effect_key, second.effects[0].effect_key)

    def test_epoch_publication_and_pending_inbox_fail_closed(self):
        IgWebhookInboxEvent.objects.create(
            namespace="instagram_login:owner-1",
            event_key="pending-outbox",
            owner_id="owner-1",
            customer_igsid=self.client_row.igsid,
            decision=IgWebhookInboxEvent.Decision.BLOCKED,
            payload={},
            payload_digest="c" * 64,
        )
        pending = pre_winner_readiness(
            self.revision.pk,
            self.revision_token,
            settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding,
        )
        self.assertIn("pending_inbound", pending.reasons)
        IgWebhookInboxEvent.objects.update(processed_at=timezone.now())

        plan = self._plan([
            {"group": "substantive_text", "kind": "text", "payload": self._payload("answer")},
        ])
        self.assertTrue(plan.created)
        claimed = claim_next_effect(
            self.revision.pk, self.revision_token, "substantive_text"
        )
        self.client_row.reply_permission_epoch += 1
        self.client_row.save(update_fields=["reply_permission_epoch", "updated_at"])
        cancelled = mark_provider_started(
            claimed.effect.pk,
            claimed.token,
            self.revision_token,
        )
        self.assertEqual(cancelled.effect.state, cancelled.effect.State.CANCELLED)
        self.assertEqual(cancelled.reason, "client_permission_changed")

        other_snapshot = {"schema_version": 1, "instructions": [{"id": "changed"}]}
        other_publication = BotPolicyPublication.objects.create(
            version=2,
            kind=BotPolicyPublication.Kind.PUBLISH,
            schema_version=1,
            snapshot=other_snapshot,
            snapshot_hash=hashlib.sha256(json.dumps(
                other_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest(),
            compiler_version="instruction-set-v1",
            instruction_count=1,
            parent=self.publication,
        )
        self.settings.active_instruction_publication = other_publication
        self.settings.save(update_fields=["active_instruction_publication", "updated_at"])
        publication_changed = pre_winner_readiness(
            self.revision.pk,
            self.revision_token,
            settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding,
        )
        self.assertIn("publication_changed", publication_changed.reasons)

    def test_provider_started_cannot_cancel_and_success_without_mid_is_unknown(self):
        self._plan([
            {"group": "substantive_text", "kind": "text", "payload": self._payload("answer")},
        ])
        claimed = self._start("substantive_text")

        self.assertFalse(cancel_unstarted_effect(
            claimed.effect.pk,
            self.revision_token,
            effect_token=claimed.token,
        ))
        result = finish_effect(
            claimed.effect.pk,
            claimed.token,
            provider_namespace="instagram_login:owner-1",
            http_status=200,
            provider_message_id="",
        )

        self.assertEqual(result.effect.state, result.effect.State.UNKNOWN)
        self.assertEqual(result.effect.failure_code, "provider_message_id_missing")

    def test_only_explicit_four_xx_is_definite_and_receipt_namespace_is_exact(self):
        specs = [
            {"group": "substantive_text", "kind": "text", "payload": self._payload("answer")},
        ]
        self._plan(specs)
        rejected = self._start("substantive_text")
        definite = finish_effect(
            rejected.effect.pk,
            rejected.token,
            provider_namespace="instagram_login:owner-1",
            http_status=400,
            transport_outcome="explicit_rejected",
        )
        self.assertEqual(definite.effect.state, definite.effect.State.DEFINITE_FAILED)

        self.revision, self.revision_token = self._claim_revision()
        self._plan(specs)
        wrong_namespace = self._start("substantive_text")
        unknown = finish_effect(
            wrong_namespace.effect.pk,
            wrong_namespace.token,
            provider_namespace="instagram_login:other-owner",
            http_status=200,
            provider_message_id="foreign-receipt",
        )
        self.assertEqual(unknown.effect.state, unknown.effect.State.UNKNOWN)
        self.assertEqual(unknown.effect.failure_code, "receipt_namespace_mismatch")
        self.assertEqual(unknown.effect.provider_message_id, "")

    def test_fallback_activates_only_for_exact_explicit_rejection(self):
        def specs():
            return [
                {
                    "group": "template",
                    "kind": "template",
                    "payload": self._payload("primary"),
                },
                {
                    "group": "template_fallback",
                    "kind": "fallback",
                    "payload": self._payload("fallback"),
                    "activation": {
                        "group": "template",
                        "part_index": 0,
                        "failure_code": "provider_rejected",
                    },
                },
            ]

        invalid_cycle = self._plan(list(reversed(specs())))
        self.assertEqual(
            invalid_cycle.reasons, ("fallback_activation_invalid",)
        )
        first_plan = self._plan(specs())
        fallback = first_plan.effects[1]
        fallback.activation_failure_code = "link_rejected"
        with self.assertRaises(ValueError):
            fallback.save(update_fields=["activation_failure_code", "updated_at"])
        primary = self._start("template")
        finish_effect(
            primary.effect.pk,
            primary.token,
            provider_namespace="instagram_login:owner-1",
            transport_outcome="timeout",
        )
        denied_unknown = claim_next_effect(
            self.revision.pk, self.revision_token, "template_fallback"
        )
        self.assertFalse(denied_unknown.token)
        first_plan.effects[1].refresh_from_db()
        self.assertEqual(
            first_plan.effects[1].state,
            IgRevisionDeliveryEffect.State.CANCELLED,
        )

        self.revision, self.revision_token = self._claim_revision()
        self._plan(specs())
        primary = self._start("template")
        finish_effect(
            primary.effect.pk,
            primary.token,
            provider_namespace="instagram_login:owner-1",
            http_status=200,
            provider_message_id="template-sent",
        )
        denied_sent = claim_next_effect(
            self.revision.pk, self.revision_token, "template_fallback"
        )
        self.assertFalse(denied_sent.token)
        self.assertFalse(
            self.revision.delivery_effects.filter(
                group="template_fallback",
                state=IgRevisionDeliveryEffect.State.PLANNED,
            ).exists()
        )

        self.revision, self.revision_token = self._claim_revision()
        self._plan(specs())
        primary = self._start("template")
        finish_effect(
            primary.effect.pk,
            primary.token,
            provider_namespace="instagram_login:owner-1",
            http_status=400,
            transport_outcome="explicit_rejected",
            explicit_rejection_code="provider_rejected",
        )
        activated = claim_next_effect(
            self.revision.pk, self.revision_token, "template_fallback"
        )
        self.assertTrue(activated.token)

    def test_bindings_need_checkers_and_actor_recipient_secrets_are_rejected(self):
        needs_checker = self._plan(
            [{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}],
            fact_bindings=({"kind": "catalog", "revision": 1},),
        )
        self.assertIn("fact_binding_unavailable", needs_checker.reasons)
        unsupported = self._plan(
            [{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}],
            actor="manager",
        )
        self.assertEqual(unsupported.reasons, ("unsupported_actor_purpose",))
        wrong_recipient = self._plan([
            {
                "group": "substantive_text",
                "kind": "text",
                "payload": {"recipient": {"id": "other"}, "message": {"text": "answer"}},
            },
        ])
        self.assertEqual(wrong_recipient.reasons, ("effect_recipient_mismatch",))
        secret = self._plan([
            {
                "group": "substantive_text",
                "kind": "text",
                "payload": {
                    "recipient": {"id": self.client_row.igsid},
                    "message": {"text": "answer"},
                    "access_token": "never-store",
                },
            },
        ])
        self.assertEqual(secret.reasons, ("effect_payload_invalid",))
        for forbidden_key in ("x-goog-api-key", "x-api-key", "token", "credentials"):
            rejected = self._plan([
                {
                    "group": "substantive_text",
                    "kind": "text",
                    "payload": {
                        "recipient": {"id": self.client_row.igsid},
                        "message": {"text": "answer"},
                        forbidden_key: "never-store",
                    },
                },
            ])
            self.assertEqual(
                rejected.reasons,
                ("effect_payload_invalid",),
                forbidden_key,
            )

    def test_catalog_projection_is_persisted_and_cannot_be_rebound(self):
        metadata = {"part_index": 0, "product_id": 11, "title": "Original title"}
        spec = {
            "group": "catalog_media", "kind": "image",
            "payload": {"recipient": {"id": self.client_row.igsid}, "message": {
                "attachment": {"type": "image", "payload": {"url": "https://twocomms.shop/media/original.jpg"}},
            }},
            "projection_metadata": metadata,
        }
        planned = self._plan([spec])
        self.assertTrue(planned.created, planned.reasons)
        effect = planned.effects[0]
        self.assertEqual(effect.projection_metadata, metadata)
        self.assertEqual(len(effect.projection_digest), 64)
        changed = {**spec, "projection_metadata": {**metadata, "title": "Changed catalog title"}}
        self.assertEqual(self._plan([changed]).reasons, ("plan_conflict",))
        effect.projection_metadata = changed["projection_metadata"]
        with self.assertRaises(ValueError):
            effect.save(update_fields=["projection_metadata"])

    def test_catalog_projection_rejects_unknown_fields_or_wrong_part(self):
        for metadata in (
            {"part_index": 1, "product_id": 11, "title": "Title"},
            {"part_index": 0, "product_id": 11, "title": "Title", "private_url": "forbidden"},
        ):
            with self.subTest(metadata=metadata):
                result = self._plan([{
                    "group": "catalog_media", "kind": "image", "payload": self._payload("unused"),
                    "projection_metadata": metadata,
                }])
                self.assertEqual(result.reasons, ("effect_projection_invalid",))

    def test_pending_permission_transition_blocks_plan_before_epoch_changes(self):
        from management.models import IgPermissionTransitionJob
        from management.services.ig_permission_transitions import create_permission_transition

        create_permission_transition(
            kind=IgPermissionTransitionJob.Kind.GLOBAL_PAUSE,
            dedupe_key="outbox-pause", settings=self.settings,
        )
        result = self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}])
        self.assertIn("permission_transition_pending", result.reasons)
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_changed_sender_allowlist_blocks_planning(self):
        self.settings.allowed_senders = "another-sender"
        self.settings.save(update_fields=["allowed_senders"])
        result = self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}])
        self.assertIn("sender_not_allowed", result.reasons)

    def test_overall_deadline_stops_next_part_before_longer_claim_lease(self):
        planned = self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}])
        self.assertTrue(planned.created, planned.reasons)
        claimed = claim_next_effect(self.revision.pk, self.revision_token, "substantive_text")
        self.assertTrue(claimed.token)
        result = mark_provider_started(
            claimed.effect.pk, claimed.token, self.revision_token,
            now=self.revision.overall_deadline + timedelta(milliseconds=1),
        )
        self.assertEqual(result.reason, "revision_deadline_exhausted")
        self.assertIsNone(result.effect.provider_started_at)

    def test_old_user_event_does_not_get_new_window_from_manager_activity(self):
        self.source.provider_created_at = timezone.now() - timedelta(hours=24)
        self.source.save(update_fields=["provider_created_at"])
        self.client_row.last_message_at = timezone.now()
        self.client_row.save(update_fields=["last_message_at"])
        self.revision, self.revision_token = self._claim_revision()
        result = self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}])
        self.assertIn("reply_window_closed", result.reasons)
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_proven_no_dispatch_settles_definite_failure_without_rewriting_boundary(self):
        self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}])
        claim = self._start("substantive_text")
        result = finish_effect(
            claim.effect.pk, claim.token, provider_namespace="instagram_login:owner-1",
            transport_outcome="known_not_dispatched",
            explicit_rejection_code="transport_preflight_namespace_mismatch",
        )
        self.assertEqual(result.effect.state, IgRevisionDeliveryEffect.State.DEFINITE_FAILED)
        self.assertIsNotNone(result.effect.provider_started_at)
        self.assertIsNone(result.effect.provider_http_status)
        self.assertFalse(result.effect.provider_message_id)

    def test_no_dispatch_claim_with_a_provider_receipt_remains_unknown(self):
        self._plan([{"group": "substantive_text", "kind": "text", "payload": self._payload("answer")}])
        claim = self._start("substantive_text")
        result = finish_effect(
            claim.effect.pk, claim.token, provider_namespace="instagram_login:owner-1",
            transport_outcome="known_not_dispatched", http_status=200,
            provider_message_id="contradictory-receipt",
            explicit_rejection_code="transport_preflight_namespace_mismatch",
        )
        self.assertEqual(result.effect.state, IgRevisionDeliveryEffect.State.UNKNOWN)
