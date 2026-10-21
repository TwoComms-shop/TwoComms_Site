"""Explicit private operator draft; never sends a customer message."""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Prepare one private Gemini operator reply draft. Default dry-run performs no DB/provider I/O."
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument("--client-id", type=int, required=True)
        parser.add_argument("--source-id", type=int, required=True)
        parser.add_argument("--actor-id", type=int, required=True)
        parser.add_argument("--permission-epoch", type=int, required=True)
        parser.add_argument("--operation-id", required=True)
        parser.add_argument("--allow-delay-apology", action="store_true",
            help="Operator authorizes a truthful apology only if this old wait is verified unanswered.")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        from types import SimpleNamespace
        from management.services.ig_human_reply import HumanReplyRejected
        from management.services.ig_operator_reply_drafts import prepare_operator_reply_draft
        try:
            result = prepare_operator_reply_draft(client_id=options["client_id"], source_id=options["source_id"],
                actor=SimpleNamespace(pk=options["actor_id"]), expected_permission_epoch=options["permission_epoch"],
                operation_id=options["operation_id"], apply=options["apply"],
                allow_delay_apology=options["allow_delay_apology"])
        except HumanReplyRejected as error:
            raise CommandError(error.code) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
        if result["state"] == "failed":
            raise CommandError(result["reason"])
