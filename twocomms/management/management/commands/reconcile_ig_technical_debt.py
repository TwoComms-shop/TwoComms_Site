import json

from django.core.management.base import BaseCommand

from management.services.ig_technical_debt import reconcile_ig_technical_debt_once


class Command(BaseCommand):
    help = "Inventory Instagram technical debt; persist operator cases only with --apply."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true", help="Persist bounded observations as operator cases.")
        parser.add_argument("--json", action="store_true", help="Accepted for stable machine-readable output.")

    def handle(self, *args, **options):
        result = reconcile_ig_technical_debt_once(
            limit=options["limit"], dry_run=not options["apply"],
        )
        # JSON is the default so cron/monitoring consumers do not need to
        # parse presentation text. ``--json`` remains an explicit contract.
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
