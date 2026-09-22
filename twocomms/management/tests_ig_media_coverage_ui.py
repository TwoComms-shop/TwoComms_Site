from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from management.bot_views import _message_media_rows


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class MediaCoveragePayloadTests(SimpleTestCase):
    def test_owned_part_exposes_coverage_and_authorized_preview_only(self):
        message = SimpleNamespace(
            pk=44,
            attachment_media=[{
                "source_part_id": "mp1_" + "a" * 32,
                "original_index": 0,
                "status": "owned",
                "private_storage": True,
                "storage_name": "ig_message_media/private.jpg",
                "mime": "image/jpeg",
                "content_hash": "b" * 64,
                "inspection": {
                    "state": "inspected",
                    "outcome": "understood",
                    "type_code": "certificate",
                    "provider_model": "gemini-3.7-flash",
                    "request_id": "request-1",
                },
            }],
            turn_intelligence_artifact={},
        )

        rows = _message_media_rows(message, [])

        self.assertEqual(rows[0]["capture_state"], "owned")
        self.assertEqual(rows[0]["inspection_outcome"], "understood")
        self.assertEqual(rows[0]["model"], "gemini-3.7-flash")
        self.assertEqual(rows[0]["effort"], "unknown")
        self.assertEqual(
            rows[0]["preview_url"],
            "/bot/private-media/44/mp1_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/preview/",
        )
        self.assertEqual(rows[0]["public_url"], rows[0]["preview_url"])
        self.assertEqual(rows[0]["media_label"], "Зображення сертифіката")
        self.assertNotIn("url", rows[0])
        self.assertNotIn("storage_name", rows[0])

    def test_failed_part_is_visible_without_preview_or_transport_url(self):
        message = SimpleNamespace(
            pk=45,
            attachment_media=[{
                "source_part_id": "mp1_" + "c" * 32,
                "original_index": 1,
                "status": "unavailable",
                "error_kind": "stream_too_large",
                "url_metadata_expired": True,
            }],
            turn_intelligence_artifact={},
        )

        rows = _message_media_rows(message, [])

        self.assertEqual(rows[0]["capture_state"], "failed")
        self.assertEqual(rows[0]["error_kind"], "stream_too_large")
        self.assertEqual(rows[0]["preview_url"], "")

    def test_legacy_customer_attachment_is_a_bounded_unavailable_part(self):
        message = SimpleNamespace(
            pk=46,
            role="user",
            attachments='["https://lookaside.fbsbx.com/signed?token=secret"]',
            attachment_media=[],
            turn_intelligence_artifact={},
        )

        rows = _message_media_rows(message, [])

        self.assertEqual(rows[0]["inspection_outcome"], "not_captured")
        self.assertEqual(rows[0]["error_kind"], "legacy_media_unavailable")
        self.assertNotIn("url", rows[0])

    def test_outgoing_own_origin_product_attachment_remains_public_thumbnail(self):
        message = SimpleNamespace(
            pk=47,
            role="model",
            source="catalog_media",
            attachments='["/media/catalog/product.jpg"]',
            attachment_media=[],
            turn_intelligence_artifact={},
        )

        rows = _message_media_rows(message, [])

        self.assertEqual(rows, [{
            "public_url": "https://twocomms.shop/media/catalog/product.jpg",
            "role": "product",
            "capture_state": "not_applicable",
            "inspection_state": "not_applicable",
            "inspection_outcome": "",
            "effort": "unknown",
        }])
