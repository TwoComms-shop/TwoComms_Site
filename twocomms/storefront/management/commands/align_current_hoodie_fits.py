import json

from django.core.management.base import BaseCommand, CommandError

from storefront.services.current_hoodie_fits import HoodieFitAlignmentError, align_current_hoodie_fits


class Command(BaseCommand):
    help = "Dry-run or CAS-apply the reviewed visibility/default flags for current hoodie fits only."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        parser.add_argument("--expected-snapshot-hash", default="")

    def handle(self, *args, **options):
        try:
            result = align_current_hoodie_fits(apply=options["apply"],
                expected_snapshot_hash=options["expected_snapshot_hash"])
        except HoodieFitAlignmentError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
