import json
import os
from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings

from management.models import InstagramBotSettings
from management.services.ig_catalog_media import (
    CatalogMediaItem,
    CatalogMediaSelection,
    CatalogMediaState,
    prepare_catalog_media,
)
from management.services.ig_message_templates import (
    GenericTemplate,
    QuickReply,
    TemplateCard,
    prepare_template_effects,
)
from management.services.ig_revision_transport import (
    build_provider_part_callback,
    prepare_text_effects,
)


@override_settings(SITE_BASE_URL="https://twocomms.test")
class RevisionTransportTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {"IG_PROVIDER_TRANSPORT": "instagram_login"},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = InstagramBotSettings.objects.create(
            pk=1,
            ig_user_id="owner-1",
        )
        self.recipient = "17840000000001"
        self.namespace = "instagram_login:owner-1"

    def _callback(self, **overrides):
        values = {
            "expected_namespace": self.namespace,
            "expected_recipient": self.recipient,
            "access_token": "memory-only-token",
        }
        values.update(overrides)
        return build_provider_part_callback(self.settings, **values)

    def test_text_is_split_once_and_quick_replies_only_attach_to_last_part(self):
        from management.services.ig_delivery_plan import build_delivery_plan

        text = "Перший блок відповіді. Другий блок відповіді?"
        replies = (QuickReply("Розмір M", "twc:1:size:M"),)
        with patch(
            "management.services.ig_revision_transport.build_delivery_plan",
            wraps=build_delivery_plan,
        ) as planner:
            prepared = prepare_text_effects(
                self.recipient,
                text,
                quick_replies=replies,
                limit=35,
                max_chunks=4,
            )

        self.assertFalse(prepared.error)
        self.assertGreater(len(prepared.effects), 1)
        planner.assert_called_once_with(text, limit=35, max_chunks=4)
        for effect in prepared.effects[:-1]:
            self.assertNotIn("quick_replies", effect["payload"]["message"])
        self.assertEqual(
            prepared.effects[-1]["payload"]["message"]["quick_replies"],
            [{
                "content_type": "text",
                "title": "Розмір M",
                "payload": "twc:1:size:M",
            }],
        )

    def test_one_http_request_preserves_text_catalog_and_template_payloads(self):
        text_payload = prepare_text_effects(
            self.recipient, "Exact text"
        ).effects[0]["payload"]
        catalog = prepare_catalog_media(
            self.settings,
            self.recipient,
            CatalogMediaSelection(
                CatalogMediaState.READY,
                items=(CatalogMediaItem(
                    "https://twocomms.test/product.jpg",
                    "Product",
                    "Product alt",
                    1,
                    "image/jpeg",
                    100,
                ),),
            ),
        )
        catalog_payload = catalog.payloads[0]
        template = prepare_template_effects(
            self.recipient,
            GenericTemplate(
                cards=(TemplateCard(title="Card", subtitle="Subtitle"),),
                fallback_text="Fallback",
            ),
            allow_text_fallback=False,
        )
        template_payload = template.effects[0]["payload"]
        payloads = (text_payload, catalog_payload, template_payload)
        responses = [
            (200, json.dumps({"message_id": f"mid-{index}"}))
            for index in range(len(payloads))
        ]

        callback = self._callback()
        with (
            patch(
                "management.services.instagram_bot._provider_url",
                return_value="https://graph.instagram.com/v25.0/owner-1/messages",
            ) as provider_url,
            patch(
                "management.services.instagram_bot._provider_http",
                side_effect=responses,
            ) as provider_http,
            patch(
                "management.services.instagram_bot._register_outgoing_message"
            ) as register,
        ):
            results = [callback(payload) for payload in payloads]

        self.assertEqual(provider_url.call_count, 3)
        self.assertEqual(provider_http.call_count, 3)
        self.assertEqual(
            [result.provider_message_id for result in results],
            ["mid-0", "mid-1", "mid-2"],
        )
        self.assertEqual(
            [call.kwargs["kind"] for call in register.call_args_list],
            ["text", "image", "template"],
        )
        for index, call in enumerate(provider_http.call_args_list):
            self.assertEqual(
                json.loads(call.kwargs["data"].decode("utf-8")),
                payloads[index],
            )
            self.assertEqual(call.kwargs["token"], "memory-only-token")

    def test_timeout_is_unknown_and_never_retried(self):
        payload = prepare_text_effects(
            self.recipient, "One request"
        ).effects[0]["payload"]
        callback = self._callback()
        with (
            patch(
                "management.services.instagram_bot._provider_url",
                return_value="https://graph.instagram.com/v25.0/owner-1/messages",
            ),
            patch(
                "management.services.instagram_bot._provider_http",
                side_effect=TimeoutError,
            ) as provider_http,
            patch(
                "management.services.instagram_bot._register_outgoing_message"
            ) as register,
        ):
            result = callback(payload)

        self.assertEqual(result.outcome, "timeout")
        self.assertIsNone(result.http_status)
        self.assertEqual(provider_http.call_count, 1)
        register.assert_not_called()

    def test_explicit_rejection_and_server_failure_keep_outbox_taxonomy(self):
        payload = prepare_text_effects(
            self.recipient, "One request"
        ).effects[0]["payload"]
        callback = self._callback()
        with (
            patch(
                "management.services.instagram_bot._provider_url",
                return_value="https://graph.instagram.com/v25.0/owner-1/messages",
            ),
            patch(
                "management.services.instagram_bot._provider_http",
                side_effect=[
                    (400, '{"error":{"code":100}}'),
                    (503, "unavailable"),
                ],
            ) as provider_http,
        ):
            rejected = callback(payload)
            ambiguous = callback(payload)

        self.assertEqual(rejected.outcome, "explicit_rejected")
        self.assertEqual(rejected.explicit_rejection_code, "provider_rejected")
        self.assertEqual(ambiguous.outcome, "response")
        self.assertEqual(ambiguous.http_status, 503)
        self.assertEqual(provider_http.call_count, 2)

    def test_namespace_mismatch_and_invalid_payload_never_touch_http(self):
        payload = prepare_text_effects(
            self.recipient, "One request"
        ).effects[0]["payload"]
        mismatch = self._callback(expected_namespace="instagram_login:other")
        callback = self._callback()
        invalid = {
            "recipient": {"id": "different-user"},
            "message": {"text": "One request"},
        }
        with patch(
            "management.services.instagram_bot._provider_http"
        ) as provider_http:
            mismatch_result = mismatch(payload)
            invalid_result = callback(invalid)

        self.assertEqual(mismatch_result.outcome, "known_not_dispatched")
        self.assertEqual(mismatch_result.provider_namespace, "instagram_login:other")
        self.assertEqual(invalid_result.outcome, "known_not_dispatched")
        provider_http.assert_not_called()

    def test_outer_transaction_blocks_http(self):
        payload = prepare_text_effects(
            self.recipient, "One request"
        ).effects[0]["payload"]
        callback = self._callback()
        with patch(
            "management.services.instagram_bot._provider_http"
        ) as provider_http:
            with transaction.atomic():
                result = callback(payload)

        self.assertEqual(result.outcome, "known_not_dispatched")
        self.assertIsNone(result.http_status)
        provider_http.assert_not_called()
