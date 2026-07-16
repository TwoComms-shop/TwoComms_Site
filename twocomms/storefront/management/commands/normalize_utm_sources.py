"""Normalize historical UTM source aliases without inventing attribution."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from orders.models import Order
from storefront.models import SiteSession, UTMSession
from storefront.utm_utils import normalize_utm_source


def _normalized_source(value):
    if not value:
        return None
    normalized = normalize_utm_source(value)
    return normalized if normalized and normalized != value else None


def _normalized_first_touch(data):
    if not isinstance(data, dict):
        return None
    normalized = _normalized_source(data.get('utm_source'))
    if normalized is None:
        return None
    result = dict(data)
    result['utm_source'] = normalized
    return result


def _collect_plan(*, lock=False):
    utm_queryset = UTMSession.objects.filter(utm_source__isnull=False).only(
        'id', 'utm_source'
    ).order_by('pk')
    site_queryset = SiteSession.objects.exclude(first_touch_data={}).only(
        'id', 'first_touch_data'
    ).order_by('pk')
    order_queryset = Order.objects.filter(utm_source__isnull=False).only(
        'id', 'utm_session_id', 'utm_source'
    ).order_by('pk')
    if lock:
        utm_queryset = utm_queryset.select_for_update()
        site_queryset = site_queryset.select_for_update()
        order_queryset = order_queryset.select_for_update()

    utm_updates = {}
    for row in utm_queryset.iterator(chunk_size=500):
        normalized = _normalized_source(row.utm_source)
        if normalized is not None:
            utm_updates[row.pk] = normalized

    site_updates = {}
    for row in site_queryset.iterator(chunk_size=500):
        normalized = _normalized_first_touch(row.first_touch_data)
        if normalized is not None:
            site_updates[row.pk] = normalized

    order_updates = {}
    for row in order_queryset.iterator(chunk_size=500):
        normalized = _normalized_source(row.utm_source)
        if normalized is not None:
            order_updates[row.pk] = normalized

    linked_order_ids = []
    conflicts = []
    if utm_updates:
        linked_queryset = Order.objects.filter(utm_session_id__in=utm_updates).only(
            'id', 'utm_session_id', 'utm_source'
        ).order_by('pk')
        if lock:
            linked_queryset = linked_queryset.select_for_update()
        for order in linked_queryset.iterator(chunk_size=500):
            linked_order_ids.append(order.pk)
            if not order.utm_source:
                continue
            current_source = normalize_utm_source(order.utm_source)
            if current_source != utm_updates[order.utm_session_id]:
                conflicts.append(order.pk)

    return {
        'utm_updates': utm_updates,
        'site_updates': site_updates,
        'order_updates': order_updates,
        'linked_order_ids': linked_order_ids,
        'conflicts': conflicts,
    }


def _counts(plan):
    return {
        'utm_sessions': len(plan['utm_updates']),
        'site_sessions': len(plan['site_updates']),
        'orders': len(plan['order_updates']),
        'conflicts': len(plan['conflicts']),
    }


class Command(BaseCommand):
    help = (
        'Dry-run or normalize historical utm_source aliases across '
        'UTMSession, SiteSession first-touch data, and Order snapshots.'
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

    def _write_report(self, counts, *, dry_run, updated=None):
        parts = [f'{label}={value}' for label, value in counts.items()]
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

    def _validate_guards(self, counts, options):
        option_names = {
            'utm_sessions': 'expect_utm_sessions',
            'site_sessions': 'expect_site_sessions',
            'orders': 'expect_orders',
        }
        missing = [
            option_name
            for option_name in option_names.values()
            if options.get(option_name) is None
        ]
        if missing:
            flags = ', '.join(f"--{name.replace('_', '-')}" for name in missing)
            raise CommandError(f'Apply requires exact dry-run guards: {flags}')

        mismatches = [
            f"{label}: expected {options[option_name]}, found {counts[label]}"
            for label, option_name in option_names.items()
            if options[option_name] != counts[label]
        ]
        if mismatches:
            raise CommandError('Candidate counts changed; aborting: ' + '; '.join(mismatches))
        if counts['conflicts']:
            raise CommandError(
                f"Found {counts['conflicts']} linked order source conflicts; aborting"
            )

    def handle(self, *args, **options):
        if not options['apply']:
            self._write_report(_counts(_collect_plan()), dry_run=True)
            return

        with transaction.atomic():
            plan = _collect_plan(lock=True)
            counts = _counts(plan)
            self._validate_guards(counts, options)

            utm_rows = UTMSession.objects.in_bulk(plan['utm_updates'])
            site_rows = SiteSession.objects.in_bulk(plan['site_updates'])
            order_rows = Order.objects.in_bulk(plan['order_updates'])
            if (
                len(utm_rows) != counts['utm_sessions']
                or len(site_rows) != counts['site_sessions']
                or len(order_rows) != counts['orders']
            ):
                raise CommandError('Candidate rows changed while locking; aborting')

            for pk, source in plan['utm_updates'].items():
                row = utm_rows[pk]
                row.utm_source = source
                row.save(update_fields=['utm_source'])

            for pk, first_touch_data in plan['site_updates'].items():
                row = site_rows[pk]
                row.first_touch_data = first_touch_data
                row.save(update_fields=['first_touch_data'])

            for pk, source in plan['order_updates'].items():
                row = order_rows[pk]
                row.utm_source = source
                row.save(update_fields=['utm_source'])

            self._write_report(counts, dry_run=False, updated=counts)
