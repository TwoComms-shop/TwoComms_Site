"""Explicit, bounded source observation replay; previews never mutate/call AI."""
import json

from django.core.management.base import BaseCommand, CommandError

from management.models import InstagramBotMessage
from management.services.ig_payment_observation import (
    _namespace, _source_digest, _source_reason, observe_payment_source,
)


class Command(BaseCommand):
    help = "Preview current accepted commerce/payment sources; apply only with --apply."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", type=int)
        parser.add_argument("--message-id", type=int)
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--allow-provider", action="store_true")

    def handle(self, *args, **options):
        if not options["client_id"] and not options["message_id"]:
            raise CommandError("Specify --client-id or --message-id; whole-history replay is disabled.")
        if not 1 <= options["limit"] <= 50:
            raise CommandError("--limit must be between 1 and 50.")
        if options["allow_provider"] and not options["apply"]:
            raise CommandError("--allow-provider requires --apply; preview is provider-free.")
        query = InstagramBotMessage.objects.select_related("client").filter(role__in=("user", "manager"))
        if options["client_id"]:
            query = query.filter(client_id=options["client_id"])
        if options["message_id"]:
            query = query.filter(pk=options["message_id"])
        rows = list(query.order_by("-pk")[:options["limit"]])
        results = []
        for source in reversed(rows):
            namespace = _namespace(source)
            reason = _source_reason(source.client, source, namespace)
            result = {"message_id": source.pk, "client_id": source.client_id,
                "role": source.role, "eligible": not reason, "reason": reason,
                "source_digest": _source_digest(source, namespace)}
            if options["apply"] and not reason:
                observed = observe_payment_source(source.pk, allow_provider=options["allow_provider"])
                result.update(observed=observed.observed, reason=observed.reason)
            results.append(result)
        self.stdout.write(json.dumps({"dry_run": not options["apply"],
            "allow_provider": options["allow_provider"], "results": results}, sort_keys=True))
