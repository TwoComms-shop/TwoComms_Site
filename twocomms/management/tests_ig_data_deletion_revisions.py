"""Focused B03.8 erasure contracts for revision and delivery payloads."""
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from management.ig_bot_models import BotDataDeletionRequest
from management.models import (
    IgClient,
    IgCustomerTurn,
    IgCustomerTurnRevision,
    IgRevisionDeliveryEffect,
    IgTurnMessage,
    IgTurnRevisionSource,
    IgWebhookInboxEvent,
    InstagramBotSettings,
    InstagramBotMessage,
)
from management.services.ig_turn_revisions import create_collecting_revision


class RevisionErasureTests(TransactionTestCase):
    reset_sequences = True

    def _source_and_revision(self):
        client = IgClient.objects.create(igsid="erase-revision-client", username="erase_revision")
        source = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            provider_namespace="instagram_login:owner",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            mid="erase-revision-mid",
            text="private revision source text",
        )
        now = timezone.now()
        turn = IgCustomerTurn.objects.create(
            client=client,
            primary_source_message=source,
            window_started_at=now,
            window_deadline=now,
        )
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role=source.role)
        revision = create_collecting_revision(turn, [source], now=now, bypass_quiet=True).revision
        proposal = {"reply_text": "private generated reply", "controls": []}
        revision.generation_proposal = proposal
        revision.generation_proposal_digest = hashlib.sha256(
            json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        revision.generation_proposed_at = now
        revision.action_receipts = {
            **revision.action_receipts,
            "client_configuration_update": {"outcome": "private action receipt"},
        }
        revision.save(update_fields=[
            "generation_proposal", "generation_proposal_digest", "generation_proposed_at",
            "action_receipts",
        ])
        payload = {"recipient": {"id": client.igsid}, "message": {"text": "private effect text"}}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        effect = IgRevisionDeliveryEffect.objects.create(
            revision=revision,
            source_message=source,
            effect_key="erase-revision-effect",
            actor="bot",
            purpose="normal_reply",
            group="substantive_text",
            kind="text",
            order_index=0,
            part_index=0,
            part_count=1,
            plan_digest="a" * 64,
            payload=payload,
            payload_digest=digest,
            recipient_igsid=client.igsid,
            provider_namespace=source.provider_namespace,
            settings_id_snapshot=1,
            settings_permission_epoch=0,
            client_permission_epoch=revision.permission_epoch,
            revision_snapshot_digest=revision.snapshot_digest,
            publication_id=1,
            publication_version=1,
            publication_hash="b" * 64,
            authority_context_digest="c" * 64,
        )
        return client, source, revision, effect

    def test_client_erasure_cascades_revision_source_proposal_and_effect(self):
        from management.bot_views import _delete_direct_bot_records

        client, source, revision, effect = self._source_and_revision()
        source_row = IgTurnRevisionSource.objects.get(revision=revision)
        self.assertIn("private revision source text", source_row.text)

        _delete_direct_bot_records(client.username)

        self.assertFalse(IgCustomerTurnRevision.objects.filter(pk=revision.pk).exists())
        self.assertFalse(IgTurnRevisionSource.objects.filter(pk=source_row.pk).exists())
        self.assertFalse(IgRevisionDeliveryEffect.objects.filter(pk=effect.pk).exists())
        self.assertFalse(InstagramBotMessage.objects.filter(pk=source.pk).exists())

    def test_blob_callback_observes_committed_fence_outside_transaction(self):
        from management.bot_views import _delete_direct_bot_records

        client = IgClient.objects.create(igsid="erase-fence-client", username="erase_fence")
        message = InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            private_media_state="active",
            attachment_media=[{"status": "owned", "private_storage": True}],
        )
        observed = {}

        def observe(message_ids, **_kwargs):
            client.refresh_from_db()
            observed["fenced"] = client.privacy_erasure_started_at is not None
            observed["atomic"] = connection.in_atomic_block
            return len(message_ids)

        with patch("management.services.ig_private_media.delete_immediately", side_effect=observe):
            _delete_direct_bot_records(client.username)

        self.assertTrue(observed["fenced"])
        self.assertFalse(observed["atomic"])
        self.assertFalse(InstagramBotMessage.objects.filter(pk=message.pk).exists())

    def test_blob_failure_leaves_durable_request_claim_and_client_fence(self):
        from management.services.ig_data_deletion import fulfill_deletion_request

        client = IgClient.objects.create(igsid="erase-failure-client", username="erase_failure")
        InstagramBotMessage.objects.create(
            client=client,
            sender_id=client.igsid,
            role=InstagramBotMessage.Role.USER,
            private_media_state="active",
            attachment_media=[{"status": "owned", "private_storage": True}],
        )
        request = BotDataDeletionRequest.objects.create(
            confirmation_code="ERASEFAIL",
            source=BotDataDeletionRequest.Source.MANUAL_FORM,
            identifier=client.username,
            normalized_identifier=client.username,
            status=BotDataDeletionRequest.Status.PENDING_VERIFICATION,
        )

        with patch(
            "management.services.ig_private_media.delete_immediately",
            side_effect=RuntimeError("private storage unavailable"),
        ), self.assertRaisesRegex(RuntimeError, "private storage unavailable"):
            fulfill_deletion_request(request, actor_label="manager:test")

        client.refresh_from_db()
        request.refresh_from_db()
        self.assertIsNotNone(client.privacy_erasure_started_at)
        self.assertEqual(request.status, BotDataDeletionRequest.Status.ERASING)
        self.assertTrue(request.erasure_lease_token)

    def test_recovered_claim_cannot_erase_recreated_same_igsid(self):
        from management.services.ig_data_deletion import fulfill_deletion_request
        from management.services.ig_webhook_inbox import (
            _namespace, accept_webhook, drain_webhook_inbox,
        )

        old = IgClient.objects.create(igsid="erase-recreated", username="erase_recreated")
        settings_obj = InstagramBotSettings.load()
        settings_obj.page_id = "erase-owner"
        settings_obj.save(update_fields=["page_id"])
        namespace, owner = _namespace(settings_obj)
        replay_payload = {
            "object": "instagram",
            "entry": [{"id": owner, "messaging": [{
                "sender": {"id": old.igsid},
                "recipient": {"id": owner},
                "message": {"mid": "erase-old-mid", "text": "old private receipt"},
            }]}],
        }
        import json
        self.assertEqual(accept_webhook(json.dumps(replay_payload).encode(), settings_obj).accepted, 1)
        old_inbox = IgWebhookInboxEvent.objects.get(namespace=namespace)
        request = BotDataDeletionRequest.objects.create(
            confirmation_code="ERASERECREATE",
            source=BotDataDeletionRequest.Source.MANUAL_FORM,
            identifier=old.username,
            normalized_identifier=old.username,
            status=BotDataDeletionRequest.Status.PENDING_VERIFICATION,
        )
        with patch(
            "management.services.ig_data_deletion._settle_erasure",
            side_effect=RuntimeError("crash before settlement"),
        ), self.assertRaisesRegex(RuntimeError, "crash before settlement"):
            fulfill_deletion_request(request, actor_label="manager:test")

        recreated = IgClient.objects.create(igsid="erase-recreated", username="new_account")
        BotDataDeletionRequest.objects.filter(pk=request.pk).update(
            erasure_lease_until=timezone.now() - timedelta(seconds=1)
        )
        fulfill_deletion_request(request, actor_label="manager:recovery")

        self.assertTrue(IgClient.objects.filter(pk=recreated.pk).exists())
        old_inbox.refresh_from_db()
        self.assertEqual(old_inbox.decision, IgWebhookInboxEvent.Decision.REJECTED)
        self.assertEqual(old_inbox.reason, "privacy_erased")
        self.assertEqual(old_inbox.customer_igsid, "")
        self.assertEqual(old_inbox.payload, {})
        self.assertIsNotNone(old_inbox.processed_at)
        replay = accept_webhook(json.dumps(replay_payload).encode(), settings_obj)
        self.assertEqual(replay.duplicates, 1)
        self.assertEqual(drain_webhook_inbox(settings_obj, limit=1), 0)
        self.assertFalse(InstagramBotMessage.objects.filter(mid="erase-old-mid").exists())
        new_inbox = IgWebhookInboxEvent.objects.create(
            namespace=namespace,
            owner_id=owner,
            customer_igsid=recreated.igsid,
            event_key="erase-new-inbox",
            decision=IgWebhookInboxEvent.Decision.ACCEPTED,
            payload={"private": "new accepted receipt"},
            payload_digest="e" * 64,
        )
        self.assertTrue(IgWebhookInboxEvent.objects.filter(pk=new_inbox.pk).exists())
        request.refresh_from_db()
        self.assertEqual(request.status, BotDataDeletionRequest.Status.COMPLETED)
        self.assertEqual(request.erasure_actor_label, "manager:test")

    def test_claim_is_single_owner_recovers_only_after_lease_and_outer_atomic_is_rejected(self):
        from management.services.ig_data_deletion import (
            DeletionRequestNotActionable,
            _claim_erasure,
            fulfill_deletion_request,
        )

        client = IgClient.objects.create(igsid="erase-claim-client", username="erase_claim")
        request = BotDataDeletionRequest.objects.create(
            confirmation_code="ERASECLAIM",
            source=BotDataDeletionRequest.Source.MANUAL_FORM,
            identifier=client.username,
            normalized_identifier=client.username,
            status=BotDataDeletionRequest.Status.PENDING_VERIFICATION,
        )
        first = _claim_erasure(request.pk, actor="manager:test")
        with self.assertRaises(DeletionRequestNotActionable):
            _claim_erasure(request.pk, actor="manager:second")
        BotDataDeletionRequest.objects.filter(pk=request.pk).update(
            erasure_lease_until=timezone.now() - timedelta(seconds=1)
        )
        recovered = _claim_erasure(request.pk, actor="manager:recovery")
        self.assertNotEqual(first.token, recovered.token)
        request.refresh_from_db()
        self.assertEqual(request.erasure_actor_label, "manager:test")

        request.refresh_from_db()
        request.status = BotDataDeletionRequest.Status.PENDING_VERIFICATION
        request.erasure_lease_token = ""
        request.erasure_lease_until = None
        request.save(update_fields=["status", "erasure_lease_token", "erasure_lease_until"])
        with transaction.atomic(), self.assertRaises(DeletionRequestNotActionable):
            fulfill_deletion_request(request, actor_label="manager:test")
        self.assertTrue(IgClient.objects.filter(pk=client.pk).exists())
