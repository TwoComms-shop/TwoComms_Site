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
        self.assertIn("data-manager-quick-contact", self.template)
        self.assertIn("Обговорити в Telegram", self.template)
        self.assertNotIn("cp-manager-inline-link", self.template)
        self.assertNotIn("cp-manager-shortcut-link", self.template)
        self.assertNotIn("cp-manager-shortcut-draft", self.template)
        self.assertNotIn("data-safe-exit-trigger", self.template)
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        self.assertIn("addEventListener(\"click\", openManagerDialog)", configurator)
        self.assertNotIn("window.open(buildManagerTelegramUrl", configurator)

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
        self.assertIn("data-final-checklist", self.template)
        self.assertIn("data-manager-summary", self.template)
        self.assertIn("data-studio-boundary", self.template)

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

    def test_mobile_app_shell_owns_scroll_and_has_one_bottom_clearance(self):
        self.assertIn("--cp-mobile-bar-clearance", self.css)
        self.assertIn(
            "body.cp-studio-active > header:not(.cp-studio-appbar)",
            self.css,
        )
        self.assertRegex(
            self.css,
            r"@media \(min-width: 1101px\)[\s\S]*?body\s*>\s*\.cp-studio-appbar\s*\{[^}]*display:\s*none",
        )
        self.assertRegex(
            self.css,
            r"@media \(min-width: 1101px\)[\s\S]*?\.cp-page\.is-studio-active\s*\{[^}]*overflow:\s*visible",
        )
        self.assertRegex(
            self.css,
            r"@media \(min-width: 1101px\)[\s\S]*?body\.cp-studio-active\s*\{[^}]*overflow:\s*visible\s*!important",
        )
        self.assertRegex(
            self.css,
            r"@media \(min-width: 1101px\)[\s\S]*?\.cp-stage-card\s*\{[^}]*top:\s*84px;[^}]*max-height:\s*calc\(100svh - 104px\)",
        )
        self.assertRegex(
            self.css,
            r"body\.cp-studio-active\s*\{[^}]*overflow:\s*hidden\s*!important",
        )
        self.assertRegex(
            self.css,
            r"\.cp-page\.is-studio-active\s+\.cp-studio-appbar,\s*body\.cp-studio-active\s*>\s*\.cp-studio-appbar\s*\{[^}]*position:\s*fixed",
        )
        self.assertRegex(
            self.css,
            r"body\.cp-studio-active\s+\.cp-step-viewport\s*\{[^}]*overflow-y:\s*auto",
        )
        self.assertRegex(
            self.css,
            r"body\.cp-studio-active\s*>\s*\.cp-mobile-action-bar\s*\{[^}]*display:\s*grid",
        )
        self.assertIn(
            "body.cp-studio-active .cp-build-strip-shell { display: none !important; }",
            self.css,
        )
        self.assertNotRegex(
            self.css,
            r"body\.cp-studio-active\s+\.cp-waterfall\s*\{[^}]*padding-bottom",
        )

    def test_mobile_preview_dialog_prevents_background_scroll_chaining(self):
        submit_flow = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-submit-flow.js").read_text(encoding="utf-8")
        self.assertIn("cp-dialog-open", submit_flow)
        self.assertIn("lockDocumentScroll", submit_flow)
        self.assertIn("unlockDocumentScroll", submit_flow)
        self.assertIn("overscroll-behavior: contain", self.css)

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
            "body.cp-studio-active { overflow: hidden !important; }",
            "body.cp-studio-active .cp-workbench { grid-template-columns: minmax(0, 1fr); }",
            "@media (max-width: 1100px)",
            ".cp-stage-card { display: none !important; }",
            ".cp-page.is-studio-active .cp-studio-appbar",
            "grid-template-columns: repeat(2, minmax(0, 1fr));",
            ".cp-fabric-row { display: grid;",
            ".cp-scroll-anchor",
            "prefers-reduced-motion: reduce",
        ):
            self.assertIn(contract, self.css)

    def test_mobile_navigation_uses_one_offset_aware_scroll_helper(self):
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        self.assertIn("function scrollToStudioTarget", configurator)
        self.assertNotIn("activeStep.parentNode.insertBefore(dom.progressShell", configurator)
        self.assertNotIn("field?.scrollIntoView({ behavior: \"smooth\", block: \"center\" })", configurator)
        self.assertIn("studioManuallyExited", configurator)
        self.assertNotIn("const shouldRelease = rect.top", configurator)
        self.assertIn('window.scrollTo({ top: 0, behavior:', configurator)

    def test_resolved_step_warnings_do_not_leak_into_the_final_step(self):
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        self.assertIn("dom.statusBox.dataset.defaultStatus", configurator)
        self.assertIn("function resetStatus()", configurator)
        self.assertIn('dom.statusBox?.classList.contains("is-warning") && canAdvance(STATE.ui.current_step)', configurator)

    def test_fabric_info_is_a_keyboard_safe_control(self):
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        self.assertIn("cp-fabric-info-trigger", configurator)
        self.assertIn("keydown", configurator)
        self.assertNotIn('<span role="button" class="cp-fabric-info-trigger"', configurator)
        self.assertIn('modal.setAttribute("aria-modal", "true")', configurator)
        self.assertIn(".cp-fabric-modal-overlay.is-visible", self.css)

    def test_mobile_fabric_descriptions_are_not_line_clamped(self):
        self.assertRegex(
            self.css,
            r"@media \(max-width: 760px\)[\s\S]*?\.cp-fabric-chip-hint\s*\{[^}]*overflow:\s*visible;[^}]*-webkit-line-clamp:\s*unset",
        )

    def test_fit_and_fabric_palette_resolvers_feed_preview_and_refresh(self):
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        preview = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-preview.js").read_text(encoding="utf-8")
        self.assertIn("function getAllowedColorOptions", configurator)
        self.assertIn("renderColorChips();", configurator)
        self.assertIn("productConfig.fit_colors?.[state.product.fit]", preview)

    def test_mobile_shell_reactivation_keeps_bar_visible_after_scroll(self):
        mobile_shell = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-mobile-shell.js").read_text(encoding="utf-8")
        self.assertIn("mobileBar.hidden = !active", mobile_shell)
        self.assertIn("document.body.append(appbar)", mobile_shell)
        self.assertIn('data-studio-exit', self.template)
        self.assertIn('aria-pressed="false"', self.template)

    def test_artwork_controls_have_icon_and_stable_upload_contract(self):
        self.assertIn("cp-artwork-service-icon", self.template)
        self.assertIn("cp-field-label-row", self.template)
        self.assertIn("cp-dropzone input[type=\"file\"]", self.css)
        self.assertIn("cp-dropzone-progress", self.css)
        self.assertIn(".cp-artwork-services > *", self.css)
        self.assertIn("min-width: 0", self.css)

    def test_mobile_placement_has_an_eye_preview_hint(self):
        self.assertIn("cp-mobile-preview-hint", self.template)
        self.assertIn("data-preview-open", self.template)
        self.assertIn("кнопку з оком угорі", self.template)
        self.assertRegex(
            self.css,
            r"@media \(min-width: 1101px\)[^{]*\{[^}]*\.cp-mobile-preview-hint\s*\{[^}]*display:\s*none",
        )

    def test_fit_cards_use_historical_selector_art_not_stage_silhouettes(self):
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        for asset in (
            "ui/tshirt-regular.png",
            "ui/tshirt-oversize.png",
            "ui/hoodie-regular.png",
            "ui/hoodie-oversize.png",
        ):
            self.assertIn(asset, configurator)

    def test_thermo_cards_and_swatches_expose_clear_mobile_identity(self):
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        self.assertIn("cp-swatch--thermo", configurator)
        self.assertIn("cp-swatch-thermo-icon", configurator)
        self.assertIn("Термохромна тканина", configurator)
        self.assertIn("min-width: 44px", self.css)

    def test_gift_step_has_one_dynamic_continue_action(self):
        gift_step = self.template.split('data-step="gift"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn("data-step-skip", gift_step)
        self.assertIn("data-gift-continue", gift_step)
        configurator = (REPO_ROOT / "twocomms/twocomms_django_theme/static/js/custom-print-configurator.js").read_text(encoding="utf-8")
        self.assertIn("Продовжити без подарункової упаковки", configurator)
        self.assertIn("Продовжити з подарунковою упаковкою", configurator)

    def test_hero_calibration_keeps_print_frames_on_garments_on_mobile(self):
        self.assertIn(".cp-hero-print-zone--hoodie { left: 12%; }", self.css)
        self.assertIn(".cp-hero-print-zone--longsleeve { left: 78%; }", self.css)
        self.assertIn(".cp-hero-theatre { inset: 48% 0 0; }", self.css)

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
