from datetime import UTC, datetime, timedelta
import hashlib
import json
from unittest.mock import patch

from django.test import TestCase

from management.models import (
    IgClient,
    IgCustomerTurn,
    IgCustomerTurnRevision,
    IgRevisionDeliveryEffect,
    IgTurnMessage,
    InstagramBotMessage,
)
from management.services.ig_turn_revisions import (
    MAX_SOURCES,
    claim_revision_preparation,
    claim_sealed_revision,
    create_collecting_revision,
    create_refresh_successor,
    replay_snapshot,
    revision_claim_is_current,
    seal_revision,
)


class TurnRevisionTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        self.client_row = IgClient.objects.create(
            igsid="turn-revision-client",
            reply_permission_epoch=7,
        )
        self.first = self.message("Перше повідомлення", mid="turn-rev-1")
        self.turn = IgCustomerTurn.objects.create(
            client=self.client_row,
            primary_source_message=self.first,
            window_started_at=self.now,
            window_deadline=self.now + timedelta(seconds=6),
        )
        IgTurnMessage.objects.create(
            turn=self.turn,
            message=self.first,
            ordinal=1,
            role=self.first.role,
        )

    def message(self, text, *, mid, media=None):
        return InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            role=InstagramBotMessage.Role.USER,
            text=text,
            mid=mid,
            provider_namespace="instagram_login:owner-1",
            attachment_media=list(media or []),
            provider_created_at=self.now,
            reply_to_provider_message_id="reply-parent",
            quick_reply_payload="",
            status=InstagramBotMessage.Status.PENDING,
        )

    def add_to_turn(self, message):
        IgTurnMessage.objects.create(
            turn=self.turn,
            message=message,
            ordinal=self.turn.turn_messages.count() + 1,
            role=message.role,
        )

    def claimed_revision(self, *, overall_deadline=None, source_metadata=None):
        revision = create_collecting_revision(
            self.turn,
            [self.first],
            source_metadata=source_metadata,
            now=self.now,
            bypass_quiet=True,
            overall_deadline=overall_deadline,
        ).revision
        preparation = claim_revision_preparation(revision.pk, now=self.now)
        sealed = seal_revision(revision.pk, preparation.token, now=self.now)
        self.assertTrue(sealed.sealed, sealed.reason)
        claimed = claim_sealed_revision(revision.pk, now=self.now)
        self.assertTrue(claimed.token, claimed.reason)
        return claimed.revision, claimed.token

    def delivery_effect(self, revision, *, state, index=0):
        payload = {
            "recipient": {"id": self.client_row.igsid},
            "message": {"text": "planned reply"},
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return IgRevisionDeliveryEffect.objects.create(
            revision=revision,
            source_message=self.first,
            effect_key=f"refresh-effect-{revision.pk}-{index}",
            actor="bot",
            purpose="normal_reply",
            group="substantive_text",
            kind="text",
            order_index=index,
            part_index=index,
            part_count=1,
            plan_digest="a" * 64,
            payload=payload,
            payload_digest=hashlib.sha256(encoded).hexdigest(),
            recipient_igsid=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1",
            settings_id_snapshot=1,
            settings_permission_epoch=0,
            client_permission_epoch=revision.permission_epoch,
            revision_snapshot_digest=revision.snapshot_digest,
            publication_id=1,
            publication_version=1,
            publication_hash="b" * 64,
            authority_context_digest="c" * 64,
            state=state,
        )

    def test_each_inbound_advances_active_revision_and_quiet_caps_at_four_seconds(self):
        first = create_collecting_revision(
            self.turn,
            [self.first],
            source_metadata={
                self.first.pk: {
                    "referral": {
                        "ref": "campaign",
                        "ad_id": "123",
                        "photo_url": "https://signed.invalid/creative",
                    }
                }
            },
            now=self.now,
        ).revision
        second_message = self.message("Друге повідомлення", mid="turn-rev-2")
        self.add_to_turn(second_message)
        second = create_collecting_revision(
            self.turn,
            [self.first, second_message],
            now=self.now + timedelta(seconds=1),
        ).revision
        third_message = self.message("Третє повідомлення", mid="turn-rev-3")
        self.add_to_turn(third_message)
        third = create_collecting_revision(
            self.turn,
            [self.first, second_message, third_message],
            now=self.now + timedelta(seconds=3.5),
        ).revision

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual([first.revision, second.revision, third.revision], [1, 2, 3])
        self.assertEqual(first.state, first.State.SUPERSEDED)
        self.assertEqual(second.state, second.State.SUPERSEDED)
        self.assertEqual(third.quiet_started_at, self.now)
        self.assertEqual(third.quiet_cap_at, self.now + timedelta(seconds=4))
        self.assertEqual(third.quiet_deadline, third.quiet_cap_at)
        self.assertEqual(third.overall_deadline, self.now + timedelta(seconds=45))
        self.assertEqual(third.sources.count(), 3)
        source = third.sources.get(message_id=self.first.pk)
        self.assertEqual(source.referral["ref"], "campaign")
        self.assertNotIn("photo_url", source.referral)
        self.assertEqual(
            IgCustomerTurnRevision.objects.filter(
                client=self.client_row, active_slot=1
            ).get().pk,
            third.pk,
        )

    def test_media_preparation_budget_is_separate_from_quiet_and_send_reserve(self):
        overall = self.now + timedelta(seconds=8)
        revision = create_collecting_revision(
            self.turn,
            [self.first],
            now=self.now,
            bypass_quiet=True,
            overall_deadline=overall,
        ).revision

        claim = claim_revision_preparation(revision.pk, now=self.now)

        self.assertTrue(claim.token)
        self.assertEqual(revision.quiet_cap_at, self.now + timedelta(seconds=4))
        self.assertEqual(claim.media_prepare_deadline, self.now + timedelta(seconds=3))
        self.assertEqual(revision.overall_deadline, overall)

    def test_seal_is_write_once_and_redacts_media_transport_details(self):
        self.first.attachment_media = [{
            "source_part_id": "mp1_" + "a" * 32,
            "original_index": 0,
            "type": "image",
            "role": "receipt",
            "capture_state": "owned",
            "mime": "image/jpeg",
            "bytes": 321,
            "content_hash": "a" * 64,
            "private_storage": True,
            "storage_name": "ig-private/test/customer.jpg",
            "url": "https://signed.invalid/customer",
            "ocr_text": "secret receipt text",
        }]
        self.first.private_media_state = InstagramBotMessage.PrivateMediaState.ACTIVE
        self.first.save(update_fields=["attachment_media", "private_media_state"])
        revision = create_collecting_revision(
            self.turn, [self.first], now=self.now, bypass_quiet=True
        ).revision
        claim = claim_revision_preparation(revision.pk, now=self.now)

        sealed = seal_revision(revision.pk, claim.token, now=self.now)

        self.assertTrue(sealed.sealed)
        snapshot = replay_snapshot(revision.pk)
        self.assertEqual(snapshot["coverage"]["owned_media_parts"], 1)
        part = snapshot["sources"][0]["media_parts"][0]
        self.assertEqual(part["content_hash"], "a" * 64)
        self.assertEqual(
            part["owner_ref"],
            {"message_id": self.first.pk, "source_part_id": "mp1_" + "a" * 32},
        )
        rendered = str(snapshot)
        self.assertNotIn("signed.invalid", rendered)
        self.assertNotIn("storage_name", rendered)
        self.assertNotIn("secret receipt", rendered)

        original = revision.__class__.objects.get(pk=revision.pk).bundle_snapshot
        self.first.attachment_media[0]["capture_state"] = "unavailable"
        self.first.save(update_fields=["attachment_media"])
        repeated = seal_revision(revision.pk, claim.token, now=self.now + timedelta(seconds=20))
        self.assertTrue(repeated.sealed)
        self.assertEqual(
            revision.__class__.objects.get(pk=revision.pk).bundle_snapshot,
            original,
        )

        sealed.revision.bundle_snapshot = {"tampered": True}
        with self.assertRaises(ValueError):
            sealed.revision.save(update_fields=["bundle_snapshot", "updated_at"])

    def test_pending_media_waits_until_prepare_deadline_then_seals_unavailable(self):
        self.first.attachment_media = [
            {
                "source_part_id": "mp1_" + "b" * 32,
                "original_index": 0,
                "type": "image",
                "status": "acquiring",
                "capture_state": "fetching",
                "url": "https://signed.invalid/pending",
            },
            {
                "source_part_id": "mp1_" + "c" * 32,
                "original_index": 1,
                "type": "image",
                "status": "unavailable",
                "capture_failure_class": "temporary",
                "capture_retryable": True,
                "capture_terminal": False,
                "url": "https://signed.invalid/retry",
            },
        ]
        self.first.save(update_fields=["attachment_media"])
        revision = create_collecting_revision(
            self.turn, [self.first], now=self.now, bypass_quiet=True
        ).revision
        claim = claim_revision_preparation(revision.pk, now=self.now)

        waiting = seal_revision(
            revision.pk,
            claim.token,
            now=claim.media_prepare_deadline - timedelta(microseconds=1),
        )
        self.assertFalse(waiting.sealed)
        self.assertEqual(waiting.reason, "media_pending")

        sealed = seal_revision(
            revision.pk, claim.token, now=claim.media_prepare_deadline
        )
        parts = sealed.revision.bundle_snapshot["sources"][0]["media_parts"]
        self.assertTrue(sealed.sealed)
        self.assertEqual(
            [part["capture_outcome"] for part in parts],
            ["unavailable", "unavailable"],
        )
        self.assertEqual(
            [part["reason"] for part in parts],
            ["media_prepare_deadline", "media_prepare_deadline"],
        )

    def test_manifest_canonicalizes_legacy_duplicates_and_seals_owned_audio(self):
        duplicate_url = "https://signed.invalid/same"
        self.first.attachment_media = [
            {"url": duplicate_url, "original_index": 0},
            {"url": duplicate_url, "original_index": 1},
        ]
        self.first.save(update_fields=["attachment_media"])
        legacy = create_collecting_revision(
            self.turn, [self.first], now=self.now, bypass_quiet=True
        ).revision.sources.get().discovered_media

        self.assertEqual(len(legacy), 2)
        self.assertNotEqual(legacy[0]["source_part_id"], legacy[1]["source_part_id"])
        self.assertTrue(all(part["source_part_id"].startswith("mp1_") for part in legacy))
        self.assertEqual([part["original_index"] for part in legacy], [0, 1])
        self.assertTrue(all(part["identity_origin"] == "legacy_positional" for part in legacy))
        self.assertTrue(all(part["type"] == "unknown" for part in legacy))

        audio_client = IgClient.objects.create(igsid="turn-revision-audio")
        audio = InstagramBotMessage.objects.create(
            client=audio_client,
            sender_id=audio_client.igsid,
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text="",
            mid="turn-revision-audio-mid",
            provider_namespace="instagram_login:owner-1",
            private_media_state=InstagramBotMessage.PrivateMediaState.ACTIVE,
            attachment_media=[{
                "source_part_id": "mp1_" + "d" * 32,
                "original_index": 0,
                "type": "audio",
                "status": "owned",
                "mime": "audio/mpeg",
                "bytes": 512,
                "content_hash": "d" * 64,
                "private_storage": True,
                "storage_name": "ig-private/test/audio.mp3",
            }],
            status=InstagramBotMessage.Status.PENDING,
        )
        audio_turn = IgCustomerTurn.objects.create(
            client=audio_client,
            primary_source_message=audio,
            window_started_at=self.now,
            window_deadline=self.now,
        )
        IgTurnMessage.objects.create(
            turn=audio_turn, message=audio, ordinal=1, role=audio.role
        )
        audio_revision = create_collecting_revision(
            audio_turn, [audio], now=self.now, bypass_quiet=True
        ).revision
        claim = claim_revision_preparation(audio_revision.pk, now=self.now)
        sealed = seal_revision(audio_revision.pk, claim.token, now=self.now)
        audio_part = sealed.revision.bundle_snapshot["sources"][0]["media_parts"][0]

        self.assertEqual(audio_part["capture_outcome"], "owned")
        self.assertEqual(audio_part["mime"], "audio/mpeg")

    def test_overflow_is_durable_and_requires_root_created_successor(self):
        messages = [self.first]
        for index in range(1, MAX_SOURCES + 1):
            message = self.message(f"source {index}", mid=f"overflow-{index}")
            self.add_to_turn(message)
            messages.append(message)

        result = create_collecting_revision(self.turn, messages, now=self.now)

        self.assertTrue(result.created)
        self.assertTrue(result.successor_required)
        self.assertEqual(result.reason, "source_count_exceeded")
        self.assertEqual(result.revision.state, result.revision.State.OVERFLOW)
        self.assertEqual(result.revision.sources.count(), 0)
        self.assertEqual(
            result.revision.overflow["source_message_ids"],
            [message.pk for message in messages],
        )
        self.assertTrue(result.revision.overflow["successor_required"])

    def test_claim_is_single_owner_and_later_revision_invalidates_it(self):
        revision = create_collecting_revision(
            self.turn, [self.first], now=self.now, bypass_quiet=True
        ).revision
        preparation = claim_revision_preparation(revision.pk, now=self.now)
        self.assertTrue(seal_revision(revision.pk, preparation.token, now=self.now).sealed)
        first_claim = claim_sealed_revision(revision.pk, now=self.now)
        second_claim = claim_sealed_revision(revision.pk, now=self.now)

        self.assertTrue(first_claim.token)
        self.assertFalse(second_claim.token)
        with patch("management.services.ig_turn_revisions.timezone.now", return_value=self.now):
            self.assertTrue(revision_claim_is_current(revision.pk, first_claim.token))

        # Same historical source may participate in a later/manual revision;
        # the source message and its original turn membership remain unchanged.
        successor = create_collecting_revision(
            self.turn,
            [self.first],
            now=self.now + timedelta(seconds=1),
        ).revision
        revision.refresh_from_db()
        self.assertEqual(successor.parent_id, revision.pk)
        self.assertEqual(successor.sources.get().message_id, self.first.pk)
        self.assertEqual(self.first.turn_membership.turn_id, self.turn.pk)
        self.assertFalse(revision_claim_is_current(revision.pk, first_claim.token))

    def test_expired_preparation_lease_can_be_reclaimed(self):
        revision = create_collecting_revision(
            self.turn, [self.first], now=self.now, bypass_quiet=True
        ).revision
        first = claim_revision_preparation(revision.pk, now=self.now)
        revision.refresh_from_db()

        early = claim_revision_preparation(
            revision.pk, now=revision.lease_until - timedelta(microseconds=1)
        )
        reclaimed = claim_revision_preparation(
            revision.pk, now=revision.lease_until
        )

        self.assertFalse(early.token)
        self.assertTrue(reclaimed.token)
        self.assertNotEqual(first.token, reclaimed.token)

    def test_refresh_successor_copies_sealed_envelopes_not_mutable_message(self):
        revision, token = self.claimed_revision(source_metadata={
            self.first.pk: {
                "referral": {"ref": "sealed-campaign", "ad_id": "42"}
            }
        })
        original_snapshot = revision.bundle_snapshot
        original_source = revision.sources.get()
        self.first.text = "mutable row was changed after generation"
        self.first.save(update_fields=["text"])
        planned = self.delivery_effect(
            revision, state=IgRevisionDeliveryEffect.State.PLANNED
        )

        result = create_refresh_successor(
            revision.pk,
            token,
            reason=IgCustomerTurnRevision.SuccessorReason.PUBLICATION_CHANGED,
            now=self.now + timedelta(seconds=1),
        )

        self.assertTrue(result.created, result.reason)
        child = result.revision
        copied = child.sources.get()
        self.assertEqual(child.origin, child.Origin.AUTO_REFRESH)
        self.assertEqual(child.parent_id, revision.pk)
        self.assertEqual(child.bundle_snapshot, original_snapshot)
        self.assertEqual(child.snapshot_digest, revision.snapshot_digest)
        self.assertEqual(copied.text, original_source.text)
        self.assertNotEqual(copied.text, self.first.text)
        self.assertEqual(copied.referral, original_source.referral)
        self.assertEqual(copied.source_digest, original_source.source_digest)
        planned.refresh_from_db()
        self.assertEqual(planned.state, planned.State.CANCELLED)
        self.assertEqual(planned.failure_code, "revision_refreshed")
        self.assertEqual(self.first.turn_membership.turn_id, self.turn.pk)

    def test_refresh_is_idempotent_single_hop_and_keeps_original_deadline(self):
        deadline = self.now + timedelta(seconds=20)
        revision, token = self.claimed_revision(overall_deadline=deadline)
        first = create_refresh_successor(
            revision.pk,
            token,
            reason=IgCustomerTurnRevision.SuccessorReason.FACT_BINDING_STALE,
            now=self.now + timedelta(seconds=2),
        )
        repeated = create_refresh_successor(
            revision.pk,
            token,
            reason=IgCustomerTurnRevision.SuccessorReason.FACT_BINDING_STALE,
            now=self.now + timedelta(seconds=3),
        )

        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.reason, "existing_successor")
        self.assertEqual(first.revision.pk, repeated.revision.pk)
        self.assertEqual(first.revision.overall_deadline, deadline)
        self.assertEqual(
            IgCustomerTurnRevision.objects.filter(parent=revision).count(), 1
        )

        child_claim = claim_sealed_revision(
            first.revision.pk, now=self.now + timedelta(seconds=4)
        )
        denied = create_refresh_successor(
            first.revision.pk,
            child_claim.token,
            reason=(
                IgCustomerTurnRevision.SuccessorReason.PUBLIC_POLICY_INPUTS_STALE
            ),
            now=self.now + timedelta(seconds=5),
        )
        self.assertEqual(denied.reason, "refresh_limit_reached")

    def test_newer_inbound_head_and_takeover_block_refresh(self):
        revision, token = self.claimed_revision()
        second = self.message("new inbound", mid="turn-rev-newer")
        self.add_to_turn(second)
        newer = create_collecting_revision(
            self.turn,
            [self.first, second],
            now=self.now + timedelta(seconds=1),
        ).revision

        stale = create_refresh_successor(
            revision.pk,
            token,
            reason=IgCustomerTurnRevision.SuccessorReason.PUBLICATION_CHANGED,
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(stale.reason, "newer_head_exists")
        self.assertEqual(
            IgCustomerTurnRevision.objects.get(active_slot=1).pk, newer.pk
        )

        # A fresh client/turn proves takeover independently of the newer-head
        # fence above.
        other = IgClient.objects.create(
            igsid="refresh-takeover", reply_permission_epoch=1
        )
        message = InstagramBotMessage.objects.create(
            client=other,
            sender_id=other.igsid,
            role=InstagramBotMessage.Role.USER,
            text="question",
            mid="refresh-takeover-source",
            provider_namespace="instagram_login:owner-1",
            status=InstagramBotMessage.Status.PENDING,
        )
        turn = IgCustomerTurn.objects.create(
            client=other,
            primary_source_message=message,
            window_started_at=self.now,
            window_deadline=self.now,
        )
        IgTurnMessage.objects.create(
            turn=turn, message=message, ordinal=1, role=message.role
        )
        other_revision = create_collecting_revision(
            turn, [message], now=self.now, bypass_quiet=True
        ).revision
        prep = claim_revision_preparation(other_revision.pk, now=self.now)
        seal_revision(other_revision.pk, prep.token, now=self.now)
        other_claim = claim_sealed_revision(other_revision.pk, now=self.now)
        other.manager_takeover = True
        other.save(update_fields=["manager_takeover", "updated_at"])
        takeover = create_refresh_successor(
            other_revision.pk,
            other_claim.token,
            reason=IgCustomerTurnRevision.SuccessorReason.PUBLICATION_CHANGED,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(takeover.reason, "client_not_eligible")

    def test_unknown_delivery_and_exhausted_budget_reject_successor(self):
        revision, token = self.claimed_revision(
            overall_deadline=self.now + timedelta(seconds=10)
        )
        self.delivery_effect(
            revision, state=IgRevisionDeliveryEffect.State.UNKNOWN
        )
        unknown = create_refresh_successor(
            revision.pk,
            token,
            reason=IgCustomerTurnRevision.SuccessorReason.FACT_BINDING_STALE,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(unknown.reason, "delivery_outcome_uncertain")
        revision.refresh_from_db()
        self.assertEqual(revision.active_slot, 1)

        IgRevisionDeliveryEffect.objects.filter(revision=revision).delete()
        expired = create_refresh_successor(
            revision.pk,
            token,
            reason=IgCustomerTurnRevision.SuccessorReason.FACT_BINDING_STALE,
            now=self.now + timedelta(seconds=10),
        )
        self.assertEqual(expired.reason, "overall_deadline_expired")
