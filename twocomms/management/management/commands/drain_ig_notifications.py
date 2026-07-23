from django.core.management.base import BaseCommand

from management.services.instagram_bot import drain_manager_notifications


class Command(BaseCommand):
    help = "Надіслати належні до повтору сповіщення Instagram CRM з outbox."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"]), 500))
        sent = drain_manager_notifications(limit=limit)
        self.stdout.write(f"Надіслано: {sent}; перевірено не більше: {limit}")
