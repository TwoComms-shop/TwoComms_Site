from django.test import TestCase, override_settings

from storefront.models import BlogPost
from storefront.services.blog_blocks import render_post_blocks


@override_settings(COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False)
class VeteranFundStageThreeReportTests(TestCase):
    def test_stage_three_report_renders_verified_media_and_progress(self):
        post = BlogPost.objects.get(slug="twocomms-veteran-fund-stage-three")
        html, _schema = render_post_blocks(post)

        self.assertIn("Третій із чотирьох етапів", html)
        self.assertIn("3/4", html)
        self.assertIn("Варто більше", html)
        self.assertIn("7K2dkjNg3cQ", html)
        self.assertIn("Радіо Накипіло", html)
        self.assertIn("Український ветеранський фонд", html)
        self.assertIn("article-video-lite", html)
        self.assertIn("article-source-card", html)
