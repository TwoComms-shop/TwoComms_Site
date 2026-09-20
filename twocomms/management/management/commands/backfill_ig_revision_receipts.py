from django.core.management.base import BaseCommand, CommandError

from management.services.ig_revision_receipt_backfill import inspect_historical_receipts


class Command(BaseCommand):
    help = "Audit old confirmed Instagram revision receipts; apply only explicit revision IDs."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Persist projections for explicitly listed revisions.")
        parser.add_argument("--revision-id", action="append", type=int, default=[], help="Revision ID to inspect/apply; repeatable.")
        parser.add_argument("--limit", type=int, default=1000, help="Maximum rows to inspect.")

    def handle(self, *args, **options):
        revision_ids = tuple(options["revision_id"] or ())
        if options["apply"] and not revision_ids:
            raise CommandError("--apply requires at least one --revision-id")
        result = inspect_historical_receipts(
            revision_ids=revision_ids,
            limit=max(1, options["limit"]),
            apply=bool(options["apply"]),
        )
        self.stdout.write(self.style.SUCCESS(str(result)))
