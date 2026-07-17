import re
import struct
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "twocomms/twocomms_django_theme/templates/pages/custom_print.html"
CSS = REPO_ROOT / "twocomms/twocomms_django_theme/static/css/custom-print-guided-studio.css"
JS_FILES = (
    "custom-print-state.js",
    "custom-print-preview.js",
    "custom-print-mobile-shell.js",
    "custom-print-submit-flow.js",
    "custom-print-configurator.js",
)
STATIC_PAGES = REPO_ROOT / "twocomms/storefront/views/static_pages.py"
ASSET_DIR = REPO_ROOT / "twocomms/twocomms_django_theme/static/img/configurator/studio"


class CustomPrintGuidedStudioSourceTests(unittest.TestCase):
    def setUp(self):
        self.template = TEMPLATE.read_text(encoding="utf-8")
        self.css = CSS.read_text(encoding="utf-8")
        js_dir = REPO_ROOT / "twocomms/twocomms_django_theme/static/js"
        self.js = "\n".join((js_dir / name).read_text(encoding="utf-8") for name in JS_FILES)

    def test_template_exposes_established_eight_stage_journey(self):
        self.assertEqual(
            re.findall(r'data-studio-step="([^"]+)"', self.template),
            ["format", "garment", "config", "placement", "artwork", "quantity", "gift", "contact"],
        )
        self.assertIn("{% trans 'Крок 1 з 8' %}", self.template)
        self.assertIn("data-step-pattern=\"{% trans 'Крок {current} з {total}' %}\"", self.template)

    def test_hero_has_one_primary_start_action_and_no_telegram(self):
        hero = self.template.split('data-custom-print-hero', 1)[1].split('</header>', 1)[0]
        self.assertEqual(hero.count('data-action-start'), 1)
        self.assertNotIn("Telegram", hero)
        self.assertNotIn("telegram", hero.lower())
        for legacy_contract in (
            "cp-hero-badge",
            "cp-hero-feature",
            "cp-hero-options",
            "cp-hero-palette",
            "cp-hero-proof-row",
            "cp-hero-signature",
        ):
            self.assertNotIn(legacy_contract, hero)

    def test_manager_contact_is_one_app_action_not_repeated_telegram_links(self):
        self.assertEqual(self.template.count("data-manager-open"), 1)
        self.assertNotIn("cp-manager-inline-link", self.template)
        self.assertNotIn("cp-manager-shortcut-link", self.template)
        self.assertNotIn("cp-manager-shortcut-draft", self.template)
        self.assertNotIn("data-safe-exit-trigger", self.template)

    def test_hero_heading_has_a_non_concatenated_accessible_name(self):
        self.assertIn("aria-label=\"{% trans 'Створи річ, що говорить за тебе' %}\"", self.template)
        translations = {
            "en": "Create a piece that speaks for you",
            "ru": "Создай вещь, которая говорит за тебя",
        }
        for language, expected in translations.items():
            catalog = (REPO_ROOT / f"twocomms/locale/{language}/LC_MESSAGES/django.po").read_text(encoding="utf-8")
            self.assertIn('msgid "Створи річ, що говорить за тебе"', catalog)
            self.assertIn(f'msgstr "{expected}"', catalog)

    def test_preview_uses_png_scene_without_legacy_garment_rotor(self):
        for legacy_contract in (
            "data-stage-rotor",
            "data-stage-overlay",
            "data-garment",
            "data-zone-layer",
        ):
            self.assertNotIn(legacy_contract, self.template)

    def test_lobby_collapses_unused_preview_column(self):
        self.assertRegex(
            self.css,
            r"\.cp-workbench\.is-lobby-mode\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)",
        )

    def test_artwork_file_validation_focuses_upload_control(self):
        self.assertIn("artwork_file_required", self.js)
        self.assertRegex(
            self.js,
            r"artwork_file_required[^\n]+\[data-dropzone-input\]",
        )

    def test_template_has_app_shell_preview_manager_and_cart_review_dialogs(self):
        for contract in (
            "data-studio-appbar",
            "data-mobile-action-bar",
            "data-preview-dialog",
            "data-manager-dialog",
            "data-cart-review-dialog",
        ):
            self.assertIn(contract, self.template)

    def test_seo_support_stack_sits_outside_the_studio_shell(self):
        shell_end = self.template.index("    <div class=\"cp-support-stack\">")
        configurator_end = self.template.rfind("    </div>\n\n    <div class=\"container-xxl\">", 0, shell_end)
        self.assertGreater(configurator_end, -1)

    def test_submit_flow_does_not_auto_redirect_to_cart(self):
        self.assertNotRegex(
            self.js,
            r"setTimeout\s*\([^)]*window\.location(?:\.href)?\s*=\s*data\.cart_url",
        )
        self.assertIn("openCartReviewDialog", self.js)
        self.assertIn("lead_number", self.js)
        self.assertIn("cart_url", self.js)

    def test_guided_studio_tracks_new_interactions_and_portals_mobile_bar(self):
        for event_name in ("preview_open", "step_complete", "manager_open", "draft_resume"):
            self.assertIn(event_name, self.js)
        self.assertRegex(self.js, r"document\.body\.append(?:Child)?\(.*mobile")

    def test_preview_and_handoff_dialogs_escape_containing_layout(self):
        submit_flow = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-submit-flow.js").read_text(encoding="utf-8")
        self.assertRegex(submit_flow, r"document\.body\.append(?:Child)?\(dialog\)")
        self.assertIn("is-refreshing", (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-preview.js").read_text(encoding="utf-8"))
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        self.assertLess(configurator.index("CustomPrintPreview?.create"), configurator.index("CustomPrintSubmitFlow?.create"))

    def test_css_defines_desktop_studio_mobile_shell_and_reduced_motion(self):
        for contract in (
            "grid-template-columns: minmax(0, 42fr) minmax(0, 58fr)",
            ".cp-studio-appbar",
            ".cp-mobile-action-bar",
            "body.cp-studio-active > .navbar",
            "body.cp-studio-active > .bottom-nav",
            "body.cp-studio-active { overflow: visible !important; }",
            "body.cp-studio-active .cp-workbench { grid-template-columns: minmax(0, 1fr); }",
            "@media (max-width: 1100px)",
            ".cp-stage-card { display: none !important; }",
            ".cp-page.is-studio-active { overflow: visible; }",
            "prefers-reduced-motion: reduce",
        ):
            self.assertIn(contract, self.css)

    def test_custom_print_source_has_no_replacement_character_mojibake(self):
        source = STATIC_PAGES.read_text(encoding="utf-8")
        self.assertNotIn("�", source)
        self.assertNotIn("��", source)

    def test_preview_png_assets_share_a_transparent_1200_by_1400_canvas(self):
        names = (
            "hoodie-regular-front.png",
            "hoodie-regular-back.png",
            "hoodie-oversize-front.png",
            "hoodie-oversize-back.png",
            "tshirt-regular-front.png",
            "tshirt-regular-back.png",
            "tshirt-oversize-front.png",
            "tshirt-oversize-back.png",
            "longsleeve-front.png",
            "longsleeve-back.png",
            "hoodie-lacing.png",
        )
        for name in names:
            data = (ASSET_DIR / name).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", name)
            width, height, _depth, color_type = struct.unpack(">IIBB", data[16:26])
            self.assertEqual((width, height), (1200, 1400), name)
            self.assertIn(color_type, (4, 6), name)


if __name__ == "__main__":
    unittest.main()
