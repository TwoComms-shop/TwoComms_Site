from types import SimpleNamespace
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from management.bot_views import _message_media_rows


@override_settings(ROOT_URLCONF="twocomms.urls_management")
class MediaCoveragePayloadTests(SimpleTestCase):
    def test_template_has_audio_player_and_non_image_fallback_contract(self):
        template = (Path(__file__).parent / "templates" / "management" / "bot.html").read_text()

        self.assertIn("media_kind", template)
        self.assertIn("audio.controls=true", template)
        self.assertIn("audio.preload='metadata'", template)
        self.assertIn("video.controls=true", template)
        self.assertIn("video.preload='metadata'", template)
        self.assertIn("label+' недоступне'", template)

    def test_owned_audio_exposes_authorized_preview_and_voice_label(self):
        message = SimpleNamespace(
            pk=48,
            attachment_media=[{
                "source_part_id": "mp1_" + "b" * 32,
                "original_index": 0,
                "status": "owned",
                "private_storage": True,
                "storage_name": "ig_message_media/private.ogg",
                "mime": "audio/ogg",
                "media_type": "audio",
                "content_hash": "c" * 64,
                "inspection": {"state": "uninspected", "outcome": "not_submitted"},
            }],
            turn_intelligence_artifact={},
        )

        rows = _message_media_rows(message, [])

        self.assertEqual(rows[0]["media_kind"], "audio")
        self.assertEqual(rows[0]["media_label"], "Голосове повідомлення")
        self.assertEqual(
            rows[0]["public_url"],
            "/bot/private-media/48/mp1_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/preview/",
        )

    def test_story_and_repost_keep_non_generic_labels(self):
        for media_type, expected in (("story_mention", "Сторіс"), ("ig_post", "Репост")):
            with self.subTest(media_type=media_type):
                message = SimpleNamespace(
                    pk=49,
                    attachment_media=[{
                        "source_part_id": "mp1_" + "d" * 32,
                        "original_index": 0,
                        "status": "unavailable",
                        "media_type": media_type,
                        "mime": "image/jpeg",
                        "inspection": {},
                    }],
                    turn_intelligence_artifact={},
                )
                self.assertEqual(_message_media_rows(message, [])[0]["media_label"], expected)

    def test_video_has_truthful_label(self):
        message = SimpleNamespace(
            pk=50,
            attachment_media=[{
                "source_part_id": "mp1_" + "e" * 32,
                "original_index": 0,
                "status": "unavailable",
                "media_type": "video",
                "mime": "video/mp4",
                "inspection": {},
            }],
            turn_intelligence_artifact={},
        )
        row = _message_media_rows(message, [])[0]
        self.assertEqual(row["media_kind"], "video")
        self.assertEqual(row["media_label"], "Відео")

    def test_owned_story_image_exposes_authorized_preview(self):
        message = SimpleNamespace(
            pk=51,
            attachment_media=[{
                "source_part_id": "mp1_" + "f" * 32,
                "status": "owned", "private_storage": True,
                "storage_name": "ig_message_media/story.jpg", "mime": "image/jpeg",
                "media_type": "story", "content_hash": "f" * 64, "inspection": {},
            }],
            turn_intelligence_artifact={},
        )
        row = _message_media_rows(message, [])[0]
        self.assertEqual(row["media_kind"], "story")
        self.assertTrue(row["public_url"].endswith("/preview/"))

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
