import hashlib
import json

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from management.models import (
    BotPolicyPublication,
    IgClient,
    IgCustomerTurn,
    IgRevisionDeliveryEffect,
    IgTurnMessage,
    InstagramBotMessage,
    InstagramBotSettings,
)
from management.services.ig_catalog_media import (
    CatalogMediaItem,
    CatalogMediaSelection,
    CatalogMediaState,
    prepare_catalog_media,
)
from management.services.ig_message_templates import (
    GenericTemplate,
    TemplateCard,
    prepare_template_effects,
)
from management.services.ig_revision_delivery import (
    ProviderPartResult,
    drain_group,
)
from management.services.ig_revision_outbox import (
    PublicationBinding,
    plan_revision_effects,
)
from management.services.ig_turn_revisions import (
    claim_revision_preparation,
    claim_sealed_revision,
    create_collecting_revision,
    seal_revision,
)


@override_settings(SITE_BASE_URL="https://twocomms.test")
class RevisionDeliveryTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
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
        self.client_row = IgClient.objects.create(
            igsid="revision-delivery-client",
            reply_permission_epoch=3,
        )
        self.source = InstagramBotMessage.objects.create(
            client=self.client_row,
            sender_id=self.client_row.igsid,
            provider_namespace="instagram_login:owner-1",
            role=InstagramBotMessage.Role.USER,
            source="webhook",
            text="question",
            mid="revision-delivery-source",
            status=InstagramBotMessage.Status.PENDING,
        )
        now = timezone.now()
        turn = IgCustomerTurn.objects.create(
            client=self.client_row,
            primary_source_message=self.source,
            window_started_at=now,
            window_deadline=now,
        )
        IgTurnMessage.objects.create(
            turn=turn, message=self.source, ordinal=1, role=self.source.role
        )
        revision = create_collecting_revision(
            turn, [self.source], now=now, bypass_quiet=True
        ).revision
        preparation = claim_revision_preparation(revision.pk, now=now)
        revision = seal_revision(revision.pk, preparation.token, now=now).revision
        claim = claim_sealed_revision(revision.pk, now=now)
        self.revision = claim.revision
        self.revision_token = claim.token
        self.binding = PublicationBinding(
            self.publication.pk,
            self.publication.version,
            self.publication.snapshot_hash,
        )

    def _payload(self, text):
        return {
            "recipient": {"id": self.client_row.igsid},
            "message": {"text": text},
        }

    def _plan(self, specs):
        result = plan_revision_effects(
            self.revision.pk,
            self.revision_token,
            source_message_id=self.source.pk,
            settings_id=self.settings.pk,
            settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=self.binding,
            authority_context_digest="e" * 64,
            effects=specs,
        )
        self.assertTrue(result.created, result.reasons)
        return result

    def test_three_chunks_stop_at_second_unknown_and_resume_never_repeats_first(self):
        self._plan([
            {"group": "substantive_text", "kind": "text", "payload": self._payload("one")},
            {"group": "substantive_text", "kind": "text", "payload": self._payload("two")},
            {"group": "substantive_text", "kind": "text", "payload": self._payload("three")},
        ])
        calls = []

        def transport(payload):
            calls.append(payload["message"]["text"])
            if len(calls) == 1:
                return ProviderPartResult(
                    "instagram_login:owner-1", 200, "receipt-one"
                )
            return ProviderPartResult(
                "instagram_login:owner-1", 503, outcome="response"
            )

        first = drain_group(
            self.revision.pk,
            self.revision_token,
            "substantive_text",
            transport,
        )
        resumed_calls = []
        resumed = drain_group(
            self.revision.pk,
            self.revision_token,
            "substantive_text",
            lambda payload: resumed_calls.append(payload),
        )

        self.assertEqual(first.state, "unknown")
        self.assertEqual(calls, ["one", "two"])
        self.assertEqual(
            [(part.part_index, part.provider_message_id) for part in first.sent_parts],
            [(0, "receipt-one")],
        )
        self.assertEqual(resumed.state, "unknown")
        self.assertEqual(resumed_calls, [])

    def test_transport_receives_fresh_db_payload_copy_outside_atomic(self):
        caller_payload = self._payload("canonical")
        plan = self._plan([
            {"group": "substantive_text", "kind": "text", "payload": caller_payload},
        ])
        caller_payload["message"]["text"] = "caller-mutated"
        observed = {}

        def transport(payload):
            observed["atomic"] = connection.in_atomic_block
            observed["text"] = payload["message"]["text"]
            payload["message"]["text"] = "callback-mutated"
            return ProviderPartResult(
                "instagram_login:owner-1", 200, "canonical-receipt"
            )

        result = drain_group(
            self.revision.pk,
            self.revision_token,
            "substantive_text",
            transport,
        )

        self.assertEqual(result.state, "sent")
        self.assertFalse(observed["atomic"])
        self.assertEqual(observed["text"], "canonical")
        plan.effects[0].refresh_from_db()
        self.assertEqual(plan.effects[0].payload["message"]["text"], "canonical")

    def test_media_unknown_does_not_block_independent_text(self):
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
        media = drain_group(
            self.revision.pk,
            self.revision_token,
            "catalog_media",
            lambda _payload: ProviderPartResult(
                "instagram_login:owner-1", outcome="timeout"
            ),
        )
        text = drain_group(
            self.revision.pk,
            self.revision_token,
            "substantive_text",
            lambda _payload: ProviderPartResult(
                "instagram_login:owner-1", 200, "text-receipt"
            ),
        )

        self.assertEqual(media.state, "unknown")
        self.assertEqual(text.state, "sent")
        self.assertEqual(text.sent_parts[0].provider_message_id, "text-receipt")

    def test_template_unknown_terminalizes_fallback_without_calling_it(self):
        prepared = prepare_template_effects(
            self.client_row.igsid,
            GenericTemplate(
                cards=(TemplateCard(title="Card", subtitle="Subtitle"),),
                fallback_text="Fallback text",
                projection_text="Card projection",
            ),
        )
        self._plan(prepared.effects)
        primary = drain_group(
            self.revision.pk,
            self.revision_token,
            "template",
            lambda _payload: ProviderPartResult(
                "instagram_login:owner-1", outcome="timeout"
            ),
        )
        fallback_calls = []
        fallback = drain_group(
            self.revision.pk,
            self.revision_token,
            "template_fallback",
            lambda payload: fallback_calls.append(payload),
        )

        self.assertEqual(primary.state, "unknown")
        self.assertEqual(fallback.state, "cancelled")
        self.assertEqual(fallback_calls, [])

    def test_catalog_preparation_returns_exact_payloads_and_safe_projection(self):
        selection = CatalogMediaSelection(
            state=CatalogMediaState.READY,
            items=(
                CatalogMediaItem(
                    "https://twocomms.test/a.jpg",
                    "First",
                    "First alt",
                    10,
                    "image/jpeg",
                    100,
                ),
                CatalogMediaItem(
                    "https://twocomms.test/b.png",
                    "Second",
                    "Second alt",
                    20,
                    "image/png",
                    200,
                ),
            ),
        )

        prepared = prepare_catalog_media(
            self.settings, self.client_row.igsid, selection
        )

        self.assertEqual(len(prepared.payloads), 2)
        self.assertEqual(
            [payload["message"]["attachment"]["payload"]["url"] for payload in prepared.payloads],
            ["https://twocomms.test/a.jpg", "https://twocomms.test/b.png"],
        )
        self.assertEqual(
            prepared.product_refs,
            (
                {"part_index": 0, "product_id": 10, "title": "First"},
                {"part_index": 1, "product_id": 20, "title": "Second"},
            ),
        )


@override_settings(SITE_BASE_URL="https://twocomms.test")
class RevisionFactoryPreflightTests(TransactionTestCase):
    reset_sequences = True
    _payload = RevisionDeliveryTests._payload
    _plan = RevisionDeliveryTests._plan

    def setUp(self):
        import os
        from unittest.mock import patch

        RevisionDeliveryTests.setUp(self)
        self.environment = patch.dict(os.environ, {"IG_PROVIDER_TRANSPORT": "instagram_login"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings.ig_user_id = "owner-1"
        self.settings.save(update_fields=["ig_user_id"])

    def _callback(self, **overrides):
        from management.services.ig_revision_transport import build_provider_part_callback

        values = dict(expected_namespace="instagram_login:owner-1",
                      expected_recipient=self.client_row.igsid,
                      access_token="memory-only-test-token")
        values.update(overrides)
        return build_provider_part_callback(self.settings, **values)

    def _text_plan(self, payload=None):
        return self._plan([{
            "group": "substantive_text", "kind": "text",
            "payload": self._payload("Exact canonical text") if payload is None else payload,
        }])

    def _drain(self, callback):
        return drain_group(self.revision.pk, self.revision_token, "substantive_text", callback)

    def _assert_no_start(self, callback, reason, *, payload=None):
        from unittest.mock import patch

        effect = self._text_plan(payload).effects[0]
        with (
            patch("management.services.instagram_bot._provider_http") as http,
            patch("management.services.ig_revision_delivery.mark_provider_started") as start,
            patch("management.services.instagram_bot._register_outgoing_message") as register,
        ):
            result = self._drain(callback)
        http.assert_not_called()
        start.assert_not_called()
        register.assert_not_called()
        effect.refresh_from_db()
        self.assertEqual(effect.state, effect.State.CANCELLED)
        self.assertIsNone(effect.provider_started_at)
        self.assertEqual(effect.provider_message_id, "")
        self.assertIsNone(effect.provider_http_status)
        self.assertEqual(effect.failure_code, reason)
        self.assertEqual(result.attempted, 0)

    def test_bad_namespace_does_not_record_start_or_unknown(self):
        self._assert_no_start(self._callback(expected_namespace="instagram_login:other"),
                              "transport_preflight_namespace_mismatch")

    def test_empty_credential_does_not_record_start_or_unknown(self):
        self._assert_no_start(self._callback(access_token=""),
                              "transport_preflight_credentials_unavailable")

    def test_invalid_payload_does_not_record_start_or_unknown(self):
        self._assert_no_start(self._callback(), "transport_preflight_payload_invalid",
                              payload=self._payload(""))

    def test_url_policy_failure_does_not_record_start_or_unknown(self):
        from unittest.mock import patch

        with patch("management.services.instagram_bot._provider_url", return_value="https://example.test/messages"):
            self._assert_no_start(self._callback(), "transport_preflight_url_invalid")

    def test_valid_preflight_outside_atomic_then_one_send_and_immediate_mid(self):
        from unittest.mock import patch
        from management.services.ig_revision_transport import RevisionProviderTransport

        effect = self._text_plan().effects[0]
        original_preflight = RevisionProviderTransport.preflight
        events = []

        def preflight(transport, payload):
            self.assertFalse(connection.in_atomic_block)
            effect.refresh_from_db()
            self.assertEqual(effect.state, effect.State.CLAIMED)
            self.assertIsNone(effect.provider_started_at)
            events.append("preflight")
            return original_preflight(transport, payload)

        def send(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            effect.refresh_from_db()
            self.assertEqual(effect.state, effect.State.PROVIDER_STARTED)
            self.assertIsNotNone(effect.provider_started_at)
            self.assertEqual(json.loads(kwargs["data"]), effect.payload)
            events.append("http")
            return 200, '{"message_id":"actual-provider-mid"}'

        def register(mid, recipient, **kwargs):
            self.assertEqual(mid, "actual-provider-mid")
            self.assertEqual(recipient, self.client_row.igsid)
            effect.refresh_from_db()
            self.assertEqual(effect.state, effect.State.PROVIDER_STARTED)
            events.append("register")

        with (
            patch.object(RevisionProviderTransport, "preflight", autospec=True, side_effect=preflight),
            patch("management.services.instagram_bot._provider_http", side_effect=send) as http,
            patch("management.services.instagram_bot._register_outgoing_message", side_effect=register),
        ):
            result = self._drain(self._callback())
        self.assertEqual(events, ["preflight", "http", "register"])
        self.assertEqual(result.state, "sent")
        self.assertEqual(result.attempted, 1)
        http.assert_called_once()
        effect.refresh_from_db()
        self.assertEqual(effect.provider_message_id, "actual-provider-mid")

    def test_passed_preflight_actual_timeout_remains_unknown_without_retry(self):
        from unittest.mock import patch

        effect = self._text_plan().effects[0]
        with patch("management.services.instagram_bot._provider_http", side_effect=TimeoutError) as http:
            first = self._drain(self._callback())
            second = self._drain(self._callback())
        self.assertEqual(first.state, "unknown")
        self.assertEqual(first.attempted, 1)
        self.assertEqual(second.state, "unknown")
        http.assert_called_once()
        effect.refresh_from_db()
        self.assertIsNotNone(effect.provider_started_at)

    def test_namespace_race_after_start_is_proven_no_dispatch(self):
        from unittest.mock import patch
        from management.services.ig_revision_outbox import mark_provider_started

        effect = self._text_plan().effects[0]
        callback = self._callback()

        def start_then_change(*args, **kwargs):
            result = mark_provider_started(*args, **kwargs)
            self.assertEqual(result.reason, "provider_started")
            self.settings.ig_user_id = "other-owner"
            return result

        with (
            patch("management.services.ig_revision_delivery.mark_provider_started", side_effect=start_then_change),
            patch("management.services.instagram_bot._provider_http") as http,
            patch("management.services.instagram_bot._register_outgoing_message") as register,
        ):
            result = self._drain(callback)
        http.assert_not_called()
        register.assert_not_called()
        effect.refresh_from_db()
        self.assertEqual(result.state, "definite_failed")
        self.assertEqual(result.attempted, 0)
        self.assertFalse(result.fallback_ready)
        self.assertIsNotNone(effect.provider_started_at)
        self.assertEqual(effect.failure_code, "transport_preflight_namespace_mismatch")
        self.assertEqual(effect.provider_message_id, "")
        self.assertIsNone(effect.provider_http_status)

    def test_corrupt_payload_or_projection_digest_cannot_pass_preflight(self):
        from unittest.mock import patch

        effect = self._text_plan().effects[0]
        # Simulate damaged persisted input; normal model writes forbid this.
        table = connection.ops.quote_name(IgRevisionDeliveryEffect._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET projection_digest = %s WHERE id = %s", ["f" * 64, effect.pk])
        with (
            patch("management.services.instagram_bot._provider_http") as http,
            patch("management.services.ig_revision_delivery.mark_provider_started") as start,
        ):
            result = self._drain(self._callback())
        self.assertEqual(result.reason, "effect_binding_invalid")
        http.assert_not_called()
        start.assert_not_called()
        effect.refresh_from_db()
        self.assertIsNone(effect.provider_started_at)

    def test_untrusted_callback_cannot_claim_factory_zero_dispatch_proof(self):
        effect = self._text_plan().effects[0]
        result = self._drain(lambda payload: ProviderPartResult(
            "instagram_login:owner-1", outcome="known_not_dispatched",
            explicit_rejection_code="transport_preflight_payload_invalid",
        ))
        self.assertEqual(result.state, "unknown")
        effect.refresh_from_db()
        self.assertEqual(effect.failure_code, "provider_transport_unknown")

    def test_second_preflight_exception_is_no_dispatch_not_unknown(self):
        from unittest.mock import patch

        effect = self._text_plan().effects[0]
        callback = self._callback()
        with (
            patch("management.services.instagram_bot._provider_url", side_effect=[
                "https://graph.instagram.com/v25.0/owner-1/messages", RuntimeError("local configuration changed"),
            ]),
            patch("management.services.instagram_bot._provider_http") as http,
        ):
            result = self._drain(callback)
        http.assert_not_called()
        effect.refresh_from_db()
        self.assertEqual(result.state, "definite_failed")
        self.assertEqual(result.attempted, 0)
        self.assertEqual(effect.failure_code, "transport_preflight_check_failed")
        self.assertIsNotNone(effect.provider_started_at)
        self.assertIsNone(effect.provider_http_status)
        self.assertEqual(effect.provider_message_id, "")

    def test_first_preflight_exception_never_records_start(self):
        from unittest.mock import patch

        with patch("management.services.instagram_bot._provider_url", side_effect=RuntimeError("local config")):
            self._assert_no_start(self._callback(), "transport_preflight_check_failed")

    def test_valid_callback_for_different_owner_cannot_send_this_effect(self):
        self.settings.ig_user_id = "different-owner"
        self._assert_no_start(
            self._callback(expected_namespace="instagram_login:different-owner"),
            "transport_preflight_namespace_mismatch",
        )

    def test_corrupt_payload_digest_is_rejected_before_start(self):
        from unittest.mock import patch

        effect = self._text_plan().effects[0]
        table = connection.ops.quote_name(IgRevisionDeliveryEffect._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET payload_digest = %s WHERE id = %s", ["f" * 64, effect.pk])
        with (
            patch("management.services.instagram_bot._provider_http") as http,
            patch("management.services.ig_revision_delivery.mark_provider_started") as start,
        ):
            result = self._drain(self._callback())
        self.assertEqual(result.reason, "effect_binding_invalid")
        http.assert_not_called()
        start.assert_not_called()
        effect.refresh_from_db()
        self.assertIsNone(effect.provider_started_at)
