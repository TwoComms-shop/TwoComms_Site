import json

from django.core.management.base import BaseCommand

from management.services.bot_conversation_analysis import (
    process_due_analysis,
    reconcile_analysis_jobs,
)


class Command(BaseCommand):
    help = "Ставить у чергу змінені/застарілі аналізи IG-діалогів без customer send."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--run-due", action="store_true")

    def handle(self, *args, **options):
        result = reconcile_analysis_jobs(limit=options["limit"])
        if options["run_due"]:
            result["processed"] = process_due_analysis(limit=1)
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
