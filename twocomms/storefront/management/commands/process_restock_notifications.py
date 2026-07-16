from django.core.management.base import BaseCommand, CommandError

from storefront.services.restock import (
    claim_due_subscription,
    due_subscriptions_queryset,
    finalize_delivery,
    recover_stale_sending,
    send_claimed_subscription,
    subscription_is_available,
)


class Command(BaseCommand):
    help = (
        "Process due Telegram/email restock deliveries. The queue provides "
        "at-least-once delivery; stale claims are retried after 15 minutes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--product-id", type=int)
        parser.add_argument("--variant-id", type=int)
        parser.add_argument("--subscription-id", type=int)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1:
            raise CommandError("--limit must be greater than zero")
        filters = {
            "product_id": options.get("product_id"),
            "variant_id": options.get("variant_id"),
            "subscription_id": options.get("subscription_id"),
        }
        if options["dry_run"]:
            due = due_subscriptions_queryset(**filters).select_related(
                "product", "color_variant"
            )[:limit]
            available = sum(1 for row in due if subscription_is_available(row))
            self.stdout.write(
                f"dry-run: {available} available automatic delivery(s) would be claimed"
            )
            return

        recovered = recover_stale_sending(**filters)
        processed = 0
        delivered = 0
        failed = 0
        while processed < limit:
            subscription = claim_due_subscription(**filters)
            if subscription is None:
                break
            processed += 1
            token = subscription.delivery_token
            try:
                success = bool(send_claimed_subscription(subscription))
                error = "" if success else "Delivery provider returned false"
            except Exception as exc:
                success = False
                error = str(exc)
            if finalize_delivery(
                subscription.pk,
                token,
                success=success,
                error=error,
            ):
                if success:
                    delivered += 1
                else:
                    failed += 1

        self.stdout.write(self.style.SUCCESS(
            f"restock: recovered={recovered} processed={processed} "
            f"delivered={delivered} failed={failed}"
        ))
