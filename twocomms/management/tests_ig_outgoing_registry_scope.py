"""Scoped exact-MID proof contracts for the legacy outgoing registry."""
from unittest.mock import patch

from django.test import TestCase

from management.models import IgClient, InstagramBotMessage
from management.services.ig_outgoing_registry import is_our_outgoing, register_outgoing


class OutgoingRegistryScopeTests(TestCase):
    def test_scoped_cache_match_requires_exact_recipient_and_namespace(self):
        register_outgoing("scope-mid", recipient_id="client-a", provider_namespace="instagram_login:owner-a")
        self.assertTrue(is_our_outgoing("scope-mid", recipient_id="client-a", provider_namespace="instagram_login:owner-a"))
        self.assertFalse(is_our_outgoing("scope-mid", recipient_id="client-b", provider_namespace="instagram_login:owner-a"))
        self.assertFalse(is_our_outgoing("scope-mid", recipient_id="client-a", provider_namespace="instagram_login:owner-b"))

    def test_legacy_cache_value_cannot_prove_scoped_echo(self):
        from management.services import ig_outgoing_registry as registry

        registry.cache.set(registry._cache_key("legacy-mid"), "media", registry.OUTGOING_TTL_SECONDS)
        self.assertTrue(is_our_outgoing("legacy-mid"))
        self.assertFalse(is_our_outgoing("legacy-mid", recipient_id="client-a", provider_namespace="instagram_login:owner-a"))

    def test_exact_db_transcript_proof_is_scoped(self):
        client = IgClient.objects.create(igsid="client-a")
        InstagramBotMessage.objects.create(
            client=client, sender_id=client.igsid, role=InstagramBotMessage.Role.MODEL,
            provider_message_id="db-mid", provider_namespace="instagram_login:owner-a",
        )
        self.assertTrue(is_our_outgoing("db-mid", recipient_id="client-a", provider_namespace="instagram_login:owner-a"))
        self.assertFalse(is_our_outgoing("db-mid", recipient_id="client-a", provider_namespace="instagram_login:owner-b"))

    @patch("management.services.ig_outgoing_registry._current_namespace", return_value="")
    def test_metadata_failure_leaves_cache_unscoped(self, _namespace):
        register_outgoing("unscoped-mid", recipient_id="client-a")
        self.assertTrue(is_our_outgoing("unscoped-mid"))
        self.assertFalse(is_our_outgoing("unscoped-mid", recipient_id="client-a", provider_namespace="instagram_login:owner-a"))
