"""Native owned uploads plus bounded raster decoding; no external fetches."""
from datetime import timedelta
from io import BytesIO
import struct
import uuid
import zlib
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from management.ig_assisted_commerce_models import IgAssistedCommerceImage as ImageAsset
from management.bot_access import OPERATE_IG_BOT_PERMISSION
from management.services import ig_assisted_commerce as commerce
from management.services import ig_assisted_commerce_media as media
from management.services.ig_private_media import private_media_storage
from management.tests_ig_assisted_commerce_api import _AssistedApiFixture


def raster(*, alpha=False, exif=False):
    from PIL import Image
    image = Image.new("RGBA" if alpha else "RGB", (3, 2), (120, 40, 90, 100) if alpha else (120, 40, 90))
    stream = BytesIO()
    options = {}
    if exif:
        metadata = Image.Exif();metadata[0x010E] = "private manager metadata"
        options["exif"] = metadata
    image.save(stream, format="PNG" if alpha else "JPEG", **options)
    return stream.getvalue()


class AssistedRasterCodecTests(SimpleTestCase):
    def test_raster_reencoding_removes_exif_and_preserves_alpha(self):
        from PIL import Image
        for alpha in (False, True):
            normalized = media.normalize_raster(SimpleUploadedFile("untrusted-name.svg", raster(alpha=alpha, exif=not alpha), content_type="image/svg+xml"))
            with Image.open(BytesIO(normalized["body"])) as decoded:
                self.assertFalse(decoded.getexif())
                self.assertNotIn("exif", decoded.info)
                self.assertEqual(decoded.size, (3, 2))
                self.assertEqual("A" in decoded.getbands(), alpha)
            self.assertEqual(normalized["mime_type"], "image/png" if alpha else "image/jpeg")

    def test_svg_urls_truncation_oversize_and_declared_pixel_bombs_are_rejected(self):
        def chunk(name, data):
            return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xffffffff)
        bomb = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 6000, 5000, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
        for body in (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', b"https://example.test/photo.jpg", raster()[:20], b"x" * (media.MAX_BYTES + 1), bomb):
            with self.subTest(bytes=len(body)), self.assertRaises(commerce.AssistedDecisionError):
                media.normalize_raster(SimpleUploadedFile("image.jpg", body, content_type="image/jpeg"))

    def test_multiframe_png_cannot_become_an_owned_product_photo(self):
        from PIL import Image
        first = Image.new("RGBA", (2, 2), "red");second = Image.new("RGBA", (2, 2), "blue")
        stream = BytesIO();first.save(stream, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
        with self.assertRaisesMessage(commerce.AssistedDecisionError, "image_animation_unsupported"):
            media.normalize_raster(SimpleUploadedFile("animation.png", stream.getvalue()))


@override_settings(ROOT_URLCONF="twocomms.urls_management", SECURE_SSL_REDIRECT=False,
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False, SITE_BASE_URL="https://twocomms.test")
class AssistedOwnedMediaTests(_AssistedApiFixture, TestCase):
    def upload_payload(self, request, context, operation=None, body=None):
        return {"action": "upload", "request_id": str(request.pk), "operation_id": str(operation or uuid.uuid4()),
            **context["cas"], "image": SimpleUploadedFile("../../not-a-public-path.jpg", body or raster(exif=True), content_type="image/jpeg")}

    def upload(self, payload):
        return self.client.post(self.endpoint(), payload)

    def preview(self, image, client_id=None):
        return reverse("management_assisted_commerce_image", args=[client_id or self.client_row.pk, image.pk])

    def test_actual_upload_replay_private_preview_and_metadata_are_owned(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, _original, context = self.produced_request()
            operation = uuid.uuid4()
            response = self.upload(self.upload_payload(request, context, operation))
            self.assertEqual(response.status_code, 200, response.content)
            asset = ImageAsset.objects.get(pk=response.json()["image"]["id"])
            self.assertTrue(commerce._verified(asset, "image"))
            self.assertEqual(asset.request_id, request.pk)
            self.assertEqual(asset.client_id, self.client_row.pk)
            self.assertEqual(asset.uploader_id, self.manager.pk)
            self.assertEqual(asset.storage_key, media.storage_key(request.pk, asset.pk, asset.mime_type))
            self.assertEqual(response.json()["image"]["preview_url"], self.preview(asset))
            replay = self.upload(self.upload_payload(request, context, operation))
            self.assertEqual(replay.status_code, 200, replay.content)
            self.assertFalse(replay.json()["created"])
            self.assertEqual(ImageAsset.objects.count(), 1)
            served = self.client.get(self.preview(asset))
            self.assertEqual(served.status_code, 200, served.content)
            self.assertEqual(served["X-Content-Type-Options"], "nosniff")
            self.assertIn("no-store", served["Cache-Control"])
            self.assertEqual(served["Content-Type"], asset.mime_type)
            self.assertEqual(len(served.content), asset.byte_size)
            self.assertEqual(self.client.get(self.preview(asset, self.client_row.pk + 9000)).status_code, 404)
            provider.assert_not_called()

    def test_malformed_files_changed_replay_and_stale_draft_do_not_add_assets(self):
        request, _original, context = self.produced_request()
        bad = self.upload(self.upload_payload(request, context, body=b"<svg/>"))
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(ImageAsset.objects.count(), 0)
        operation = uuid.uuid4()
        good = self.upload(self.upload_payload(request, context, operation))
        self.assertEqual(good.status_code, 200, good.content)
        conflict = self.upload(self.upload_payload(request, context, operation, body=raster(alpha=True)))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["reason"], "operation_conflict")
        stale = self.upload_payload(request, context);stale["expected_decision_version"] = 90
        self.assertEqual(self.upload(stale).status_code, 409)
        self.assertEqual(ImageAsset.objects.count(), 1)

    def test_bytes_tampering_expiration_and_privacy_fence_hide_private_photo(self):
        request, _original, context = self.produced_request()
        response = self.upload(self.upload_payload(request, context))
        self.assertEqual(response.status_code, 200, response.content)
        asset = ImageAsset.objects.get(pk=response.json()["image"]["id"])
        with patch("django.utils.timezone.now", return_value=asset.delete_after + timedelta(seconds=1)):
            self.assertEqual(self.client.get(self.preview(asset)).status_code, 404)
        storage = private_media_storage();storage.delete(asset.storage_key)
        from django.core.files.base import ContentFile
        self.assertEqual(storage.save(asset.storage_key, ContentFile(b"tampered")), asset.storage_key)
        self.assertEqual(self.client.get(self.preview(asset)).status_code, 404)
        self.client_row.privacy_erasure_started_at = self.now
        self.client_row.save(update_fields=["privacy_erasure_started_at"])
        self.assertEqual(self.client.get(self.preview(asset)).status_code, 404)
        self.assertEqual(self.upload(self.upload_payload(request, context)).status_code, 410)

    def test_permission_revocation_and_url_input_cannot_reuse_actual_owned_asset(self):
        with patch("management.services.instagram_bot._provider_http") as provider:
            request, _original, context = self.produced_request()
            response = self.upload(self.upload_payload(request, context))
            self.assertEqual(response.status_code, 200, response.content)
            asset = ImageAsset.objects.get(pk=response.json()["image"]["id"])
            attempted_url = self.upload_payload(request, context)
            attempted_url["image_url"] = "https://instagram.example/private/reference.jpg"
            self.assertEqual(self.upload(attempted_url).status_code, 400)
            self.manager.user_permissions.remove(*self.manager.user_permissions.filter(
                codename=OPERATE_IG_BOT_PERMISSION.split(".", 1)[1]))
            self.assertEqual(self.client.get(self.preview(asset)).status_code, 403)
            self.assertEqual(self.upload(self.upload_payload(request, context)).status_code, 403)
            self.assertEqual(ImageAsset.objects.count(), 1)
            provider.assert_not_called()
