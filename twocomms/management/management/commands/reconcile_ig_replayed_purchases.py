"""Repair saved claim replays without creating orders or contacting providers."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from management.ig_bot_models import IgClient, IgPaymentConfirmationReview
from management.services.bot_payment_truth import recalculate_client_payment_aggregates
from management.services.ig_commercial_episodes import commercial_episode_client_lock
from management.services.ig_payment_review import (
    payment_review_duplicate_identity,
    reconcile_duplicate_payment_review,
)


class Command(BaseCommand):
    help = "Preview/apply bounded pending payment replays with a saved exact claim anchor."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--client-id", type=int)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("--apply and --dry-run are mutually exclusive.")
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("--limit must be between 1 and 1000.")
        reviews = IgPaymentConfirmationReview.objects.filter(
            status=IgPaymentConfirmationReview.Status.PENDING,
            evidence__has_key="claim_anchor",
        ).order_by("pk")
        if options["client_id"]:
            reviews = reviews.filter(client_id=options["client_id"])
        result = {"mode": "apply" if options["apply"] else "dry-run",
                  "scanned": 0, "repairs": [], "skipped": []}
        for review in reviews[:options["limit"]]:
            result["scanned"] += 1
            # URL, amount, timestamps and customer identity never authorize a
            # repair. Only the immutable v2 anchor recorded at claim creation.
            if not payment_review_duplicate_identity(review).startswith("claim:"):
                result["skipped"].append({"review_id": review.pk, "reason": "no_saved_claim"})
                continue
            with commercial_episode_client_lock(review.client_id), transaction.atomic():
                client = IgClient.objects.select_for_update().get(pk=review.client_id)
                canonical = reconcile_duplicate_payment_review(review, dry_run=True)
                if not canonical or canonical.status != IgPaymentConfirmationReview.Status.CONFIRMED:
                    result["skipped"].append({"review_id": review.pk, "reason": "no_confirmed_canonical"})
                    continue
                row = {"client_id": client.pk, "review_id": review.pk,
                       "canonical_review_id": canonical.pk}
                if options["apply"]:
                    repaired = reconcile_duplicate_payment_review(review)
                    if not repaired or repaired.pk != canonical.pk:
                        result["skipped"].append({"review_id": review.pk, "reason": "source_changed"})
                        continue
                    recalculate_client_payment_aggregates(client)
                    row.update(purchases_count=client.purchases_count,
                               total_spent=str(client.total_spent))
                result["repairs"].append(row)
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
