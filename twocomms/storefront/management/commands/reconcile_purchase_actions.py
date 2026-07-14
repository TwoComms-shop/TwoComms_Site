from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from orders.models import Order
from storefront.models import UserAction
from storefront.utm_tracking import (
    CONFIRMED_PURCHASE_STATUSES,
    ensure_order_purchase_action,
)


MONOBANK_SUCCESS_STATUSES = frozenset({'success', 'hold'})


def _trusted_paid_orders():
    """Orders whose stored state is sufficient to restore internal analytics.

    Historical staff-created rows are intentionally excluded: old ``free``
    gifts and genuinely paid manual sales cannot be distinguished reliably.
    New manual/IG/admin paths write purchase actions at confirmation time.
    """
    monobank_evidence = (
        Q(payment_provider__startswith='monobank')
        & Q(payment_invoice_id__isnull=False)
        & ~Q(payment_invoice_id='')
    )
    return (
        Order.objects.filter(payment_status__in=CONFIRMED_PURCHASE_STATUSES)
        .filter(Q(source='web') | monobank_evidence)
        .exclude(
            Q(payment_payload__has_key='manual_payment_preset')
            & Q(payment_payload__manual_payment_preset='free')
        )
        .order_by('pk')
    )


def _coerce_event_time(value):
    if not value:
        return None
    parsed = value if hasattr(value, 'tzinfo') else parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return min(parsed, timezone.now())


def _purchase_occurred_at(order):
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    history = payload.get('history')
    success_times = []
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            data = entry.get('data') if isinstance(entry.get('data'), dict) else {}
            status = str(entry.get('status') or data.get('status') or '').lower()
            if status not in MONOBANK_SUCCESS_STATUSES:
                continue
            for key in ('received_at', 'ts', 'timestamp'):
                occurred_at = _coerce_event_time(entry.get(key))
                if occurred_at is not None:
                    success_times.append(occurred_at)
                    break
    if success_times:
        return min(success_times)

    if order.status == 'done' and order.shipment_status_updated:
        return _coerce_event_time(order.shipment_status_updated)
    return _coerce_event_time(order.created) or timezone.now()


class Command(BaseCommand):
    help = (
        'Dry-run or restore missing internal UserAction purchase rows for '
        'trusted historical web/Monobank orders. No external events are sent.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist missing purchase actions. Default is read-only dry-run.',
        )

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        candidates = _trusted_paid_orders()
        purchase_order_ids = UserAction.objects.filter(
            action_type='purchase',
            order_id__isnull=False,
        ).values('order_id')
        missing_candidates = candidates.exclude(pk__in=purchase_order_ids)
        eligible_count = candidates.count()
        missing_count = missing_candidates.count()

        if not apply_changes:
            self.stdout.write(
                f'eligible={eligible_count} missing={missing_count} '
                'created=0 dry_run=True'
            )
            return

        created_count = 0
        with transaction.atomic():
            # Existing purchase rows are intentionally left byte-for-byte
            # untouched. This command repairs only the proven gap and must not
            # rewrite attribution metadata from live webhook/delivery paths.
            locked_orders = missing_candidates.select_for_update()
            for order in locked_orders.iterator(chunk_size=100):
                existed = UserAction.objects.filter(
                    action_type='purchase',
                    order_id=order.pk,
                ).exists()
                occurred_at = _purchase_occurred_at(order)
                action = ensure_order_purchase_action(
                    order,
                    metadata={
                        'source': 'purchase_reconciliation',
                        'reconciled': True,
                    },
                    occurred_at=occurred_at,
                    raise_errors=True,
                )
                if action is None:
                    raise CommandError(
                        f'Could not ensure purchase action for eligible order {order.pk}'
                    )
                if not existed:
                    created_count += 1

        missing_after = _trusted_paid_orders().exclude(
            pk__in=UserAction.objects.filter(
                action_type='purchase',
                order_id__isnull=False,
            ).values('order_id')
        ).count()
        self.stdout.write(
            f'eligible={eligible_count} missing={missing_after} '
            f'created={created_count} dry_run=False'
        )
