import json

from django.core.management.base import BaseCommand, CommandError

from management.services.ig_conversion_publication import publish_reviewed_owner_scenarios
from management.services.ig_policy_publication import PolicyPublicationError


class Command(BaseCommand):
    help = "Review/publish only the five owner-approved scenarios, preserving unrelated drafts."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expected-draft-hash", default="")
        parser.add_argument("--expected-head-hash", default="")

    def handle(self, *args, **options):
        try:
            result = publish_reviewed_owner_scenarios(apply=options["apply"],
                expected_draft_hash=options["expected_draft_hash"], expected_head_hash=options["expected_head_hash"])
        except PolicyPublicationError as exc:
            raise CommandError(f"{exc.code}: {exc}") from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
