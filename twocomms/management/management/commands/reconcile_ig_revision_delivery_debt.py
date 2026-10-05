"""Inventory or assign operator ownership to conclusive incomplete replies."""
import json

from django.core.management.base import BaseCommand

from management.services.ig_revision_execution import reconcile_incomplete_revision_deliveries


class Command(BaseCommand):
    help = "Inspect incomplete revision delivery; --apply links existing operator cases without sending messages."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        result = reconcile_incomplete_revision_deliveries(
            limit=options["limit"], dry_run=not options["apply"],
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
