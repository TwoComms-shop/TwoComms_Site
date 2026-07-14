"""Safely recover only provable historical Order -> UTM attribution.

The command is intentionally conservative.  A historical session key is also
an order-access credential, so this reconciliation never writes
``Order.session_key`` and never creates sessions or analytics events.
"""

from collections import defaultdict
from datetime import timedelta
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from orders.models import Order
from storefront.models import UTMSession, UserAction


ORDER_UTM_FIELDS = (
    'utm_source',
    'utm_medium',
    'utm_campaign',
    'utm_content',
    'utm_term',
)
CONVERSION_ACTION_TYPES = frozenset({'lead', 'purchase'})
SESSION_KEY_RE = re.compile(r'^[a-z0-9]{32}$')


def _has_attribution(order):
    return bool(
        order.utm_session_id
        or any(getattr(order, field) for field in ORDER_UTM_FIELDS)
    )


def _valid_session_key(value):
    return isinstance(value, str) and bool(SESSION_KEY_RE.fullmatch(value))


def _external_session_evidence(order):
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    tracking = payload.get('tracking')
    tracking = tracking if isinstance(tracking, dict) else {}
    external_id = tracking.get('external_id')
    if not isinstance(external_id, str) or not external_id.startswith('session:'):
        return None, False
    key = external_id[len('session:'):]
    return (key, False) if _valid_session_key(key) else (None, True)


def _strongest_conversion(actions):
    purchases = [action for action in actions if action.action_type == 'purchase']
    if purchases:
        return 'purchase', min(action.timestamp for action in purchases)
    leads = [action for action in actions if action.action_type == 'lead']
    if leads:
        return 'lead', min(action.timestamp for action in leads)
    return None, None


def _conversion_needs_update(utm_session, conversion_type, converted_at):
    if conversion_type == 'purchase':
        return (
            not utm_session.is_converted
            or utm_session.conversion_type != 'purchase'
            or utm_session.converted_at != converted_at
        )
    if conversion_type == 'lead':
        if utm_session.conversion_type == 'purchase':
            return False
        return (
            not utm_session.is_converted
            or utm_session.conversion_type != 'lead'
            or utm_session.converted_at != converted_at
        )
    return False


def _scan(*, lock=False):
    report = {
        'scanned': 0,
        'already_attributed': 0,
        'recoverable_orders': 0,
        'recoverable_actions': 0,
        'recoverable_conversions': 0,
        'no_evidence': 0,
        'invalid_evidence': 0,
        'missing_utm': 0,
        'outside_window': 0,
        'ambiguous': 0,
    }

    order_qs = Order.objects.filter(source='web').only(
        'id',
        'source',
        'session_key',
        'payment_payload',
        'created',
        'utm_session',
        *ORDER_UTM_FIELDS,
    ).order_by('pk')
    if lock:
        order_qs = order_qs.select_for_update()
    orders = list(order_qs)
    report['scanned'] = len(orders)

    evidence = []
    keys = set()
    for order in orders:
        if _has_attribution(order):
            report['already_attributed'] += 1
            continue

        order_key = order.session_key
        if order_key and not _valid_session_key(order_key):
            report['invalid_evidence'] += 1
            continue
        external_key, invalid_external = _external_session_evidence(order)
        if invalid_external:
            report['invalid_evidence'] += 1
            continue
        if order_key and external_key and order_key != external_key:
            report['ambiguous'] += 1
            continue

        key = order_key or external_key
        if not key:
            report['no_evidence'] += 1
            continue
        evidence.append((order, key))
        keys.add(key)

    utm_qs = UTMSession.objects.filter(session_key__in=keys)
    if lock:
        utm_qs = utm_qs.select_for_update()
    utm_by_key = {session.session_key: session for session in utm_qs}

    max_age = timedelta(seconds=int(settings.SESSION_COOKIE_AGE))
    provisional = []
    for order, key in evidence:
        utm_session = utm_by_key.get(key)
        if utm_session is None or not str(utm_session.utm_source or '').strip():
            report['missing_utm'] += 1
            continue
        if (
            utm_session.first_seen > order.created
            or order.created - utm_session.first_seen > max_age
        ):
            report['outside_window'] += 1
            continue
        provisional.append({'order': order, 'utm_session': utm_session})

    provisional_order_ids = [item['order'].pk for item in provisional]
    target_utm_ids = {item['utm_session'].pk for item in provisional}
    action_qs = UserAction.objects.filter(
        Q(order_id__in=provisional_order_ids) | Q(utm_session_id__in=target_utm_ids),
        action_type__in=CONVERSION_ACTION_TYPES,
    ).only(
        'id',
        'order_id',
        'action_type',
        'timestamp',
        'utm_session',
        'site_session',
    )
    if lock:
        action_qs = action_qs.select_for_update()
    actions_by_order = defaultdict(list)
    actions_by_utm = defaultdict(list)
    for action in action_qs.order_by('pk'):
        if action.order_id in provisional_order_ids:
            actions_by_order[action.order_id].append(action)
        if action.utm_session_id in target_utm_ids:
            actions_by_utm[action.utm_session_id].append(action)

    recoverable = []
    conversion_actions = defaultdict(dict)
    target_sessions = {}
    for item in provisional:
        order = item['order']
        utm_session = item['utm_session']
        actions = actions_by_order.get(order.pk, [])
        if any(
            (
                action.utm_session_id is not None
                and action.utm_session_id != utm_session.pk
            )
            or (
                action.site_session_id is not None
                and action.site_session_id != utm_session.session_id
            )
            for action in actions
        ):
            report['ambiguous'] += 1
            continue

        actions_to_link = [action for action in actions if action.utm_session_id is None]
        recoverable.append(
            {
                'order': order,
                'utm_session': utm_session,
                'actions': actions_to_link,
                'all_actions': actions,
            }
        )
        report['recoverable_orders'] += 1
        report['recoverable_actions'] += len(actions_to_link)

        target_sessions[utm_session.pk] = utm_session
        for action in actions_by_utm.get(utm_session.pk, []):
            conversion_actions[utm_session.pk][action.pk] = action
        for action in actions:
            conversion_actions[utm_session.pk][action.pk] = action

    conversion_plans = []
    for utm_session_id in sorted(target_sessions):
        utm_session = target_sessions[utm_session_id]
        conversion_type, converted_at = _strongest_conversion(
            conversion_actions[utm_session_id].values()
        )
        if _conversion_needs_update(utm_session, conversion_type, converted_at):
            conversion_plans.append(
                {
                    'utm_session': utm_session,
                    'conversion_type': conversion_type,
                    'converted_at': converted_at,
                }
            )
    report['recoverable_conversions'] = len(conversion_plans)

    return report, recoverable, conversion_plans


def _action_state(actions):
    return sorted(
        (
            action.pk,
            action.order_id,
            action.action_type,
            action.utm_session_id,
            action.site_session_id,
        )
        for action in actions
    )


def _validate_expected_counts(report, options):
    expected_fields = (
        ('expect_orders', 'recoverable_orders'),
        ('expect_actions', 'recoverable_actions'),
        ('expect_conversions', 'recoverable_conversions'),
    )
    for option_name, report_name in expected_fields:
        expected = options.get(option_name)
        if expected is not None and expected != report[report_name]:
            raise CommandError(
                f'{report_name} drifted: expected={expected} actual={report[report_name]}'
            )


def _format_report(report, *, updated, dry_run):
    ordered_fields = (
        'scanned',
        'already_attributed',
        'recoverable_orders',
        'recoverable_actions',
        'recoverable_conversions',
        'no_evidence',
        'invalid_evidence',
        'missing_utm',
        'outside_window',
        'ambiguous',
    )
    parts = [f'{field}={report[field]}' for field in ordered_fields]
    parts.extend(
        [
            f'updated_orders={updated["orders"]}',
            f'updated_actions={updated["actions"]}',
            f'updated_conversions={updated["conversions"]}',
            f'dry_run={dry_run}',
        ]
    )
    return ' '.join(parts)


class Command(BaseCommand):
    help = (
        'Dry-run or recover provable historical Order UTM attribution. '
        'Never writes Order.session_key and never creates sessions or events.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist the exact matches. Default is read-only dry-run.',
        )
        parser.add_argument('--expect-orders', type=int)
        parser.add_argument('--expect-actions', type=int)
        parser.add_argument('--expect-conversions', type=int)

    def handle(self, *args, **options):
        apply_changes = bool(options['apply'])
        updated = {'orders': 0, 'actions': 0, 'conversions': 0}

        if not apply_changes:
            report, _recoverable, _conversion_plans = _scan(lock=False)
            _validate_expected_counts(report, options)
            self.stdout.write(
                _format_report(report, updated=updated, dry_run=True)
            )
            return

        missing_guards = [
            name
            for name in ('expect_orders', 'expect_actions', 'expect_conversions')
            if options.get(name) is None
        ]
        if missing_guards:
            flags = ', '.join(f'--{name.replace("_", "-")}' for name in missing_guards)
            raise CommandError(f'--apply requires drift guards: {flags}')

        with transaction.atomic():
            report, recoverable, conversion_plans = _scan(lock=True)
            if report['ambiguous']:
                raise CommandError(
                    f'Found {report["ambiguous"]} ambiguous attribution row(s); '
                    'no changes were applied.'
                )
            _validate_expected_counts(report, options)

            planned_actions = [
                action
                for item in recoverable
                for action in item['all_actions']
            ]
            for item in recoverable:
                order = item['order']
                utm_session = item['utm_session']
                order.utm_session = utm_session
                for field in ORDER_UTM_FIELDS:
                    setattr(order, field, getattr(utm_session, field))
                order.save(update_fields=['utm_session', *ORDER_UTM_FIELDS])
                updated['orders'] += 1

            # Re-lock and compare after the Order writes.  Any action drift
            # observed inside this finite historical cohort aborts and rolls
            # back the Order updates above.
            recoverable_order_ids = [item['order'].pk for item in recoverable]
            current_actions = list(
                UserAction.objects.select_for_update()
                .filter(
                    order_id__in=recoverable_order_ids,
                    action_type__in=CONVERSION_ACTION_TYPES,
                )
                .only(
                    'id',
                    'order_id',
                    'action_type',
                    'utm_session',
                    'site_session',
                )
                .order_by('pk')
            )
            if _action_state(current_actions) != _action_state(planned_actions):
                raise CommandError(
                    'Conversion actions drifted during reconciliation; '
                    'all changes were rolled back.'
                )

            target_by_order = {
                item['order'].pk: item['utm_session']
                for item in recoverable
            }
            for action in current_actions:
                utm_session = target_by_order[action.order_id]
                if action.utm_session_id is None:
                    action.utm_session = utm_session
                    action.save(update_fields=['utm_session'])
                    updated['actions'] += 1

            for plan in conversion_plans:
                utm_session = plan['utm_session']
                utm_session.is_converted = True
                utm_session.conversion_type = plan['conversion_type']
                utm_session.converted_at = plan['converted_at']
                utm_session.save(
                    update_fields=[
                        'is_converted',
                        'conversion_type',
                        'converted_at',
                    ]
                )
                updated['conversions'] += 1

        self.stdout.write(
            _format_report(report, updated=updated, dry_run=False)
        )
