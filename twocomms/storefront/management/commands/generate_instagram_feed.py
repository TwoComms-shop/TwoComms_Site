"""Generate the Instagram / Meta snapshot through the shared feed adapter."""

from pathlib import Path

from django.core.management.base import BaseCommand

from storefront.services.feed_registry import get_system_feed
from storefront.services.marketplace_feeds import (
    build_meta_catalog_feed_xml,
    build_profile_offers,
    iter_feed_offers,
    resolve_base_url,
)


class Command(BaseCommand):
    help = "Генерує XML фід для Instagram / Meta Commerce Platform"

    def add_arguments(self, parser):
        parser.add_argument("--output", default="media/instagram-feed.xml")
        parser.add_argument("--base-url", default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--log-skipped", action="store_true")

    def handle(self, *args, **options):
        base_url = resolve_base_url(options.get("base_url"))
        feed = get_system_feed("meta")
        offers = iter_feed_offers(base_url) if feed is None else build_profile_offers(feed, base_url)
        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS(f"Meta фід готовий: {len(offers)} оферів"))
            return
        output_path = Path(options["output"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(build_meta_catalog_feed_xml(base_url=base_url, feed=feed))
        self.stdout.write(self.style.SUCCESS(f"Instagram/Meta фід створено: {output_path} ({len(offers)} оферів)"))
