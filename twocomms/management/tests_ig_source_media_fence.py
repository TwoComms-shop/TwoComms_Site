"""No-DB regressions for source preferences surviving backend inspection."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

from django.test import SimpleTestCase

from management.services.ig_commerce_projection import _source_fence_row
from management.services.ig_media_manifest import normalize_attachment_media
from management.services.ig_turn_capture import validate_current_source_cart_sources
from management.services.ig_turn_intelligence import TurnContextError


class SourceMediaFenceTests(SimpleTestCase):
    def source(self):
        stamp = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
        return SimpleNamespace(pk=31, client_id=4, sender_id="customer", role="user",
            source="webhook", status="received", text="Які є кольори?",
            provider_namespace="instagram", provider_created_at=stamp, created_at=stamp,
            mid="mid-31", reply_to_provider_message_id="", quick_reply_payload="",
            attachments=["https://example.invalid/photo.jpg"], attachment_media=[{
                "type": "image", "role": "attachment", "provider_object_id": "asset-31",
                "url": "https://example.invalid/photo.jpg", "status": "owned",
                "capture_state": "owned", "storage_name": "private/31.jpg",
                "private_storage": True, "mime": "image/jpeg", "bytes": 512,
                "content_hash": "a" * 64, "source_message_scope": "message:31",
                "capture_attempts": 1, "retention_policy": "private_media",
                "inspection": {"version": "ig-media-inspection-v1", "state": "uninspected",
                    "outcome": "not_submitted"},
            }])

    def capture(self, source, *, legacy=False):
        rows = {source.pk: _source_fence_row(source, legacy_raw_media=legacy)}
        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        return {"fence": {"source_ids": [source.pk], "source_digest": digest}}

    def assert_changed(self, capture, source):
        with self.assertRaisesMessage(TurnContextError, "source_cart_sources_changed"):
            validate_current_source_cart_sources(capture, source_rows=[source])

    def project_inspection(self, source):
        normalized = normalize_attachment_media(
            source.attachment_media, message_scope=source.pk)
        part = normalized[0]
        source.attachment_media[0]["inspection"] = {"version": "ig-media-inspection-v1", "state": "inspected",
            "source_part_id": part["source_part_id"], "source_image_index": 0,
            "content_hash": part["content_hash"], "revision_id": 42, "request_id": "request-42",
            "provider_model": "fake-local", "outcome": "understood", "type_code": "selfie",
            "analysis_state": "understood", "origin": "provider_observation",
            "content_kind": "product_wearing", "sentiment": "positive", "confidence": "high",
            "evidence": ["wearing_visible"], "evidence_code": "visual_content",
            "complaint_code": "none", "transcript": "", "audio_status": "not_applicable"}

    def test_inspection_projection_and_canonical_identity_enrichment_preserve_fence(self):
        source = self.source()
        capture = self.capture(source)
        self.project_inspection(source)
        self.assertTrue(validate_current_source_cart_sources(capture, source_rows=[source]))
        source.attachment_media = normalize_attachment_media(
            source.attachment_media, message_scope=source.pk)
        self.assertTrue(validate_current_source_cart_sources(capture, source_rows=[source]))
        source.attachment_media[0]["inspection"].update(
            state="uninspected", outcome="observation_missing", sentiment="unknown")
        self.assertTrue(validate_current_source_cart_sources(capture, source_rows=[source]))

    def test_media_input_capture_storage_and_privacy_changes_are_fenced(self):
        changes = {"url": "https://example.invalid/replaced.jpg", "provider_object_id": "other",
            "type": "video", "role": "story", "status": "deleted", "capture_state": "deleted",
            "storage_name": "private/other.jpg", "private_storage": False, "mime": "image/png",
            "bytes": 513, "content_hash": "b" * 64, "source_message_scope": "message:99",
            "capture_attempts": 2, "retention_policy": "delete_now",
            "delete_after": "2026-10-07T10:00:00Z", "url_metadata_expired": True,
            "prepared_blob": {"state": "deleted"}, "local_url": "/different.jpg"}
        for key, value in changes.items():
            with self.subTest(key=key):
                source = self.source()
                capture = self.capture(source)
                self.project_inspection(source)
                source.attachment_media[0][key] = value
                self.assert_changed(capture, source)

    def test_part_identity_index_order_addition_and_removal_are_fenced(self):
        source = self.source()
        second = deepcopy(source.attachment_media[0])
        second["provider_object_id"] = "asset-32"
        source.attachment_media.append(second)
        self.project_inspection(source)
        capture = self.capture(source)
        for mutation in (lambda rows: rows.reverse(), lambda rows: rows.pop(),
                lambda rows: rows.append({"type": "image", "url": "https://example.invalid/3.jpg"}),
                lambda rows: rows[0].update(source_part_id="mp1_" + "f" * 32),
                lambda rows: rows[0].update(original_index=5)):
            with self.subTest(mutation=mutation):
                changed = deepcopy(source)
                mutation(changed.attachment_media)
                self.assert_changed(capture, changed)

    def test_explicit_identity_origin_and_malformed_identity_mutations_are_fenced(self):
        source = self.source()
        source.attachment_media = normalize_attachment_media(
            source.attachment_media, message_scope=source.pk, identity_origin="ingress")
        capture = self.capture(source)
        self.project_inspection(source)
        self.assertTrue(validate_current_source_cart_sources(capture, source_rows=[source]))
        for key, value in (("identity_origin", "legacy_positional"),
                ("identity_origin", "invalid-origin"), ("source_part_id", "invalid-part"),
                ("source_part_id", None), ("original_index", -1),
                ("original_index", "invalid-index"), ("original_index", None)):
            with self.subTest(key=key, value=value):
                changed = deepcopy(source)
                changed.attachment_media[0][key] = value
                self.assert_changed(capture, changed)

    def test_existing_malformed_identity_is_retained_without_repair(self):
        source = self.source()
        source.attachment_media[0].update(source_part_id="invalid-original",
            original_index="invalid-original", identity_origin="invalid-original")
        capture = self.capture(source)
        self.project_inspection(source)
        self.assertTrue(validate_current_source_cart_sources(capture, source_rows=[source]))
        fenced = _source_fence_row(source)["attachment_media"][0]
        for key in ("source_part_id", "original_index", "identity_origin"):
            self.assertEqual(fenced[key], "invalid-original")
            changed = deepcopy(source)
            changed.attachment_media[0][key] = "different-invalid"
            self.assert_changed(capture, changed)

    def test_source_owner_caption_status_and_attachment_changes_are_fenced(self):
        changes = {"client_id": 5, "sender_id": "other", "role": "assistant",
            "source": "import", "status": "deleted", "text": "other question",
            "provider_namespace": "other", "mid": "mid-99", "reply_to_provider_message_id": "reply-2",
            "quick_reply_payload": "choose-2", "attachments": [],
            "created_at": datetime(2026, 10, 8, tzinfo=timezone.utc), "provider_created_at": None}
        for key, value in changes.items():
            with self.subTest(key=key):
                source = self.source()
                capture = self.capture(source)
                setattr(source, key, value)
                self.assert_changed(capture, source)

    def test_unrecognized_inspection_and_other_derivative_names_remain_fenced(self):
        changes = {"inspection": {"version": "another-owner", "state": "inspected"},
            "receipt_inspection": {"paid": True}, "inspection_state": "inspected",
            "analysis": {"approved": True}}
        for key, value in changes.items():
            with self.subTest(key=key):
                source = self.source()
                capture = self.capture(source)
                source.attachment_media[0][key] = value
                self.assert_changed(capture, source)
        source = self.source()
        capture = self.capture(source)
        source.attachment_media[0]["inspection"]["unknown_source_field"] = "changed"
        self.assert_changed(capture, source)

    def test_legacy_raw_fence_accepts_only_unchanged_original_source(self):
        source = self.source()
        capture = self.capture(source, legacy=True)
        self.assertTrue(validate_current_source_cart_sources(capture, source_rows=[source]))
        self.project_inspection(source)
        self.assert_changed(capture, source)

    def test_malformed_media_is_not_silently_omitted(self):
        for media in (["invalid"], {"unexpected": "mapping"}, [
                {"source_part_id": "mp1_" + "a" * 32, "original_index": 0},
                {"source_part_id": "mp1_" + "a" * 32, "original_index": 0}]):
            with self.subTest(media=media):
                source = self.source()
                source.attachment_media = media
                capture = self.capture(source)
                self.assertTrue(validate_current_source_cart_sources(capture, source_rows=[source]))
                source.attachment_media = []
                self.assert_changed(capture, source)

    def test_source_projection_does_not_mutate_live_input(self):
        source = self.source()
        original = deepcopy(source.attachment_media)
        row = _source_fence_row(source)
        self.assertEqual(source.attachment_media, original)
        self.assertNotIn("inspection", row["attachment_media"][0])
        row["attachment_media"][0]["storage_name"] = "modified"
        self.assertEqual(source.attachment_media, original)
