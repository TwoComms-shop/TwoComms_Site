import json

from django.core.management.base import BaseCommand, CommandError

from management.services.bot_conversation_analysis import (
    process_due_analysis,
    report_failed_analysis_jobs,
    reconcile_analysis_jobs,
)
from management.services.ig_analysis_events import process_due_analysis_events


class Command(BaseCommand):
    help = "Ставить у чергу змінені/застарілі аналізи IG-діалогів без customer send."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--run-due", action="store_true")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только отчёт failed jobs; не меняет cursor/status и не вызывает провайдеры.",
        )
        parser.add_argument(
            "--report-failed",
            action="store_true",
            help="Alias для безопасного dry-run отчёта failed jobs.",
        )
        parser.add_argument(
            "--quota-budget",
            type=int,
            default=0,
            help="Максимум ID, отмеченных для адресного retry в отчёте; ничего не ретраит.",
        )

    def handle(self, *args, **options):
        if options["quota_budget"] and not (
            options["dry_run"] or options["report_failed"]
        ):
            raise CommandError("--quota-budget требует --dry-run или --report-failed")
        if (options["dry_run"] or options["report_failed"]) and options["run_due"]:
            raise CommandError("--dry-run/--report-failed нельзя совмещать с --run-due")
        if options["dry_run"] or options["report_failed"]:
            result = report_failed_analysis_jobs(
                limit=options["limit"],
                quota_budget=options["quota_budget"],
            )
            self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return
        if options["run_due"]:
            # This command is a separate process. The runtime manifest assigns
            # analysis reconciliation and both consumers to the daemon owner;
            # never mutate the shared cursor or queue before proving ownership.
            self.stdout.write(json.dumps({
                "mode": "run_due",
                "processed": {"deferred": "daemon_owner"},
                "processed_events": {"deferred": "daemon_owner"},
                "reason": "owner_busy_or_unverified",
            }, ensure_ascii=False, sort_keys=True))
            return
        result = reconcile_analysis_jobs(limit=options["limit"])
        from management.services.ig_typed_memory import reconcile_typed_memory

        result["typed_memory"] = reconcile_typed_memory(limit=options["limit"])
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
