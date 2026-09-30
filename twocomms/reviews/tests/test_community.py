"""Privacy, abuse protection, moderation and truthful public ratings."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from reviews.models import Review, ReviewCampaign, ReviewSubmissionWindow
from reviews.services.aggregate import aggregate_rating_for_product
from reviews.services.merchant import build_product_review_feed
from storefront.models import Category, Product


class CommunityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Community", slug="community")
        cls.product = Product.objects.create(title="Community Tee", slug="community-tee", category=cls.category, price=500, status="published")
        cls.user = get_user_model().objects.create_user(username="community-user")
        cls.staff = get_user_model().objects.create_user(username="community-staff", is_staff=True)

    def setUp(self):
        self.url = reverse("reviews:submit", args=[self.product.slug])
        self.state = reverse("reviews:state", args=[self.product.slug])
        self.data = {"kind": "review", "rating": "2", "body": "Тканина приємна, але посадка для мене завелика.", "author_name": "Ніка", "email": "nika@example.com"}

    def post(self, client=None, **extra):
        return (client or self.client).post(self.url, {**self.data, **extra}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_private_guest_state_survives_ip_change_and_isolated_from_others(self):
        self.assertEqual(self.post().status_code, 200)
        review = Review.objects.get()
        self.assertTrue(review.anon_key)
        self.assertEqual(review.status, "pending")
        own = self.client.get(self.state, REMOTE_ADDR="198.51.100.9")
        self.assertIn(self.data["body"], own.json()["html"])
        self.assertIn("no-store", own["Cache-Control"])
        other = Client().get(self.state)
        self.assertFalse(other.json()["has_review"])
        self.assertNotIn(self.data["body"], other.json()["html"])
        self.assertEqual(aggregate_rating_for_product(self.product).count, 0)
        self.assertNotContains(Client().get(reverse("product", args=[self.product.slug]), {"review": review.pk}), self.data["body"])

    def test_separate_guests_same_ip_have_separate_owners(self):
        self.post()
        other = Client()
        self.assertEqual(self.post(other, author_name="Інша людина").status_code, 200)
        self.assertEqual(Review.objects.values("anon_key").distinct().count(), 2)
        self.assertNotIn("Інша людина", self.client.get(self.state).json()["html"])

    def test_session_rotation_preserves_ownership(self):
        self.post()
        session = self.client.session
        session.cycle_key()
        session.save()
        self.client.cookies["sessionid"] = session.session_key
        self.assertTrue(self.client.get(self.state).json()["has_review"])

    def test_duplicate_submission_is_idempotent_and_db_constrained(self):
        self.post()
        self.assertEqual(self.post().status_code, 409)
        row = Review.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Review.objects.create(product=self.product, rating=4, body="a"*30, submission_identity=row.submission_identity)
        self.assertEqual(Review.objects.count(), 1)

    def test_comment_has_no_rating_even_if_client_sends_stars(self):
        self.assertEqual(self.post(kind="comment", rating="5").status_code, 200)
        row = Review.objects.get()
        self.assertIsNone(row.rating)
        row.mark_approved(by=self.staff)
        self.assertEqual(aggregate_rating_for_product(self.product).count, 0)

    def test_review_requires_deliberate_rating(self):
        response = self.post(rating="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("rating", response.json()["errors"])

    def test_contacts_and_markup_rejected_in_every_public_field(self):
        for field in ("author_name", "city", "title", "body", "pros", "cons"):
            with self.subTest(field=field):
                client = Client()
                response = self.post(client, **{field: "Some text https://spam.example.com"})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json()["errors"])
        self.assertEqual(Review.objects.count(), 0)

    def test_invalid_image_content_rejected_even_with_image_mime(self):
        file = SimpleUploadedFile("pretend.png", b"<script>alert(1)</script>", content_type="image/png")
        self.assertEqual(self.post(images=file).status_code, 400)
        self.assertEqual(Review.objects.count(), 0)

    def test_global_ip_limit_cannot_be_bypassed_with_new_cookies_or_xff(self):
        for n in range(20):
            response = Client().post(self.url, {**self.data, "body":"short"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest", HTTP_X_FORWARDED_FOR=f"198.51.100.{n}")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post(Client()).status_code, 429)

    def test_authenticated_users_are_rate_limited_too(self):
        self.client.force_login(self.user)
        for _ in range(8):
            self.assertEqual(self.post(body="short").status_code, 400)
        self.assertEqual(self.post().status_code, 429)

    def test_csrf_required_and_fresh_state_token_works(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post(client).status_code, 403)
        token = client.get(self.state).json()["csrf"]
        response = client.post(self.url, self.data, HTTP_X_REQUESTED_WITH="XMLHttpRequest", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_moderation_keeps_low_rating_and_updates_summary(self):
        self.post()
        row = Review.objects.get()
        self.client.force_login(self.staff)
        action = reverse("admin_review_action", args=[row.pk])
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(action, {"action":"approve"}).status_code, 200)
        self.assertEqual(aggregate_rating_for_product(self.product).avg, 2.0)
        self.client.post(action, {"action":"pending", "note":"private moderator note"})
        self.assertEqual(aggregate_rating_for_product(self.product).count, 0)
        self.assertNotIn("private moderator note", self.client.get(self.state).json()["html"])

    def test_program_disabled_without_rules_and_cannot_join_without_purchase(self):
        campaign = ReviewCampaign(enabled=True)
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            campaign.full_clean()
        self.assertEqual(self.post(campaign_opt_in="1", email="me@example.com").status_code, 400)
        self.assertFalse(Review.objects.exists())

    def test_optional_data_persists_and_private_email_is_not_rendered(self):
        self.post(city="Київ", pros="Принт", cons="Рукав", email="private@example.com")
        row = Review.objects.get()
        self.assertEqual((row.city, row.pros, row.cons), ("Київ", "Принт", "Рукав"))
        self.assertNotIn("private@example.com", self.client.get(self.state).json()["html"])

    def test_staff_sees_full_text_and_pagination(self):
        for i in range(23):
            Review.objects.create(product=self.product, rating=3, body=f"{i} " + "Довгий текст "*80, author_name="Тестер")
        self.client.force_login(self.staff)
        response = self.client.get("/admin-panel/?section=reviews&review_page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["reviews_rows"]), 3)
        self.assertContains(response, "Довгий текст "*80)

    @patch("storefront.services.marketplace_feeds.iter_feed_offers")
    def test_merchant_feed_schema_identifiers_negative_and_incentivized_reviews(self, offers):
        offers.return_value = [SimpleNamespace(product=self.product, article="TWC-tee", barcode="", product_url="https://twocomms.shop/product/community-tee/")]
        for rating in (1, 5):
            Review.objects.create(product=self.product, rating=rating, body="Реальний досвід використання цієї футболки.", author_name="Покупець", status="approved", is_verified_purchase=True, is_incentivized_review=(rating==1))
        Review.objects.create(product=self.product, rating=None, kind="comment", body="Коментар без оцінки", author_name="Гість", status="approved")
        xml = build_product_review_feed()
        root = ET.fromstring(xml)
        self.assertEqual(len(root.findall("reviews/review")), 2)
        self.assertEqual(root.findtext("reviews/review/ratings/overall"), "1")
        self.assertEqual(root.findtext("reviews/review/is_incentivized_review"), "true")
        self.assertEqual(root.findtext("reviews/review/products/product/product_ids/mpns/mpn"), f"TWC-tee-{self.product.pk}")
        from lxml import etree
        schema = etree.XMLSchema(etree.parse(str(Path(__file__).parent / "fixtures/google_product_reviews_2_4.xsd")))
        schema.assertValid(etree.fromstring(xml))

    def test_empty_feed_is_valid_without_invented_ratings(self):
        from lxml import etree
        xml = build_product_review_feed()
        schema = etree.XMLSchema(etree.parse(str(Path(__file__).parent / "fixtures/google_product_reviews_2_4.xsd")))
        schema.assertValid(etree.fromstring(xml))
        self.assertNotIn(b"<review>", xml)

    def test_pending_product_html_is_private_and_never_shared(self):
        self.post()
        url = reverse("product", args=[self.product.slug])
        own = self.client.get(url)
        self.assertEqual(own.status_code, 200)
        self.assertIn("private", own["Cache-Control"])
        self.assertContains(own, self.data["body"])
        other = Client().get(url)
        self.assertNotContains(other, self.data["body"])

    def test_review_anchor_resolves_old_public_review_inside_product(self):
        rows = [Review.objects.create(product=self.product, rating=4, body=f"Public review {i} with enough text.", author_name="Тестер", status="approved") for i in range(13)]
        url = reverse("product", args=[self.product.slug])
        response = self.client.get(url, {"review": rows[0].pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["review_page"].number, 2)
        self.assertContains(response, rows[0].body)
        from storefront.seo_utils import StructuredDataGenerator
        schema = StructuredDataGenerator.generate_product_schema(response.context["product"], review_summary=response.context["product_review_summary"])
        self.assertEqual({r["reviewBody"] for r in schema.get("review", [])}, {r.body for r in response.context["approved_reviews"]})

    def test_login_keeps_same_browser_guest_submission_visible(self):
        self.post()
        self.client.force_login(self.user)
        self.assertTrue(self.client.get(self.state).json()["has_review"])
        self.assertEqual(self.post().status_code, 409)

    def test_private_moderator_notes_never_show_in_account(self):
        Review.objects.create(user=self.user, product=self.product, rating=1, body="Private note test body", status="rejected", moderation_note="INTERNAL-DO-NOT-PUBLISH")
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("reviews:my_reviews")), "INTERNAL-DO-NOT-PUBLISH")

    def test_guest_email_required_but_not_proof_of_purchase(self):
        self.assertEqual(self.post(email="").status_code, 400)
        self.assertEqual(self.post(email="not-an-email").status_code, 400)
        self.assertEqual(self.post().status_code, 200)
        self.assertFalse(Review.objects.get().is_verified_purchase)

    def test_authenticated_member_can_review_without_email(self):
        self.client.force_login(self.user)
        self.assertEqual(self.post(email="").status_code, 200)

    def test_initial_comment_does_not_block_later_product_review(self):
        self.assertEqual(self.post(kind="comment", rating="").status_code, 200)
        state = self.client.get(self.state).json()
        self.assertFalse(state["form_complete"])
        self.assertEqual(state["submitted_kinds"], ["comment"])
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(Review.objects.count(), 2)
        self.assertTrue(self.client.get(self.state).json()["form_complete"])


    def test_single_form_accepts_optional_rating_without_type_selector(self):
        data = {k: v for k, v in self.data.items() if k != "kind"}
        data["rating"] = ""
        response = self.client.post(self.url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Review.objects.get().kind, "comment")
        self.assertIsNone(Review.objects.get().rating)
        data["rating"] = "4"
        self.assertEqual(self.client.post(self.url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 200)
        self.assertEqual(Review.objects.get(kind="review").rating, 4)
        self.assertTrue(self.client.get(self.state).json()["form_complete"])
