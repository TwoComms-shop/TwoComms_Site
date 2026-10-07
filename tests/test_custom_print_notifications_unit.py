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

from django.test import override_settings
from django.utils import translation
from django.utils.functional import Promise

from storefront.custom_print_config import ZONE_LABELS
from storefront.custom_print_notifications import (
    _build_message,
    _build_safe_exit_message,
    _info_reply_markup_full,
    _moderation_reply_markup,
    notify_new_custom_print_lead,
)


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
        self.telegram_verified_user_id = None
        self.telegram_verified_username = ""
        self.telegram_verified_phone = ""
        self.telegram_verified_at = None
        self.moderation_token = "test-token"

    def ensure_moderation_token(self):
        return self.moderation_token

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
    def __init__(self, message_result=True):
        self.calls = []
        self.message_result = message_result

    def is_configured(self):
        return True

    def send_admin_message(self, message, parse_mode="HTML", reply_markup=None, **kwargs):
        self.calls.append(("message", message, parse_mode, reply_markup))
        return self.message_result

    def send_admin_media_group(self, file_paths, captions=None, parse_mode="HTML"):
        self.calls.append(("media_group", list(file_paths), list(captions or []), parse_mode))
        return True

    def send_admin_photo(self, file_path, caption="", parse_mode="HTML", reply_markup=None):
        self.calls.append(("photo", file_path, caption, parse_mode, reply_markup))
        return True

    def send_admin_document(self, file_path, caption="", filename=None, parse_mode="HTML", reply_markup=None, **kwargs):
        self.calls.append(("document", file_path, caption, filename, parse_mode, reply_markup))
        return True


class CustomPrintNotificationUnitTests(unittest.TestCase):
    def setUp(self):
        # Fake leads exercise message delivery without claiming a database row.
        slot_patch = patch("storefront.custom_print_notifications._claim_notification_slot", return_value=True)
        slot_patch.start()
        self.addCleanup(slot_patch.stop)

    def test_telegram_keyboards_only_use_http_urls(self):
        lead = FakeLead()
        lead.contact_channel = "phone"
        lead.contact_value = "+380661815408"
        lead.telegram_verified_phone = "+380661815408"
        lead.telegram_verified_user_id = 123456

        for markup in (_info_reply_markup_full(lead), _moderation_reply_markup(lead)):
            urls = [
                button["url"]
                for row in markup["inline_keyboard"]
                for button in row
                if "url" in button
            ]
            self.assertTrue(urls)
            self.assertTrue(all(url.startswith(("http://", "https://")) for url in urls))
            self.assertFalse(any(url.startswith(("tel:", "tg:")) for url in urls))

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

    def test_build_message_reports_selected_color_and_actual_preview_fallback(self):
        lead = FakeLead()
        lead.product_type = "tshirt"
        lead.fit = "regular"
        lead.fabric = "standard"
        lead.color_choice = "khaki"
        lead.config_draft_json = {
            "product": {"type": "tshirt", "fit": "regular", "fabric": "standard", "color": "khaki"},
            "ui": {
                "preview_render": {
                    "selected_color": "khaki",
                    "preview_color": "black",
                    "fallback_used": True,
                    "profile": "tshirt:regular",
                }
            },
        }

        message = _build_message(lead)

        self.assertIn("khaki", message)
        self.assertIn("На сцені показано", message)
        self.assertIn("Чорний", message)

    def test_safe_exit_message_reports_actual_preview_fallback(self):
        snapshot = {
            "product": {"type": "tshirt", "fit": "regular", "fabric": "standard", "color": "khaki"},
            "ui": {
                "current_step": "config",
                "preview_render": {
                    "selected_color": "khaki",
                    "preview_color": "black",
                    "fallback_used": True,
                    "profile": "tshirt:regular",
                },
            },
        }

        message = _build_safe_exit_message(snapshot)

        self.assertIn("khaki", message)
        self.assertIn("На сцені показано", message)
        self.assertIn("Чорний", message)

    def test_build_message_includes_gift_text_as_quoted_block(self):
        lead = FakeLead()
        lead.config_draft_json = {
            "order": {"quantity": 1, "gift": {"enabled": True, "text": "З днем народження!"}},
        }

        message = _build_message(lead)

        self.assertIn("🎁", message)
        self.assertIn("Текст для подарунка", message)
        self.assertIn("<blockquote>З днем народження!</blockquote>", message)

    def test_submitted_gift_lead_reports_intent_when_packaging_disabled(self):
        lead = FakeLead()
        lead.client_kind = "personal"
        lead.config_draft_json = {
            "mode": "personal",
            "order_purpose": "gift",
            "order": {"gift": {"enabled": False, "text": "Старий текст"}},
        }
        notifier = FakeNotifier()

        with patch("storefront.custom_print_notifications._build_notifier", return_value=notifier):
            result = notify_new_custom_print_lead(lead)

        self.assertTrue(result)
        message = notifier.calls[0][1]
        self.assertIn("<b>Призначення замовлення:</b> На подарунок", message)
        self.assertIn("<b>Подарункова упаковка:</b> ні", message)
        self.assertNotIn("<b>Подарункова упаковка:</b> так", message)
        self.assertNotIn("Старий текст", message)

    def test_packaging_addon_does_not_imply_gift_order_intent(self):
        lead = FakeLead()
        lead.client_kind = "personal"
        lead.config_draft_json = {
            "mode": "personal",
            "order": {"gift": True},
        }

        message = _build_message(lead)

        self.assertIn("<b>Призначення замовлення:</b> Для себе", message)
        self.assertIn("<b>Подарункова упаковка:</b> так", message)
        self.assertNotIn("<b>Призначення замовлення:</b> На подарунок", message)

    def test_legacy_organization_lead_derives_order_purpose_from_client_kind(self):
        message = _build_message(FakeLead())

        self.assertIn("<b>Призначення замовлення:</b> Для команди чи організації", message)

    def test_safe_exit_gift_intent_is_independent_from_disabled_packaging_dict(self):
        message = _build_safe_exit_message({
            "mode": "personal",
            "order_purpose": "gift",
            "order": {"gift": {"enabled": False}},
        })

        self.assertIn("<b>Призначення замовлення:</b> На подарунок", message)
        self.assertIn("<b>Подарункова упаковка:</b> ні", message)
        self.assertNotIn("<b>Подарункова упаковка:</b> так", message)

    def test_safe_exit_personal_order_with_disabled_packaging_dict_is_not_a_gift(self):
        message = _build_safe_exit_message({
            "mode": "personal",
            "order": {"gift": {"enabled": False, "text": "Зі святом"}},
        })

        self.assertIn("<b>Призначення замовлення:</b> Для себе", message)
        self.assertNotIn("<b>Подарункова упаковка:</b> так", message)

    def test_safe_exit_enabled_packaging_dict_is_reported(self):
        message = _build_safe_exit_message({
            "mode": "personal",
            "order_purpose": "gift",
            "order": {"gift": {"enabled": True}},
        })

        self.assertIn("<b>Призначення замовлення:</b> На подарунок", message)
        self.assertIn("<b>Подарункова упаковка:</b> так", message)

    @override_settings(USE_I18N=True)
    def test_safe_exit_materializes_lazy_zone_labels_without_size_presets(self):
        self.assertIsInstance(ZONE_LABELS["front"], Promise)
        with translation.override("uk"):
            message = _build_safe_exit_message({
                "mode": "personal",
                "order_purpose": "gift",
                "print": {"zones": ["front", "custom"]},
                "order": {"gift": False},
            })

        self.assertIn("<b>Зони:</b> Спереду, Інша зона", message)
        self.assertIn("<b>Призначення замовлення:</b> На подарунок", message)

    @override_settings(USE_I18N=True)
    def test_safe_exit_materializes_fallback_zone_label_for_incomplete_placement(self):
        self.assertIsInstance(ZONE_LABELS["hem"], Promise)
        with translation.override("uk"):
            message = _build_safe_exit_message({
                "mode": "personal",
                "order_purpose": "gift",
                "print": {"zones": ["hem"], "zone_options": {"hem": {"side": ""}}},
            })

        self.assertIn("<b>Зони:</b> Низ виробу", message)

    @override_settings(USE_I18N=True)
    def test_submitted_message_formats_stored_lazy_placement_labels(self):
        lead = FakeLead(placement_specs=[{
            "zone": "front",
            "placement_key": "front",
            "label": ZONE_LABELS["front"],
            "requires_artwork_file": True,
        }])

        with translation.override("uk"):
            message = _build_message(lead)

        self.assertIn("<b>Спереду</b>", message)

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

    def test_failed_summary_does_not_send_attachments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "back.png"
            image_path.write_bytes(b"fake-image")
            lead = FakeLead(
                attachments=[
                    SimpleNamespace(
                        placement_zone="back",
                        file=SimpleNamespace(path=str(image_path), name=str(image_path.name)),
                    )
                ]
            )
            notifier = FakeNotifier(message_result=False)

            with patch("storefront.custom_print_notifications._build_notifier", return_value=notifier):
                result = notify_new_custom_print_lead(lead)

        self.assertFalse(result)
        self.assertEqual([call[0] for call in notifier.calls], ["message"])


if __name__ == "__main__":
    unittest.main()
