import json

from django.core.management.base import BaseCommand

from management.services.bot_payment_truth import payment_truth_inconsistency_report


class Command(BaseCommand):
    help = "Read-only audit of Instagram CRM payment/order truth inconsistencies"

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        report = payment_truth_inconsistency_report(sample_limit=options["limit"])
        if options["as_json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return

        self.stdout.write("Instagram CRM payment truth audit (read-only)")
        self.stdout.write(f"Schema: {report['schema_version']}")
        self.stdout.write(f"Findings: {report['finding_count']}")
        for name, count in report["counts"].items():
            sample = ",".join(str(item) for item in report["samples"][name]) or "-"
            self.stdout.write(f"{name}: {count}; sample IDs: {sample}")
