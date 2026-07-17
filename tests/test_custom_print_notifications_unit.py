import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twocomms.settings")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import django

django.setup()

from storefront.custom_print_notifications import _build_message, notify_new_custom_print_lead


class AttachmentList(list):
    def all(self):
        return list(self)

    def count(self):
        return len(self)


class FakeLead:
    def __init__(self, attachments=None, placement_specs=None):
        self.pk = 42
        self.lead_number = "CP16042026L001"
        self.client_kind = "brand"
        self.service_kind = "adjust"
        self.product_type = "hoodie"
        self.placements = ["back", "sleeve"]
        self.quantity = 6
        self.size_mode = "mixed"
        self.sizes_note = "M x3, L x3"
        self.name = "Микита"
        self.contact_channel = "telegram"
        self.contact_value = "@void_unit"
        self.fit = "oversize"
        self.fabric = "premium"
        self.color_choice = "graphite"
        self.file_triage_status = "needs-work"
        self.brand_name = "Void Unit"
        self.business_kind = "branding"
        self.brief = "Зробити чисту адаптацію під друк."
        self.garment_note = ""
        self.placement_note = ""
        self.pricing_snapshot_json = {
            "base_price": 1800,
            "design_price": 150,
            "discount_percent": 10,
            "final_total": None,
            "estimate_required": True,
        }
        self.placement_specs_json = placement_specs or [
            {
                "zone": "back",
                "placement_key": "back",
                "label": "На спині",
                "size_preset": "A2",
                "requires_artwork_file": True,
            },
            {
                "zone": "sleeve",
                "placement_key": "sleeve_left",
                "label": "Лівий рукав",
                "mode": "full_text",
                "text": "VOID UNIT",
                "requires_artwork_file": False,
            },
            {
                "zone": "sleeve",
                "placement_key": "sleeve_right",
                "label": "Правий рукав",
                "mode": "a6",
                "requires_artwork_file": True,
            },
        ]
        self.attachments = AttachmentList(attachments or [])

    def get_client_kind_display(self):
        return "Для команди / бренду"

    def get_service_kind_display(self):
        return "Потрібно допрацювати"

    def get_product_type_display(self):
        return "Худі"

    def get_contact_channel_display(self):
        return "Telegram"

    def get_size_mode_display(self):
        return "Мікс розмірів"

    def get_business_kind_display(self):
        return "Брендинг"


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def is_configured(self):
        return True

    def send_admin_message(self, message, parse_mode="HTML", reply_markup=None):
        self.calls.append(("message", message, parse_mode, reply_markup))
        return True

    def send_admin_media_group(self, file_paths, captions=None, parse_mode="HTML"):
        self.calls.append(("media_group", list(file_paths), list(captions or []), parse_mode))
        return True

    def send_admin_photo(self, file_path, caption="", parse_mode="HTML", reply_markup=None):
        self.calls.append(("photo", file_path, caption, parse_mode, reply_markup))
        return True

    def send_admin_document(self, file_path, caption="", filename=None, parse_mode="HTML", reply_markup=None):
        self.calls.append(("document", file_path, caption, filename, parse_mode, reply_markup))
        return True


class CustomPrintNotificationUnitTests(unittest.TestCase):
    def test_build_message_includes_text_only_sleeve_in_structured_placements(self):
        lead = FakeLead()

        message = _build_message(lead)

        self.assertIn("Лівий рукав", message)
        self.assertIn("текст", message.lower())
        self.assertIn("На спині", message)
        self.assertIn("A2", message)
        self.assertIn("https://twocomms.shop/admin-panel/?section=custom_print_orders&lead=42", message)

    def test_build_message_resolves_color_label_from_product_matrix(self):
        """Колір повинен виводитися як 'Графіт', а не сирим 'graphite'.

        Регрес-тест для CP-UX-2026-05-18: до фіксу адмін бачив 'graphite'.
        """
        lead = FakeLead()

        message = _build_message(lead)

        # Лейбл повинен бути в повідомленні, а сирий слаг — ні.
        self.assertIn("Графіт", message)
        self.assertNotIn("graphite", message)
        # Hex кольору додається в окремому highlighted-рядку.
        self.assertIn("#3b3b3f", message)

    def test_build_message_marks_premium_fabric_explicitly(self):
        """Премиум должна быть выделена отдельной строкой с эмодзи 💎."""
        lead = FakeLead()

        message = _build_message(lead)

        self.assertIn("Тип тканини", message)
        self.assertIn("Преміум", message)
        self.assertIn("💎", message)

    def test_notify_new_custom_print_lead_sends_summary_then_captioned_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "back.png"
            doc_path = Path(temp_dir) / "right-sleeve.pdf"
            image_path.write_bytes(b"fake-image")
            doc_path.write_bytes(b"fake-pdf")

            lead = FakeLead(
                attachments=[
                    SimpleNamespace(
                        placement_zone="back",
                        file=SimpleNamespace(path=str(image_path), name=str(image_path.name)),
                    ),
                    SimpleNamespace(
                        placement_zone="sleeve_right",
                        file=SimpleNamespace(path=str(doc_path), name=str(doc_path.name)),
                    ),
                ]
            )
            notifier = FakeNotifier()

            with patch("storefront.custom_print_notifications._build_notifier", return_value=notifier):
                result = notify_new_custom_print_lead(lead)

        self.assertTrue(result)
        self.assertEqual(notifier.calls[0][0], "message")
        # Print assets are intentionally sent as documents to preserve the
        # original file bytes instead of Telegram photo recompression.
        self.assertEqual(notifier.calls[1][0], "document")
        self.assertIn("1/2", notifier.calls[1][2])
        self.assertIn("НА СПИНІ", notifier.calls[1][2])
        self.assertIn("A2", notifier.calls[1][2])
        self.assertEqual(notifier.calls[2][0], "document")
        self.assertIn("2/2", notifier.calls[2][2])
        self.assertIn("ПРАВИЙ РУКАВ", notifier.calls[2][2])
        self.assertIn("A6", notifier.calls[2][2])


if __name__ == "__main__":
    unittest.main()
