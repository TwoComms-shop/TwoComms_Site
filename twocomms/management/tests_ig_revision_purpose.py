"""Source purpose and durable ingress at the real revision send boundary."""
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings

from management.services.ig_revision_outbox import mark_provider_started
from management import tests_ig_revision_live as fixtures


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class RevisionPurposeTests(TransactionTestCase):
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare
    _generate = fixtures.RevisionLiveTests._generate
    _execute = fixtures.RevisionLiveTests._execute
    _replace_bundle = fixtures.RevisionLiveTests._replace_bundle

    def source_text(self, text):
        self._replace_bundle([text])

    def test_community_message_cannot_get_unrequested_sales_cta(self):
        self.source_text("Наш матеріал про засновника бренду.")
        self.parsed["reply_text"] = "Дякуємо за матеріал! Хочете замовити футболку?"
        result, _generate, http = self._execute()
        self.assertEqual(result.state, "blocked")
        self.assertIn("current_purpose_disallows_sales", result.reasons)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.exists())

    def test_explicit_customer_request_allows_optional_next_step(self):
        self.source_text("Хочу замовити футболку.")
        self.parsed["reply_text"] = "Яка модель вас цікавить? Можу допомогти підібрати розмір."
        result, _generate, http = self._execute()
        self.assertEqual(result.state, "completed", result.reasons)
        self.assertEqual(http.call_count, 1)

    def test_acknowledged_inbox_correction_after_start_marker_prevents_http(self):
        self.source_text("Хочу замовити футболку.")

        def interleave(*args, **kwargs):
            result = mark_provider_started(*args, **kwargs)
            self._message("Не замовляйте, у мене інше питання.", "late-correction")
            return result

        with patch("management.services.ig_revision_delivery.mark_provider_started", side_effect=interleave):
            result, _generate, http = self._execute()
        http.assert_not_called()
        self.assertNotEqual(result.state, "completed")
        effect = self.revision.delivery_effects.get()
        self.assertEqual(effect.state, "definite_failed")
        self.assertEqual(effect.failure_code, "pending_inbound")
        self.assertEqual(result.attempted_parts, 0)
