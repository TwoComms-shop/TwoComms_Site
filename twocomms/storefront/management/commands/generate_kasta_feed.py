from pathlib import Path

from django.core.management.base import BaseCommand

from storefront.services.marketplace_feeds import build_kasta_feed_xml, build_profile_offers, iter_feed_offers, resolve_base_url
from storefront.services.feed_registry import get_system_feed


class Command(BaseCommand):
    help = "Генерирует XML/YML фид для Kasta"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            default="media/kasta-feed.xml",
            help="Путь к выходному XML файлу",
        )
        parser.add_argument(
            "--base-url",
            type=str,
            default=None,
            help="Базовый URL сайта для ссылок в фиде",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Показать количество офферов без записи файла",
        )

    def handle(self, *args, **options):
        base_url = resolve_base_url(options["base_url"])
        feed = get_system_feed("kasta")
        offers = iter_feed_offers(base_url) if feed is None else build_profile_offers(feed, base_url)

        if options["dry_run"]:
            self.stdout.write(
                self.style.SUCCESS(f"Kasta feed готов к генерации: {len(offers)} офферов, base_url={base_url}")
            )
            return

        output_path = Path(options["output"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(build_kasta_feed_xml(base_url=base_url, feed=feed))

        self.stdout.write(self.style.SUCCESS(f"Kasta фид создан: {output_path} ({len(offers)} офферов)"))
