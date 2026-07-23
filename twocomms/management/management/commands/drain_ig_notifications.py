from django.core.management.base import BaseCommand, CommandError

from management.services.instagram_bot import drain_manager_notifications
from management.services.ig_maintenance import maintenance_status


class Command(BaseCommand):
    help = "Надіслати належні до повтору сповіщення Instagram CRM з outbox."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        if maintenance_status()["active"]:
            raise CommandError("maintenance active — notification drain refused")
        limit = max(1, min(int(options["limit"]), 500))
        sent = drain_manager_notifications(limit=limit)
        self.stdout.write(f"Надіслано: {sent}; перевірено не більше: {limit}")
