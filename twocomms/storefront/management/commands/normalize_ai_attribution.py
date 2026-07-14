from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from orders.models import Order
from storefront.models import SiteSession, UTMSession
from storefront.utm_utils import (
    detect_ai_source,
    normalize_first_touch_attribution,
    normalize_utm_attribution,
    normalize_utm_source,
)


CANONICAL_SOURCE = 'chatgpt'


def _is_chatgpt_source(value):
    return normalize_utm_source(value) == CANONICAL_SOURCE


def _normalized_utm_values(source, medium):
    if not _is_chatgpt_source(source):
        return None
    normalized = normalize_utm_attribution(source, medium)
    current_medium = str(medium).strip() if medium is not None else None
    current_medium = current_medium or None
    if normalized == (source, current_medium):
        return None
    return normalized


def _normalized_first_touch(data):
    if not isinstance(data, dict):
        return None
    raw_source = data.get('utm_source')
    detected_source = detect_ai_source(data.get('referrer'))
    if not _is_chatgpt_source(raw_source) and not (
        not raw_source and detected_source == CANONICAL_SOURCE
    ):
        return None
    normalized = normalize_first_touch_attribution(data)
    return normalized if normalized != data else None


def _collect_plan():
    utm_updates = {}
    for item in UTMSession.objects.filter(utm_source__isnull=False).only(
        'id', 'utm_source', 'utm_medium'
    ).iterator(chunk_size=500):
        normalized = _normalized_utm_values(item.utm_source, item.utm_medium)
        if normalized is not None:
            utm_updates[item.pk] = normalized

    site_updates = {}
    for item in SiteSession.objects.exclude(first_touch_data={}).only(
        'id', 'first_touch_data'
    ).iterator(chunk_size=500):
        normalized = _normalized_first_touch(item.first_touch_data)
        if normalized is not None:
            site_updates[item.pk] = normalized

    order_updates = {}
    for item in Order.objects.filter(utm_source__isnull=False).only(
        'id', 'utm_session_id', 'utm_source', 'utm_medium'
    ).iterator(chunk_size=500):
        normalized = _normalized_utm_values(item.utm_source, item.utm_medium)
        if normalized is not None:
            order_updates[item.pk] = normalized

    conflicts = []
    linked_order_ids = []
    if utm_updates:
        linked_orders = Order.objects.filter(utm_session_id__in=utm_updates).only(
            'id', 'utm_session_id', 'utm_source', 'utm_medium'
        )
        for order in linked_orders.iterator(chunk_size=500):
            linked_order_ids.append(order.pk)
            if not order.utm_source:
                continue
            canonical_order_attribution = normalize_utm_attribution(
                order.utm_source,
                order.utm_medium,
            )
            target_attribution = utm_updates[order.utm_session_id]
            if canonical_order_attribution != target_attribution:
                conflicts.append(order.pk)

    return {
        'utm_updates': utm_updates,
        'site_updates': site_updates,
        'order_updates': order_updates,
        'conflicts': conflicts,
        'linked_order_ids': linked_order_ids,
    }


def _plan_counts(plan):
    return {
        'utm_sessions': len(plan['utm_updates']),
        'site_sessions': len(plan['site_updates']),
        'orders': len(plan['order_updates']),
        'conflicts': len(plan['conflicts']),
    }


def _validate_locked_plan(plan, utm_rows, site_rows, order_rows):
    """Ensure no value changed between the read-only plan and row locks."""
    stale_rows = []

    for pk, target in plan['utm_updates'].items():
        row = utm_rows.get(pk)
        current_target = (
            _normalized_utm_values(row.utm_source, row.utm_medium)
            if row is not None
            else None
        )
        if current_target != target:
            stale_rows.append(f'utm:{pk}')

    for pk, target in plan['site_updates'].items():
        row = site_rows.get(pk)
        current_target = (
            _normalized_first_touch(row.first_touch_data)
            if row is not None
            else None
        )
        if current_target != target:
            stale_rows.append(f'site:{pk}')

    for pk, target in plan['order_updates'].items():
        row = order_rows.get(pk)
        current_target = (
            _normalized_utm_values(row.utm_source, row.utm_medium)
            if row is not None
            else None
        )
        if current_target != target:
            stale_rows.append(f'order:{pk}')

    locked_conflicts = []
    for pk in plan['linked_order_ids']:
        order = order_rows.get(pk)
        if order is None:
            stale_rows.append(f'linked-order:{pk}')
            continue
        if not order.utm_source:
            continue
        target_attribution = plan['utm_updates'].get(order.utm_session_id)
        current_order_attribution = normalize_utm_attribution(
            order.utm_source,
            order.utm_medium,
        )
        if target_attribution is None or current_order_attribution != target_attribution:
            locked_conflicts.append(pk)

    if stale_rows:
        raise CommandError(
            'Candidate values changed while locking; aborting: ' + ', '.join(stale_rows)
        )
    if set(locked_conflicts) != set(plan['conflicts']):
        raise CommandError('Linked order conflicts changed while locking; aborting')


class Command(BaseCommand):
    help = (
        'Dry-run or normalize ChatGPT attribution across UTMSession, '
        'SiteSession first-touch data, and denormalized Order fields.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist the guarded normalization. Default is read-only.',
        )
        parser.add_argument('--expect-utm-sessions', type=int)
        parser.add_argument('--expect-site-sessions', type=int)
        parser.add_argument('--expect-orders', type=int)

    def _write_plan(self, counts, *, dry_run, updated=None):
        parts = [
            f"utm_sessions={counts['utm_sessions']}",
            f"site_sessions={counts['site_sessions']}",
            f"orders={counts['orders']}",
            f"conflicts={counts['conflicts']}",
        ]
        if updated is not None:
            parts.extend(
                [
                    f"updated_utm_sessions={updated['utm_sessions']}",
                    f"updated_site_sessions={updated['site_sessions']}",
                    f"updated_orders={updated['orders']}",
                ]
            )
        parts.append(f'dry_run={dry_run}')
        self.stdout.write(' '.join(parts))

    def _validate_expected_counts(self, counts, options):
        expected_options = {
            'utm_sessions': 'expect_utm_sessions',
            'site_sessions': 'expect_site_sessions',
            'orders': 'expect_orders',
        }
        missing = [
            f"--{option_name.replace('_', '-')}"
            for option_name in expected_options.values()
            if options.get(option_name) is None
        ]
        if missing:
            raise CommandError(
                'Apply requires exact dry-run guards: ' + ', '.join(missing)
            )
        mismatches = [
            f"{label}: expected {options[option_name]}, found {counts[label]}"
            for label, option_name in expected_options.items()
            if options[option_name] != counts[label]
        ]
        if mismatches:
            raise CommandError('Candidate counts changed; aborting: ' + '; '.join(mismatches))
        if counts['conflicts']:
            raise CommandError(
                f"Found {counts['conflicts']} linked order attribution conflicts; aborting"
            )

    def handle(self, *args, **options):
        if not options['apply']:
            counts = _plan_counts(_collect_plan())
            self._write_plan(counts, dry_run=True)
            return

        with transaction.atomic():
            plan = _collect_plan()
            counts = _plan_counts(plan)
            self._validate_expected_counts(counts, options)

            utm_rows = UTMSession.objects.select_for_update().in_bulk(plan['utm_updates'])
            site_rows = SiteSession.objects.select_for_update().in_bulk(plan['site_updates'])
            order_ids = set(plan['order_updates']) | set(plan['linked_order_ids'])
            order_rows = Order.objects.select_for_update().in_bulk(order_ids)

            if (
                len(utm_rows) != counts['utm_sessions']
                or len(site_rows) != counts['site_sessions']
                or not set(plan['order_updates']).issubset(order_rows)
            ):
                raise CommandError('Candidate rows changed while locking; aborting')
            _validate_locked_plan(plan, utm_rows, site_rows, order_rows)

            for pk, (source, medium) in plan['utm_updates'].items():
                row = utm_rows[pk]
                row.utm_source = source
                row.utm_medium = medium
                row.save(update_fields=['utm_source', 'utm_medium'])

            for pk, first_touch_data in plan['site_updates'].items():
                row = site_rows[pk]
                row.first_touch_data = first_touch_data
                row.save(update_fields=['first_touch_data'])

            for pk, (source, medium) in plan['order_updates'].items():
                row = order_rows[pk]
                row.utm_source = source
                row.utm_medium = medium
                row.save(update_fields=['utm_source', 'utm_medium'])

            remaining = _plan_counts(_collect_plan())
            if any(remaining.values()):
                raise CommandError(
                    'Normalization did not converge to a clean state; rolling back: '
                    + ', '.join(f'{key}={value}' for key, value in remaining.items())
                )

        self._write_plan(remaining, dry_run=False, updated=counts)
