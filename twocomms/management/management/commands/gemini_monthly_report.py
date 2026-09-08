"""Print a sanitized, read-only monthly Gemini observation report."""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from management.services.gemini_monthly_report import build_monthly_payload


class Command(BaseCommand):
    help = "Print a sanitized read-only Gemini attempt aggregate for 1 to 31 UTC days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, **options):
        try:
            payload = build_monthly_payload(days=options["days"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
