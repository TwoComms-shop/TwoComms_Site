from django.core.management.base import BaseCommand

from management.services.ig_technical_debt import reconcile_ig_technical_debt_once


class Command(BaseCommand):
    help = "Inventory Instagram technical debt; persist operator cases only with --apply."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true", help="Persist bounded observations as operator cases.")

    def handle(self, *args, **options):
        result = reconcile_ig_technical_debt_once(
            limit=options["limit"], dry_run=not options["apply"],
        )
        self.stdout.write(self.style.SUCCESS(str(result)))
