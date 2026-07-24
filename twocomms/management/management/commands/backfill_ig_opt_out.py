import json

from django.core.management.base import BaseCommand, CommandError

from management.services.ig_maintenance import maintenance_status
from management.services.ig_opt_out_backfill import reconcile_opt_out_backfill


class Command(BaseCommand):
    help = "Bounded, deterministic backfill of legacy Instagram opt-out truth."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        if not dry_run and not maintenance_status().get("active"):
            raise CommandError("writes require an active Instagram maintenance lease")
        result = reconcile_opt_out_backfill(
            limit=options["limit"],
            dry_run=dry_run,
        )
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
