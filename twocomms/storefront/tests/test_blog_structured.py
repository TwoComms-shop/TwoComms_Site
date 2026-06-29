from __future__ import annotations

import json
import io
import tempfile
from pathlib import Path
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation
from PIL import Image

from storefront.models import (
    BlogCategory,
    BlogMediaAsset,
    BlogPost,
    BlogPostBlock,
    BlogPromoCampaign,
    BlogPromoClaim,
    PromoCode,
    UserPromoCode,
)
from storefront.management.commands.publish_custom_print_blog import COVER_ASSET_DIR
from storefront.services.blog_blocks import render_post_blocks
from storefront.services.index_targets import build_blog_urls
from storefront.sitemaps import BlogCategorySitemap


TEST_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
    "fragments": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
}


EXPECTED_CUSTOM_PRINT_BLOG_SLUGS = (
    "futbolka-zi-svoim-dyzainom",
    "hudi-zi-svoim-pryntom",
    "prynt-z-foto-memu-abo-kartynky",
    "yak-pidhotuvaty-maket-dlya-druku",
    "rozmir-pryntu-na-oversize-futbolku",
    "dtf-druk-bez-efektu-nakleyky",
    "dtf-dtg-chy-shovkografiya",
    "futbolky-z-logotypom-dlya-brendu",
    "kastomnyy-druk-dlya-instagram-magazyniv",
    "futbolka-abo-hudi-z-pryntom-u-podarunok",
)


@override_settings(
    COMPRESS_ENABLED=False,
    COMPRESS_OFFLINE=False,
    SITE_BASE_URL="https://twocomms.shop",
    CACHES=TEST_CACHES,
)
class BlogStructuredPublicTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.reviews = BlogCategory.objects.create(name="Огляди Test", slug="reviews-test")
        self.product_reviews = BlogCategory.objects.create(
            name="Огляди продукції Test",
            slug="product-reviews-test",
            parent=self.reviews,
        )
        self.news = BlogCategory.objects.create(name="Новини Test", slug="news-test")
        self.veteran = BlogCategory.objects.create(
            name="Український ветеранський фонд Test",
            slug="veteran-fund-test",
            parent=self.news,
        )

    def test_product_review_without_blocks_does_not_render_veteran_fund_hardcode(self):
        post = BlogPost.objects.create(
            category=self.product_reviews,
            title="Огляд футболки TwoComms",
            slug="shirt-review",
            excerpt="Огляд посадки та тканини.",
            content_html="<p>Матеріал без фондових блоків.</p>",
            published_at=timezone.now(),
            is_published=True,
        )

        response = self.client.get(reverse("blog_post", kwargs={"slug": post.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Огляд футболки TwoComms")
        self.assertContains(response, "Матеріал без фондових блоків")
        self.assertNotContains(response, "Український ветеранський фонд")
        self.assertNotContains(response, "Внутрішній прогрес")
        self.assertNotContains(response, "2/4")
        self.assertNotContains(response, "article-custom-print-cta")
        self.assertNotContains(response, "blog-article-primary-cta")

    def test_structured_blocks_render_only_when_attached_to_post(self):
        fund_post = BlogPost.objects.create(
            category=self.veteran,
            title="Звіт фонду",
            slug="twocomms-veteran-fund-progress-test",
            excerpt="Звіт по етапах.",
            content_html="<p>Legacy fallback.</p>",
            published_at=timezone.now(),
            is_published=True,
        )
        BlogPostBlock.objects.create(
            post=fund_post,
            block_type=BlogPostBlock.BlockType.METRIC_CARDS,
            sort_order=10,
            payload={
                "cards": [
                    {
                        "label": {"uk": "Внутрішній прогрес"},
                        "value": "2/4",
                        "caption": {"uk": "етапи закрито"},
                    }
                ]
            },
        )
        BlogPostBlock.objects.create(
            post=fund_post,
            block_type=BlogPostBlock.BlockType.CTA_GROUP,
            sort_order=20,
            payload={
                "layout": "full",
                "buttons": [
                    {
                        "provider": "telegram",
                        "label": {"uk": "Написати в Telegram"},
                        "url": "https://t.me/twocomms",
                    }
                ],
            },
        )

        response = self.client.get(reverse("blog_post", kwargs={"slug": fund_post.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Внутрішній прогрес")
        self.assertContains(response, "2/4")
        self.assertContains(response, "Написати в Telegram")
        self.assertContains(response, "article-structured-block")
        self.assertContains(response, "article-metric-board")
        self.assertContains(response, "article-cta-panel")
        self.assertContains(response, "article-link-card--telegram")

    def test_product_cta_with_empty_product_id_does_not_break_article(self):
        post = BlogPost.objects.create(
            category=self.product_reviews,
            title="Огляд з товарним блоком",
            slug="review-with-empty-product-cta",
            excerpt="Огляд.",
            content_html="<p>Legacy fallback.</p>",
            published_at=timezone.now(),
            is_published=True,
        )
        BlogPostBlock.objects.create(
            post=post,
            block_type=BlogPostBlock.BlockType.RICH_TEXT,
            sort_order=10,
            payload={"html": {"uk": "<p>Основний текст огляду.</p>"}},
        )
        BlogPostBlock.objects.create(
            post=post,
            block_type=BlogPostBlock.BlockType.PRODUCT_CTA,
            sort_order=20,
            payload={"product_id": "", "title": {"uk": "Купити товар"}, "button_label": {"uk": "До товару"}},
        )

        response = self.client.get(reverse("blog_post", kwargs={"slug": post.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Основний текст огляду")
        self.assertNotContains(response, "Server Error")

    def test_built_css_keeps_python_rendered_blog_block_classes(self):
        css_path = Path(__file__).resolve().parents[2] / "twocomms_django_theme" / "static" / "css" / "styles.purged.css"
        css = css_path.read_text(encoding="utf-8")

        for css_class in (
            ".article-structured-block",
            ".article-metric-board",
            ".article-cta-panel",
            ".article-link-card",
            ".article-source-card",
            ".article-video-lite",
            ".article-spec-table",
            ".article-product-cta",
            ".article-promo-claim",
            ".article-mini-landing",
            ".article-fast-answer",
            ".article-decision-strip",
            ".article-proof-gallery",
            ".article-process-ladder",
            ".article-scenario-grid",
            ".article-scenario-grid--count-",
            ".article-checklist-grid",
            ".article-keyword-cloud",
            ".article-interlink-strip",
            ".article-interlink-grid",
            ".article-top-cta",
            ".article-final-cta",
            ".article-metric-board--count-5",
            ".article-process-steps--count-5",
            ".blog-article-primary-cta",
            ".blog-block-visual-preview",
            ".blog-block-modal",
            ".blog-editor-workbench",
        ):
            with self.subTest(css_class=css_class):
                self.assertIn(css_class, css)

    def test_nested_category_url_filters_child_and_flat_url_redirects(self):
        child_post = BlogPost.objects.create(
            category=self.veteran,
            title="Звіт УВФ",
            slug="fund-report",
            excerpt="Новина фонду.",
            content_html="<p>Звіт.</p>",
            published_at=timezone.now(),
            is_published=True,
        )
        other_post = BlogPost.objects.create(
            category=self.product_reviews,
            title="Огляд худі",
            slug="hoodie-review",
            excerpt="Огляд.",
            content_html="<p>Огляд.</p>",
            published_at=timezone.now(),
            is_published=True,
        )

        nested = self.client.get("/blog/category/news-test/veteran-fund-test/", secure=True)
        legacy = self.client.get("/blog/category/veteran-fund-test/", secure=True, follow=False)

        self.assertEqual(nested.status_code, 200)
        self.assertContains(nested, child_post.title)
        self.assertNotContains(nested, other_post.title)
        self.assertEqual(legacy.status_code, 301)
        self.assertEqual(legacy["Location"], "/blog/category/news-test/veteran-fund-test/")

    def test_child_category_related_posts_do_not_mix_other_parent_fallback(self):
        review_post = BlogPost.objects.create(
            category=self.product_reviews,
            title="Огляд худі TwoComms",
            slug="hoodie-review-related",
            excerpt="Огляд.",
            content_html="<p>Огляд.</p>",
            published_at=timezone.now(),
            is_published=True,
        )
        veteran_post = BlogPost.objects.create(
            category=self.veteran,
            title="Звіт ветеранського фонду",
            slug="fund-report-related",
            excerpt="Звіт.",
            content_html="<p>Звіт.</p>",
            published_at=timezone.now(),
            is_published=True,
        )

        response = self.client.get(reverse("blog_post", kwargs={"slug": review_post.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, review_post.title)
        self.assertNotContains(response, veteran_post.title)

    def test_sitemap_and_index_targets_include_nested_category_urls(self):
        BlogPost.objects.create(
            category=self.veteran,
            title="Звіт УВФ для sitemap",
            slug="fund-report-sitemap",
            excerpt="Новина фонду.",
            content_html="<p>Звіт.</p>",
            published_at=timezone.now(),
            is_published=True,
        )
        sitemap = BlogCategorySitemap()

        self.assertIn(self.veteran, list(sitemap.items()))
        with translation.override("uk"):
            self.assertEqual(sitemap.location(self.veteran), "/blog/category/news-test/veteran-fund-test/")
        urls = build_blog_urls(["uk"])
        self.assertIn("https://twocomms.shop/blog/category/news-test/veteran-fund-test/", urls)

    def test_rich_text_is_sanitized_and_h1_is_removed(self):
        post = BlogPost.objects.create(
            category=self.product_reviews,
            title="Безпечний пост",
            slug="safe-post",
            excerpt="Тест.",
            content_html="",
            published_at=timezone.now(),
            is_published=True,
        )
        BlogPostBlock.objects.create(
            post=post,
            block_type=BlogPostBlock.BlockType.RICH_TEXT,
            sort_order=10,
            payload={
                "html": {
                    "uk": "<h1>Небажаний H1</h1><h2>Дозволений H2</h2><p onclick='x()'>Текст</p><script>alert(1)</script>"
                }
            },
        )

        html, schema = render_post_blocks(post, request=self.client.request().wsgi_request)

        self.assertIn("Дозволений H2", html)
        self.assertIn("<p>Текст</p>", html)
        self.assertNotIn("<h1", html)
        self.assertNotIn("onclick", html)
        self.assertNotIn("script", html)
        self.assertEqual(schema, {})

    def test_faq_block_contributes_schema(self):
        post = BlogPost.objects.create(
            category=self.product_reviews,
            title="FAQ пост",
            slug="faq-post",
            excerpt="FAQ.",
            content_html="",
            published_at=timezone.now(),
            is_published=True,
        )
        BlogPostBlock.objects.create(
            post=post,
            block_type=BlogPostBlock.BlockType.FAQ,
            sort_order=10,
            payload={
                "items": [
                    {
                        "question": {"uk": "Як прати футболку?"},
                        "answer": {"uk": "Прати навиворіт при 30 градусах."},
                    }
                ]
            },
        )

        html, schema = render_post_blocks(post, request=self.client.request().wsgi_request)

        self.assertIn("Як прати футболку?", html)
        self.assertEqual(schema["@type"], "FAQPage")
        self.assertEqual(schema["mainEntity"][0]["name"], "Як прати футболку?")

    def test_metric_cards_emit_count_class_for_balanced_odd_grids(self):
        post = BlogPost.objects.create(
            category=self.product_reviews,
            title="Пост з п'ятьма метриками",
            slug="five-metrics-post",
            excerpt="Метрики.",
            content_html="",
            published_at=timezone.now(),
            is_published=True,
        )
        BlogPostBlock.objects.create(
            post=post,
            block_type=BlogPostBlock.BlockType.METRIC_CARDS,
            sort_order=10,
            payload={
                "cards": [
                    {"label": {"uk": f"Етап {number}"}, "value": str(number), "caption": {"uk": "пояснення"}}
                    for number in range(1, 6)
                ]
            },
        )

        html, _schema = render_post_blocks(post, request=self.client.request().wsgi_request)

        self.assertIn("article-metric-board--count-5", html)
        self.assertIn("--metric-count:5", html)

    def test_publish_custom_print_blog_command_creates_indexable_localized_post_landings(self):
        out = io.StringIO()

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            call_command("publish_custom_print_blog", stdout=out)

            category = BlogCategory.objects.get(slug="custom-print-guides")
            self.assertEqual(category.parent.slug, "guides")
            posts = BlogPost.objects.filter(slug__in=EXPECTED_CUSTOM_PRINT_BLOG_SLUGS).select_related("category")
            self.assertEqual(posts.count(), 10)
            for post in posts:
                with self.subTest(slug=post.slug):
                    self.assertEqual(post.category, category)
                    self.assertTrue(post.is_published)
                    self.assertLessEqual(post.published_at, timezone.now())
                    self.assertTrue(post.cover_image)
                    self.assertTrue(post.cover_image.name.endswith(".webp"))
                    self.assertTrue((Path(media_root) / post.cover_image.name).exists())
                    self.assertTrue((COVER_ASSET_DIR / f"{post.slug}.webp").exists())
                    self.assertTrue(post.title_uk)
                    self.assertTrue(post.title_ru)
                    self.assertTrue(post.seo_title_uk)
                    self.assertTrue(post.seo_title_ru)
                    self.assertTrue(post.seo_description_uk)
                    self.assertTrue(post.seo_description_ru)
                    block_types = set(post.blocks.values_list("block_type", flat=True))
                    self.assertIn(BlogPostBlock.BlockType.CTA_GROUP, block_types)
                    self.assertIn(BlogPostBlock.BlockType.METRIC_CARDS, block_types)
                    self.assertIn(BlogPostBlock.BlockType.FAQ, block_types)
                    self.assertIn(BlogPostBlock.BlockType.SOURCE_LIST, block_types)

            first_post = BlogPost.objects.get(slug=EXPECTED_CUSTOM_PRINT_BLOG_SLUGS[0])
            ordered_blocks = list(first_post.blocks.order_by("sort_order"))
            self.assertEqual(ordered_blocks[0].block_type, BlogPostBlock.BlockType.RICH_TEXT)
            self.assertEqual(ordered_blocks[1].block_type, BlogPostBlock.BlockType.CTA_GROUP)
            self.assertEqual(ordered_blocks[1].payload.get("extra_class"), "article-top-cta")
            self.assertEqual(ordered_blocks[2].block_type, BlogPostBlock.BlockType.RICH_TEXT)
            html, schema = render_post_blocks(first_post, request=self.client.request().wsgi_request)
            self.assertIn("/custom-print/", html)
            self.assertIn("article-fast-answer", html)
            self.assertIn("article-top-cta", html)
            self.assertIn("article-decision-strip", html)
            self.assertIn("article-proof-gallery", html)
            self.assertIn("article-process-ladder", html)
            self.assertIn("article-process-steps--count-5", html)
            self.assertIn("article-interlink-strip", html)
            self.assertIn("article-scenario-grid--count-", html)
            self.assertIn("/catalog/tshirts/", html)
            self.assertIn("/catalog/tshirts/black/", html)
            self.assertIn("/catalog/hoodie/black/", html)
            self.assertIn("Коротко: як це працює", html)
            self.assertIn("Можна почати за 5 хвилин", html)
            self.assertNotIn("Коротко для AI", html)
            self.assertNotIn("Внутрішня навігація", html)
            self.assertEqual(schema["@context"], "https://schema.org")
            self.assertEqual(schema["@type"], "FAQPage")
            self.assertGreaterEqual(len(schema["mainEntity"]), 6)
            for faq_item in schema["mainEntity"]:
                self.assertEqual(faq_item["@type"], "Question")
                self.assertTrue(faq_item["name"])
                self.assertEqual(faq_item["acceptedAnswer"]["@type"], "Answer")
                self.assertTrue(faq_item["acceptedAnswer"]["text"])
                self.assertNotIn("AI", faq_item["name"])
                self.assertNotIn("SEO", faq_item["acceptedAnswer"]["text"])

            response = self.client.get(reverse("blog_post", kwargs={"slug": first_post.slug}), secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'content="index, follow', html=False)
            self.assertNotContains(response, "noindex")
            self.assertContains(response, "/custom-print/")
            self.assertContains(response, "blog-article-primary-cta")
            self.assertContains(response, "article-top-cta")
            self.assertContains(response, "Швидкі переходи")
            self.assertContains(response, "article-final-cta")
            self.assertContains(response, '"about"', html=False)
            self.assertContains(response, '"mentions"', html=False)
            self.assertContains(response, '"potentialAction"', html=False)
            self.assertContains(response, '"@type": "OrderAction"', html=False)
            self.assertContains(response, "https://testserver/custom-print/", html=False)
            self.assertContains(response, f'property="og:image"\n    content="https://testserver{first_post.cover_image.url}"', html=False)
            self.assertContains(response, 'property="og:image:type" content="image/webp"', html=False)
            self.assertContains(response, 'property="og:image:width" content="1600"', html=False)
            self.assertContains(response, 'property="og:image:height" content="1000"', html=False)
            self.assertContains(response, f'name="twitter:image"\n    content="https://testserver{first_post.cover_image.url}"', html=False)
            self.assertNotContains(response, "Коротко для AI")
            self.assertNotContains(response, "Внутрішня навігація")

            ru_response = self.client.get(f"/ru/blog/{first_post.slug}/", secure=True)
            self.assertEqual(ru_response.status_code, 200)
            self.assertContains(ru_response, "/ru/custom-print/")
            self.assertContains(ru_response, "/ru/catalog/tshirts/")
            self.assertContains(ru_response, "/ru/catalog/tshirts/black/")
            self.assertContains(ru_response, "/ru/catalog/hoodie/black/")
            self.assertContains(ru_response, "Коротко: как это работает")
            self.assertContains(ru_response, "Быстрые переходы")
            self.assertNotContains(ru_response, "Коротко для AI")
            self.assertNotContains(ru_response, "Внутренняя навигация")

            ru_alias = self.client.get("/ru/blog/futbolka-so-svoim-dizaynom/", secure=True, follow=False)
            self.assertEqual(ru_alias.status_code, 301)
            self.assertEqual(ru_alias["Location"], "/ru/blog/futbolka-zi-svoim-dyzainom/")


@override_settings(COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False, CACHES=TEST_CACHES)
class BlogStructuredAdminTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(username="blog-admin", password="pass12345", is_staff=True)
        self.category = BlogCategory.objects.create(name="Огляди Test Admin", slug="reviews-admin-test")
        self.post = BlogPost.objects.create(
            category=self.category,
            title="Огляд худі",
            slug="hoodie-review-admin-test",
            excerpt="Огляд.",
            content_html="<p>Legacy.</p>",
            published_at=timezone.now(),
            is_published=True,
        )

    def test_admin_editor_exposes_unified_visual_canvas_and_block_modal(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("admin_blog_post_update", kwargs={"pk": self.post.pk}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-blog-block-editor")
        self.assertContains(response, "data-block-palette")
        self.assertContains(response, "data-media-upload-endpoint")
        self.assertContains(response, "data-block-visual-preview")
        self.assertContains(response, "data-block-modal")
        self.assertContains(response, "openBlockModal")
        self.assertContains(response, "renderInlinePreview")
        self.assertContains(response, "blog-editor-workbench")
        self.assertContains(response, "blog-editor-settings")
        self.assertNotContains(response, "Live preview")
        self.assertNotContains(response, "blog-builder-preview")
        self.assertContains(response, "SEO")
        self.assertContains(response, "45-60")

    def test_admin_create_editor_has_unsaved_server_preview(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("admin_blog_post_create"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-block-visual-preview")
        self.assertContains(response, "data-block-modal")
        self.assertNotContains(response, "data-render-preview")

    def test_category_editor_does_not_boot_post_block_composer(self):
        self.client.force_login(self.staff)

        response = self.client.get(reverse("admin_blog_category_create"), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-blog-block-editor")
        self.assertContains(response, "Нова категорія блогу")

    def test_admin_preview_endpoint_uses_server_renderer(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("admin_blog_post_preview", kwargs={"pk": self.post.pk}),
            data=json.dumps(
                {
                    "blocks": [
                        {
                            "type": "callout",
                            "payload": {
                                "tone": "info",
                                "title": {"uk": "Важливо"},
                                "body": {"uk": "Server-side preview."},
                            },
                        }
                    ]
                }
            ),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Server-side preview.")
        self.assertContains(response, "article-callout", html=False)

    def test_admin_preview_endpoint_supports_unsaved_posts(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("admin_blog_post_preview_new"),
            data=json.dumps(
                {
                    "blocks": [
                        {
                            "type": "cta_group",
                            "payload": {
                                "layout": "cards",
                                "buttons": [
                                    {
                                        "provider": "instagram",
                                        "label": {"uk": "Відкрити Instagram"},
                                        "url": "twocomms",
                                    }
                                ],
                            },
                        }
                    ]
                }
            ),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Відкрити Instagram")
        self.assertContains(response, "article-cta-panel", html=False)

    def test_admin_preview_handles_unsaved_promo_block(self):
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse("admin_blog_post_preview", kwargs={"pk": self.post.pk}),
            data=json.dumps(
                {
                    "blocks": [
                        {
                            "type": "promo_claim",
                            "payload": {
                                "campaign_id": "",
                                "title": {"uk": "Отримати промокод"},
                                "button_label": {"uk": "Забрати"},
                            },
                        }
                    ]
                }
            ),
            content_type="application/json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Отримати промокод")
        self.assertContains(response, "disabled")

    def test_admin_media_upload_creates_asset_for_image_blocks(self):
        self.client.force_login(self.staff)
        image = Image.new("RGB", (640, 400), "#f97316")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        upload = SimpleUploadedFile("article.jpg", buffer.getvalue(), content_type="image/jpeg")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse("admin_blog_media_upload"),
                {
                    "file": upload,
                    "alt_text": "Фото худі",
                    "caption_text": "Підпис під фото",
                    "credit_text": "TwoComms",
                },
                secure=True,
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["asset"]["width"], 640)
            self.assertEqual(payload["asset"]["height"], 400)
            asset = BlogMediaAsset.objects.get(pk=payload["asset"]["id"])
            self.assertEqual(asset.alt["uk"], "Фото худі")
            self.assertEqual(asset.caption["uk"], "Підпис під фото")
            self.assertGreater(asset.file.size, 0)


@override_settings(COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False, CACHES=TEST_CACHES)
class BlogPromoAndLockedContentTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.category = BlogCategory.objects.create(name="Акції", slug="promos")
        self.post = BlogPost.objects.create(
            category=self.category,
            title="Акція",
            slug="promo-post",
            excerpt="Промо.",
            content_html="",
            published_at=timezone.now(),
            is_published=True,
        )
        self.campaign = BlogPromoCampaign.objects.create(
            name="Blog launch",
            code_prefix="BLOG",
            discount_type="percentage",
            discount_value=Decimal("15.00"),
            max_claims=2,
            valid_days=14,
            is_active=True,
        )
        self.block = BlogPostBlock.objects.create(
            post=self.post,
            block_type=BlogPostBlock.BlockType.PROMO_CLAIM,
            sort_order=10,
            payload={
                "campaign_id": self.campaign.pk,
                "title": {"uk": "Отримати промокод"},
                "button_label": {"uk": "Забрати знижку"},
            },
        )

    def test_locked_content_is_not_sent_to_anonymous_users(self):
        BlogPostBlock.objects.create(
            post=self.post,
            block_type=BlogPostBlock.BlockType.LOCKED_CONTENT,
            sort_order=20,
            payload={
                "teaser": {"uk": "Увійдіть, щоб побачити бонус."},
                "html": {"uk": "<p>SECRET DISCOUNT DETAILS</p>"},
            },
        )

        response = self.client.get(reverse("blog_post", kwargs={"slug": self.post.slug}), secure=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Увійдіть, щоб побачити бонус")
        self.assertNotContains(response, "SECRET DISCOUNT DETAILS")

    def test_anonymous_promo_claim_does_not_create_code(self):
        response = self.client.post(
            reverse("blog_promo_claim", kwargs={"slug": self.post.slug, "block_id": self.block.pk}),
            secure=True,
            follow=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertEqual(PromoCode.objects.count(), 0)
        self.assertEqual(BlogPromoClaim.objects.count(), 0)

    def test_authenticated_promo_claim_generates_unique_code_once(self):
        user = User.objects.create_user(username="reader", password="pass12345")
        self.client.force_login(user)

        first = self.client.post(
            reverse("blog_promo_claim", kwargs={"slug": self.post.slug, "block_id": self.block.pk}),
            secure=True,
            follow=True,
        )
        second = self.client.post(
            reverse("blog_promo_claim", kwargs={"slug": self.post.slug, "block_id": self.block.pk}),
            secure=True,
            follow=True,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(PromoCode.objects.count(), 1)
        self.assertEqual(BlogPromoClaim.objects.count(), 1)
        claim = BlogPromoClaim.objects.get()
        self.assertEqual(claim.user, user)
        self.assertEqual(claim.post, self.post)
        self.assertEqual(claim.block, self.block)
        self.assertTrue(claim.promo_code.code.startswith("BLOG"))
        self.assertTrue(UserPromoCode.objects.filter(user=user, promo_code=claim.promo_code).exists())
