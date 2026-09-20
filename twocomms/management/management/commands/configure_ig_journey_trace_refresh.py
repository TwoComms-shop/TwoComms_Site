"""Explicit audited activation of bounded automatic journey interpretation."""
import json

from django.core.management.base import BaseCommand, CommandError
from management.models import IgJourneyTraceRefreshControl
from management.services.ig_journey_trace_refresh import configure_refresh


class Command(BaseCommand):
    help = "Read status or explicitly enable/disable bounded journey trace refresh."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--enable", action="store_true")
        mode.add_argument("--disable", action="store_true")
        parser.add_argument("--actor-id", type=int)
        parser.add_argument("--max-starts-per-hour", type=int)

    def handle(self, *args, **options):
        mutation = options["enable"] or options["disable"]
        if not mutation:
            if options["actor_id"] is not None or options["max_starts_per_hour"] is not None:
                raise CommandError("An explicit --enable or --disable is required for configuration.")
            row = IgJourneyTraceRefreshControl.objects.filter(pk=1).values(
                "enabled", "activation_watermark", "scan_cursor", "max_starts_per_hour",
                "budget_starts", "changed_by_id", "activated_at", "changed_at").first()
            result = row or {"enabled": False, "status": "not_configured"}
        else:
            try:
                result = configure_refresh(enabled=bool(options["enable"]), actor_id=options["actor_id"],
                    max_starts_per_hour=options["max_starts_per_hour"])
            except ValueError as exc:
                raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, sort_keys=True, default=str))
