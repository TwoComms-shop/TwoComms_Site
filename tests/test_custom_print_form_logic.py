import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "twocomms.settings")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import django

django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile

from storefront.forms import CustomPrintLeadForm


PNG_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_png(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PNG_PIXEL, content_type="image/png")


def base_payload(**overrides):
    payload = {
        "service_kind": "ready",
        "product_type": "hoodie",
        "placements": ["sleeve"],
        "placement_specs_json": json.dumps(
            [
                {
                    "zone": "sleeve",
                    "placement_key": "sleeve_left",
                    "label": "Лівий рукав",
                    "mode": "full_text",
                    "text": "TWOCOMMS",
                    "requires_artwork_file": False,
                }
            ]
        ),
        "pricing_snapshot_json": json.dumps(
            {
                "product_label": "Худі",
                "base_price": 1800,
                "design_price": 0,
                "discount_percent": 0,
                "discount_amount": 0,
                "final_total": 1800,
                "estimate_required": False,
            }
        ),
        "quantity": "1",
        "size_mode": "single",
        "sizes_note": "L x1",
        "client_kind": "personal",
        "name": "Тест",
        "contact_channel": "telegram",
        "contact_value": "@twocomms_test",
        "brief": "",
        "fit": "oversize",
        "fabric": "premium",
        "color_choice": "graphite",
        "file_triage_status": "print-ready",
        "config_draft_json": json.dumps(
            {
                "version": 2,
                "mode": "personal",
                "product": {
                    "type": "hoodie",
                    "fit": "oversize",
                    "fabric": "premium",
                    "color": "graphite",
                },
                "print": {
                    "zones": ["sleeve"],
                    "zone_options": {
                        "sleeve": {
                            "left_enabled": True,
                            "right_enabled": False,
                            "left_mode": "full_text",
                            "left_text": "TWOCOMMS",
                        },
                    },
                },
                "artwork": {
                    "service_kind": "ready",
                    "triage_status": "print-ready",
                    "files": [],
                },
                "order": {
                    "quantity": 1,
                    "size_mode": "single",
                    "sizes_note": "L x1",
                },
                "contact": {
                    "channel": "telegram",
                    "name": "Тест",
                    "value": "@twocomms_test",
                },
                "ui": {"current_step": "contact"},
            }
        ),
    }
    payload.update(overrides)
    return payload


class CustomPrintLeadFormLogicTests(unittest.TestCase):
    def test_legacy_hoodie_form_removes_cached_hardware_fee_from_separate_price_snapshot(self):
        from tests.test_custom_print_creation_unit import item_snapshot
        snapshot = item_snapshot("hoodie", 2, 3900)
        snapshot["pricing"].update(addons_price=150, unit_total=1950, base_price=1800)
        snapshot["print"]["add_ons"] = ["no_fleece", "grommets"]
        snapshot["print"]["zones"] = ["sleeve"]
        form = CustomPrintLeadForm(base_payload(quantity="2", config_draft_json=json.dumps(snapshot),
                                               pricing_snapshot_json=json.dumps(snapshot["pricing"])))
        self.assertTrue(form.is_valid(), form.errors)
        for price in (form.cleaned_data["pricing_snapshot_json"], form.cleaned_data["config_draft_json"]["pricing"]):
            self.assertEqual(price["base_price"], 1800)
            self.assertEqual(price["unit_total"], 1800)
            self.assertEqual(price["final_total"], 3600)
            self.assertEqual(price["addons_price"], 0)
        self.assertEqual(form.cleaned_data["config_draft_json"]["print"]["add_ons"], ["fleece", "lacing"])

    def test_ready_manager_submission_now_blocks_missing_files_for_upload_backed_placements(self):
        """CP-UX-2026-05-18 regression.

        Раніше менеджер-сабмішн пропускав кейс «обрав готовий файл, але не
        прикріпив»: клієнт міг надіслати порожню заявку. Після фікса для
        service_kind=ready / adjust файли є обовʼязковими ЗАВЖДИ, навіть
        якщо view передає require_artwork_files=False.
        """
        payload = base_payload(
            placements=["back"],
            placement_specs_json=json.dumps(
                [
                    {
                        "zone": "back",
                        "placement_key": "back",
                        "label": "На спині",
                        "size_preset": "A2",
                        "requires_artwork_file": True,
                        "file_index": 0,
                    }
                ]
            ),
            config_draft_json=json.dumps(
                {
                    "version": 2,
                    "submission_type": "lead",
                    "mode": "personal",
                    "product": {
                        "type": "hoodie",
                        "fit": "oversize",
                        "fabric": "premium",
                        "color": "graphite",
                    },
                    "print": {
                        "zones": ["back"],
                        "zone_options": {
                            "back": {
                                "size_preset": "A2",
                            },
                        },
                    },
                    "artwork": {
                        "service_kind": "ready",
                        "triage_status": "print-ready",
                        "files": [],
                    },
                    "order": {
                        "quantity": 1,
                        "size_mode": "single",
                        "sizes_note": "L x1",
                    },
                    "contact": {
                        "channel": "telegram",
                        "name": "Тест",
                        "value": "@twocomms_test",
                    },
                    "ui": {"current_step": "contact"},
                }
            ),
        )

        form = CustomPrintLeadForm(payload, {}, require_artwork_files=False)

        self.assertFalse(form.is_valid())
        self.assertIn("files", form.errors)

    def test_ready_cart_submission_still_requires_missing_files_for_upload_backed_placements(self):
        payload = base_payload(
            placements=["back"],
            placement_specs_json=json.dumps(
                [
                    {
                        "zone": "back",
                        "placement_key": "back",
                        "label": "На спині",
                        "size_preset": "A2",
                        "requires_artwork_file": True,
                        "file_index": 0,
                    }
                ]
            ),
            config_draft_json=json.dumps(
                {
                    "version": 2,
                    "submission_type": "cart",
                    "mode": "personal",
                    "product": {
                        "type": "hoodie",
                        "fit": "oversize",
                        "fabric": "premium",
                        "color": "graphite",
                    },
                    "print": {
                        "zones": ["back"],
                        "zone_options": {
                            "back": {
                                "size_preset": "A2",
                            },
                        },
                    },
                    "artwork": {
                        "service_kind": "ready",
                        "triage_status": "print-ready",
                        "files": [],
                    },
                    "order": {
                        "quantity": 1,
                        "size_mode": "single",
                        "sizes_note": "L x1",
                    },
                    "contact": {
                        "channel": "telegram",
                        "name": "Тест",
                        "value": "@twocomms_test",
                    },
                    "ui": {"current_step": "contact"},
                }
            ),
        )

        form = CustomPrintLeadForm(payload, {}, require_artwork_files=True)

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["files"],
            ["Додайте файли для всіх зон, де потрібен макет."],
        )

    def test_ready_submission_with_text_only_sleeve_is_valid_without_files(self):
        form = CustomPrintLeadForm(base_payload(), {})

        self.assertTrue(form.is_valid(), form.errors)

    def test_adjust_submission_requires_files_only_for_upload_backed_placements(self):
        payload = base_payload(
            service_kind="adjust",
            brief="Підчистити фон.",
            placements=["back", "sleeve"],
            placement_specs_json=json.dumps(
                [
                    {
                        "zone": "back",
                        "placement_key": "back",
                        "label": "На спині",
                        "size_preset": "A2",
                        "requires_artwork_file": True,
                        "file_index": 0,
                    },
                    {
                        "zone": "sleeve",
                        "placement_key": "sleeve_left",
                        "label": "Лівий рукав",
                        "mode": "full_text",
                        "text": "TWOCOMMS",
                        "requires_artwork_file": False,
                    },
                    {
                        "zone": "sleeve",
                        "placement_key": "sleeve_right",
                        "label": "Правий рукав",
                        "mode": "a6",
                        "requires_artwork_file": True,
                        "file_index": 1,
                    },
                ]
            ),
        )

        form = CustomPrintLeadForm(payload, {})

        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["files"],
            ["Додайте файли для всіх зон, де потрібен макет."],
        )

    def test_save_maps_attachment_to_concrete_placement_key(self):
        payload = base_payload(
            placements=["front", "sleeve"],
            placement_specs_json=json.dumps(
                [
                    {
                        "zone": "front",
                        "placement_key": "front",
                        "label": "Спереду",
                        "size_preset": "A4",
                        "requires_artwork_file": True,
                        "file_index": 0,
                    },
                    {
                        "zone": "sleeve",
                        "placement_key": "sleeve_left",
                        "label": "Лівий рукав",
                        "mode": "full_text",
                        "text": "TWOCOMMS",
                        "requires_artwork_file": False,
                    },
                    {
                        "zone": "sleeve",
                        "placement_key": "sleeve_right",
                        "label": "Правий рукав",
                        "mode": "a6",
                        "requires_artwork_file": True,
                        "file_index": 1,
                    },
                ]
            ),
        )
        files = {"files": [make_png("front.png"), make_png("right-sleeve.png")]}
        form = CustomPrintLeadForm(payload, files)

        self.assertTrue(form.is_valid(), form.errors)

        dummy_lead = SimpleNamespace(pk=101)
        with patch("storefront.forms.CustomPrintLead.objects.create", return_value=dummy_lead) as create_lead:
            with patch("storefront.forms.CustomPrintLeadAttachment.objects.create") as create_attachment:
                lead = form.save()

        self.assertIs(lead, dummy_lead)
        create_lead.assert_called_once()
        self.assertEqual(create_attachment.call_count, 2)
        first_call = create_attachment.call_args_list[0].kwargs
        second_call = create_attachment.call_args_list[1].kwargs
        self.assertEqual(first_call["placement_zone"], "front")
        self.assertEqual(second_call["placement_zone"], "sleeve_right")

    def test_save_accepts_source_and_moderation_overrides_for_initial_insert(self):
        payload = base_payload(
            placements=["front"],
            placement_specs_json=json.dumps(
                [
                    {
                        "zone": "front",
                        "placement_key": "front",
                        "label": "Спереду",
                        "size_preset": "A4",
                        "requires_artwork_file": True,
                        "file_index": 0,
                    }
                ]
            ),
            config_draft_json=json.dumps(
                {
                    "version": 2,
                    "mode": "personal",
                    "product": {
                        "type": "hoodie",
                        "fit": "oversize",
                        "fabric": "premium",
                        "color": "graphite",
                    },
                    "print": {
                        "zones": ["front"],
                        "zone_options": {"front": {"size_preset": "A4"}},
                    },
                    "artwork": {
                        "service_kind": "ready",
                        "triage_status": "print-ready",
                        "files": [{"name": "front.png", "zone": "front", "status": "print-ready"}],
                    },
                    "order": {
                        "quantity": 1,
                        "size_mode": "single",
                        "sizes_note": "L x1",
                    },
                    "contact": {
                        "channel": "telegram",
                        "name": "Тест",
                        "value": "@twocomms_test",
                    },
                    "ui": {"current_step": "contact"},
                }
            ),
        )
        files = {"files": [make_png("front.png")]}
        form = CustomPrintLeadForm(payload, files)

        self.assertTrue(form.is_valid(), form.errors)

        dummy_lead = SimpleNamespace(pk=202)
        with patch("storefront.forms.CustomPrintLead.objects.create", return_value=dummy_lead) as create_lead:
            with patch("storefront.forms.CustomPrintLeadAttachment.objects.create"):
                form.save(source="custom_print_cart", moderation_status="draft")

        self.assertEqual(create_lead.call_args.kwargs["source"], "custom_print_cart")
        self.assertEqual(create_lead.call_args.kwargs["moderation_status"], "draft")


if __name__ == "__main__":
    unittest.main()
