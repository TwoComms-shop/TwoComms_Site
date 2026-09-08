import json

from django.core.management.base import BaseCommand, CommandError

from management.services.ig_journey_trace_generation import rebuild_journey_traces


class Command(BaseCommand):
    help = "Перевірити до 6 явно вибраних діалогів; --apply створює лише інтерпретації маршруту."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", type=int, action="append", required=True,
                            help="Exact client ID; repeat for up to six clients.")
        parser.add_argument("--apply", action="store_true", help="Call the existing analysis provider and save immutable trace artifacts.")
        parser.add_argument("--allow-historical", action="store_true",
                            help="With --apply, authorize this bounded run despite the background backfill toggle; key mapping and provider limits still apply.")

    def handle(self, *args, **options):
        try:
            report = rebuild_journey_traces(options["client_id"], apply=options["apply"], allow_historical=options["allow_historical"])
        except ValueError:
            raise CommandError("Provide one to six distinct positive --client-id values; --allow-historical requires --apply.") from None
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
