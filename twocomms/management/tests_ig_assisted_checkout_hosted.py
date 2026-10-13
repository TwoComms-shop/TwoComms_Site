"""Actual token entry and rendered assisted checkout, including mixed money."""
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from management.models import IgCheckoutAccessToken, IgCheckoutInvoiceGeneration
from management.tests_ig_assisted_checkout_payment import AssistedCheckoutPaymentFixture
from storefront.views.ig_checkout import CHECKOUT_COPY


@override_settings(ROOT_URLCONF="twocomms.urls", SECURE_SSL_REDIRECT=False,
    IG_FULFILLMENT_REVIEWS_ENABLED=True, IG_ASSISTED_CHECKOUT_V2="enforced",
    IG_ASSISTED_CHECKOUT_V2_CANARY_PERCENT=100, GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeAssistedHostedRenderTests(AssistedCheckoutPaymentFixture, TransactionTestCase):
    def items_for(self, request, **kwargs):
        return super().items_for(request, **{"unit_price": "1590.00", **kwargs})

    def assert_native_hosted_offer(self, language, *, mixed):
        with patch("management.services.instagram_bot._provider_http") as instagram, \
                patch("storefront.views.monobank._monobank_api_request") as bank:
            result = self._custom_proposal(photo=True, mixed=mixed)
            proposal = result["proposal"]
            path = reverse("ig_checkout_proposal", kwargs={"proposal_id": proposal.public_id})
            self.assertEqual(self.client.get(path).status_code, 404)
            raw, token = IgCheckoutAccessToken.issue(proposal=proposal)
            entry = self.client.get(reverse("ig_checkout_token_entry", kwargs={"token": raw}), follow=True)
            self.assertEqual(entry.status_code, 200, entry.content)
            self.assertEqual(entry.redirect_chain, [(path, 302)])
            token.refresh_from_db()
            self.assertEqual(token.use_count, 1)
            page = self.client.get(path, {"lang": language})
            self.assertEqual(page.status_code, 200, page.content)
            self.assertTemplateUsed(page, "pages/ig_checkout.html")
            self.assertEqual(page.context["html_lang"], language)
            self.assertTrue(page.context["has_assisted_terms"])
            self.assertTrue(page.context["has_assisted_custom"])
            self.assertContains(page, CHECKOUT_COPY[language]["assisted_terms_confirmation"])
            self.assertContains(page, 'name="assisted_terms_accepted" type="checkbox" value="1" required')
            self.assertNotContains(page, raw)
            rendered = page.context["items"]
            actual = list(proposal.items.order_by("position", "pk"))
            self.assertEqual(len(rendered), 2 if mixed else 1)
            for item, row in zip(actual, rendered, strict=True):
                self.assertEqual(row["unit_price"], f"{item.quoted_unit_price:.2f}")
                self.assertEqual(row["line_total"], f"{item.quoted_line_total:.2f}")
                self.assertEqual(row["quantity"], item.quantity)
                self.assertTrue(row["is_assisted"])
                self.assertContains(page, row["line_total"] + " UAH")
            custom = rendered[-1]
            self.assertTrue(custom["is_custom"])
            self.assertEqual(custom["unit_price"], "1590.00")
            self.assertEqual(custom["title"], "Фіолетове худі")
            self.assertEqual(custom["color_label"], "Фіолетовий")
            self.assertEqual(custom["size"], "L")
            self.assertContains(page, custom["title"])
            self.assertContains(page, custom["color_label"])
            photo_url = reverse("ig_checkout_item_image", kwargs={"proposal_id": proposal.public_id,
                "item_id": actual[-1].pk})
            self.assertEqual(custom["image_url"], photo_url)
            self.assertContains(page, 'src="' + photo_url + '"')
            self.assertEqual(self.client.get(photo_url).status_code, 200)
            if mixed:
                self.assertFalse(rendered[0]["is_custom"])
                self.assertEqual(rendered[0]["unit_price"], "790.00")
                self.assertEqual(page.context["proposal"]["total"], "2380.00")
            self.assertFalse(IgCheckoutInvoiceGeneration.objects.exists())
            instagram.assert_not_called()
            bank.assert_not_called()

    def test_custom_token_follow_get_uk(self):
        self.assert_native_hosted_offer("uk", mixed=False)

    def test_custom_token_follow_get_ru(self):
        self.assert_native_hosted_offer("ru", mixed=False)

    def test_custom_token_follow_get_en(self):
        self.assert_native_hosted_offer("en", mixed=False)

    def test_mixed_token_follow_get_uk(self):
        self.assert_native_hosted_offer("uk", mixed=True)

    def test_mixed_token_follow_get_ru(self):
        self.assert_native_hosted_offer("ru", mixed=True)

    def test_mixed_token_follow_get_en(self):
        self.assert_native_hosted_offer("en", mixed=True)
