"""Recover only deterministic historical Order attribution from fresh FBC evidence."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from orders.models import Order
from storefront.models import SiteSession, UTMSession, UserAction
from storefront.utm_utils import parse_fbc


ORDER_UTM_FIELDS = (
    'utm_source',
    'utm_medium',
    'utm_campaign',
    'utm_content',
    'utm_term',
)
SESSION_KEY_RE = re.compile(r'^[a-z0-9]{32}$')
CLOCK_SKEW = timedelta(minutes=5)
REPORT_FIELDS = (
    'scanned',
    'eligible',
    'linkable_groups',
    'linkable_orders',
    'stale',
    'no_key',
    'invalid',
    'conflicting',
    'create_sessions',
    'reuse_sessions',
)
GUARD_FIELDS = (
    ('expect_groups', 'linkable_groups'),
    ('expect_orders', 'linkable_orders'),
    ('expect_stale', 'stale'),
    ('expect_no_key', 'no_key'),
    ('expect_invalid', 'invalid'),
    ('expect_conflicting', 'conflicting'),
    ('expect_create_sessions', 'create_sessions'),
    ('expect_reuse_sessions', 'reuse_sessions'),
)


def _valid_session_key(value):
    return isinstance(value, str) and bool(SESSION_KEY_RE.fullmatch(value))


def _tracking(order):
    payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
    tracking = payload.get('tracking')
    return tracking if isinstance(tracking, dict) else {}


def _session_key_evidence(order, tracking):
    order_key = order.session_key
    valid_keys = set()
    invalid = False
    if order_key:
        if _valid_session_key(order_key):
            valid_keys.add(order_key)
        else:
            invalid = True

    external_id = tracking.get('external_id')
    if isinstance(external_id, str) and external_id.startswith('session:'):
        external_key = external_id[len('session:'):]
        if _valid_session_key(external_key):
            valid_keys.add(external_key)
        else:
            invalid = True

    if len(valid_keys) > 1:
        invalid = True
    resolved = next(iter(valid_keys)) if len(valid_keys) == 1 and not invalid else None
    return resolved, valid_keys, invalid


def _base_orders(*, lock):
    queryset = Order.objects.filter(source='web', utm_session__isnull=True)
    for field in ORDER_UTM_FIELDS:
        queryset = queryset.filter(Q(**{f'{field}__isnull': True}) | Q(**{field: ''}))
    queryset = queryset.only(
        'id',
        'source',
        'session_key',
        'payment_payload',
        'created',
        'utm_session',
        *ORDER_UTM_FIELDS,
    ).order_by('pk')
    if lock:
        queryset = queryset.select_for_update()
    return list(queryset)


def _is_utm_compatible(utm_session, *, key, site_session, fbc, fbclid):
    if utm_session.session_key != key:
        return False
    if utm_session.session_id not in (None, getattr(site_session, 'pk', None)):
        return False
    allowed = {
        'utm_source': 'facebook',
        'utm_medium': 'paid_social',
        'fbc': fbc,
        'fbclid': fbclid,
    }
    if any(
        str(getattr(utm_session, field) or '').strip()
        and getattr(utm_session, field) != expected
        for field, expected in allowed.items()
    ):
        return False
    return not any(
        str(getattr(utm_session, field) or '').strip()
        for field in ('utm_campaign', 'utm_content', 'utm_term', 'gclid', 'ttclid')
    )


def _has_linkage_conflict(actions, *, order_ids, site_session, utm_session):
    site_id = getattr(site_session, 'pk', None)
    utm_id = getattr(utm_session, 'pk', None)
    for action in actions:
        belongs_to_orders = action.order_id in order_ids
        belongs_to_site = site_id is not None and action.site_session_id == site_id
        belongs_to_utm = utm_id is not None and action.utm_session_id == utm_id
        if belongs_to_orders:
            if action.site_session_id is not None and action.site_session_id != site_id:
                return True
            if action.utm_session_id is not None and action.utm_session_id != utm_id:
                return True
        if belongs_to_site and action.utm_session_id is not None and action.utm_session_id != utm_id:
            return True
        if belongs_to_utm and action.site_session_id is not None and action.site_session_id != site_id:
            return True
    return False


def _durable_first_touch_is_compatible(payload, *, fbc, fbclid):
    if not payload:
        return True
    if not isinstance(payload, dict):
        return False

    source = payload.get('utm_source')
    medium = payload.get('utm_medium')
    has_source = bool(str(source or '').strip())
    has_medium = bool(str(medium or '').strip())
    if has_source or has_medium:
        if source != 'facebook' or medium != 'paid_social':
            return False

    if any(
        str(payload.get(field) or '').strip()
        for field in ('utm_campaign', 'utm_content', 'utm_term', 'gclid', 'ttclid')
    ):
        return False
    for field, expected in (('fbc', fbc), ('fbclid', fbclid)):
        value = payload.get(field)
        if str(value or '').strip() and value != expected:
            return False
    return True


def _has_durable_first_touch_conflict(
    actions,
    *,
    order_ids,
    site_session,
    utm_session,
    fbc,
    fbclid,
):
    if site_session is not None and not _durable_first_touch_is_compatible(
        site_session.first_touch_data,
        fbc=fbc,
        fbclid=fbclid,
    ):
        return True

    site_id = getattr(site_session, 'pk', None)
    utm_id = getattr(utm_session, 'pk', None)
    for action in actions:
        belongs_to_group = (
            action.order_id in order_ids
            or (site_id is not None and action.site_session_id == site_id)
            or (utm_id is not None and action.utm_session_id == utm_id)
        )
        if not belongs_to_group:
            continue
        metadata = action.metadata if isinstance(action.metadata, dict) else {}
        if 'first_touch' not in metadata:
            continue
        if not _durable_first_touch_is_compatible(
            metadata['first_touch'],
            fbc=fbc,
            fbclid=fbclid,
        ):
            return True
    return False


def _collect_plan(*, lock=False):
    report = {field: 0 for field in REPORT_FIELDS}
    orders = _base_orders(lock=lock)
    report['scanned'] = len(orders)
    window = timedelta(days=settings.META_FBC_ATTRIBUTION_WINDOW_DAYS)
    grouped = defaultdict(list)
    poisoned_keys = set()

    for order in orders:
        tracking = _tracking(order)
        key, evidence_keys, invalid_key = _session_key_evidence(order, tracking)
        fbc = tracking.get('fbc')
        parsed = parse_fbc(fbc)
        if parsed is None:
            report['invalid'] += 1
            poisoned_keys.update(evidence_keys)
            continue

        click_time = datetime.fromtimestamp(
            parsed.created_at_ms / 1000,
            tz=dt_timezone.utc,
        )
        if order.created.tzinfo is None:
            click_time = click_time.replace(tzinfo=None)
        age = order.created - click_time
        if age < -CLOCK_SKEW:
            report['invalid'] += 1
            poisoned_keys.update(evidence_keys)
            continue
        if age > window:
            report['stale'] += 1
            poisoned_keys.update(evidence_keys)
            continue

        report['eligible'] += 1
        if invalid_key:
            report['invalid'] += 1
            poisoned_keys.update(evidence_keys)
            continue
        if key is None:
            report['no_key'] += 1
            continue

        raw_fbclid = str(tracking.get('fbclid') or '').strip()
        raw_conflict = bool(
            (raw_fbclid and raw_fbclid != parsed.click_id)
            or str(tracking.get('gclid') or '').strip()
            or str(tracking.get('ttclid') or '').strip()
        )
        grouped[key].append(
            {
                'order': order,
                'fbc': fbc,
                'fbclid': parsed.click_id,
                'click_time': click_time,
                'raw_conflict': raw_conflict,
            }
        )

    keys = set(grouped)
    site_queryset = SiteSession.objects.filter(session_key__in=keys).order_by('pk')
    if lock:
        site_queryset = site_queryset.select_for_update()
    sites = list(site_queryset)
    sites_by_key = {site.session_key: site for site in sites}

    utm_queryset = UTMSession.objects.filter(
        Q(session_key__in=keys) | Q(session__session_key__in=keys)
    ).order_by('pk')
    if lock:
        utm_queryset = utm_queryset.select_for_update()
    utm_rows = list(utm_queryset)
    utms_by_key = defaultdict(list)
    for utm_session in utm_rows:
        if utm_session.session_key in keys:
            utms_by_key[utm_session.session_key].append(utm_session)
        if utm_session.session_id:
            site_key = utm_session.session.session_key
            if site_key in keys and utm_session not in utms_by_key[site_key]:
                utms_by_key[site_key].append(utm_session)

    order_ids = {
        item['order'].pk
        for group in grouped.values()
        for item in group
    }
    site_ids = [site.pk for site in sites]
    utm_ids = [utm_session.pk for utm_session in utm_rows]
    action_queryset = UserAction.objects.filter(
        Q(order_id__in=order_ids)
        | Q(site_session_id__in=site_ids)
        | Q(utm_session_id__in=utm_ids)
    ).only('id', 'order_id', 'site_session', 'utm_session', 'metadata').order_by('pk')
    if lock:
        action_queryset = action_queryset.select_for_update()
    actions = list(action_queryset)

    linked_order_queryset = Order.objects.filter(utm_session_id__in=utm_ids).only(
        'id',
        'utm_session',
    ).order_by('pk')
    if lock:
        linked_order_queryset = linked_order_queryset.select_for_update()
    used_utm_ids = {
        linked_order.utm_session_id
        for linked_order in linked_order_queryset
    }

    plan = []
    for key in sorted(grouped):
        group = grouped[key]
        evidence = {(item['fbc'], item['fbclid']) for item in group}
        site_session = sites_by_key.get(key)
        matching_utms = utms_by_key.get(key, [])
        existing_utm = matching_utms[0] if len(matching_utms) == 1 else None
        group_order_ids = {item['order'].pk for item in group}
        conflict = (
            key in poisoned_keys
            or any(item['raw_conflict'] for item in group)
            or len(evidence) != 1
            or len(matching_utms) > 1
            or (
                existing_utm is not None
                and existing_utm.pk in used_utm_ids
            )
        )
        if not conflict and existing_utm is not None:
            fbc, fbclid = next(iter(evidence))
            conflict = not _is_utm_compatible(
                existing_utm,
                key=key,
                site_session=site_session,
                fbc=fbc,
                fbclid=fbclid,
            )
        if not conflict:
            conflict = _has_linkage_conflict(
                actions,
                order_ids=group_order_ids,
                site_session=site_session,
                utm_session=existing_utm,
            )
        if not conflict:
            fbc, fbclid = next(iter(evidence))
            conflict = _has_durable_first_touch_conflict(
                actions,
                order_ids=group_order_ids,
                site_session=site_session,
                utm_session=existing_utm,
                fbc=fbc,
                fbclid=fbclid,
            )
        if conflict:
            report['conflicting'] += len(group)
            continue

        plan.append(
            {
                'key': key,
                'orders': [item['order'] for item in group],
                'site_session': site_session,
                'utm_session': existing_utm,
                'fbc': group[0]['fbc'],
                'fbclid': group[0]['fbclid'],
                'click_time': group[0]['click_time'],
            }
        )

    report['linkable_groups'] = len(plan)
    report['linkable_orders'] = sum(len(item['orders']) for item in plan)
    report['create_sessions'] = sum(item['utm_session'] is None for item in plan)
    report['reuse_sessions'] = sum(item['utm_session'] is not None for item in plan)
    return report, plan


def _validate_guards(report, options, *, require_all):
    if require_all:
        missing = [option for option, _report in GUARD_FIELDS if options.get(option) is None]
        if missing:
            flags = ', '.join(f"--{name.replace('_', '-')}" for name in missing)
            raise CommandError(f'--apply requires exact dry-run guards: {flags}')

    mismatches = []
    for option_name, report_name in GUARD_FIELDS:
        expected = options.get(option_name)
        if expected is not None and expected != report[report_name]:
            mismatches.append(
                f'{report_name}: expected {expected}, found {report[report_name]}'
            )
    if mismatches:
        raise CommandError('Candidate counts changed; aborting: ' + '; '.join(mismatches))


def _format_report(report, *, dry_run, updated_orders=0, created_sessions=0):
    parts = [f'{field}={report[field]}' for field in REPORT_FIELDS]
    parts.extend(
        (
            f'updated_orders={updated_orders}',
            f'created_sessions={created_sessions}',
            f'dry_run={dry_run}',
        )
    )
    return ' '.join(parts)


def _upsert_utm_session(item):
    utm_session = item['utm_session']
    created = utm_session is None
    if created:
        utm_session = UTMSession.objects.create(session_key=item['key'])

    values = {
        'session_id': getattr(item['site_session'], 'pk', None),
        'utm_source': 'facebook',
        'utm_medium': 'paid_social',
        'utm_campaign': None,
        'utm_content': None,
        'utm_term': None,
        'fbc': item['fbc'],
        'fbclid': item['fbclid'],
        'gclid': None,
        'ttclid': None,
    }
    if created:
        values.update(
            first_seen=item['click_time'],
            last_seen=item['click_time'],
        )
    UTMSession.objects.filter(pk=utm_session.pk).update(**values)
    utm_session.refresh_from_db()
    return utm_session, created


class Command(BaseCommand):
    help = (
        'Dry-run or recover historical fresh-FBC attribution for deterministic '
        'Order session groups. Never writes session credentials or analytics events.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist the guarded reconciliation. Default is read-only.',
        )
        for option_name, _report_name in GUARD_FIELDS:
            parser.add_argument(f"--{option_name.replace('_', '-')}", type=int)

    def handle(self, *args, **options):
        if not options['apply']:
            report, _plan = _collect_plan(lock=False)
            _validate_guards(report, options, require_all=False)
            self.stdout.write(_format_report(report, dry_run=True))
            return

        with transaction.atomic():
            report, plan = _collect_plan(lock=True)
            _validate_guards(report, options, require_all=True)

            orders = []
            created_sessions = 0
            for item in plan:
                utm_session, created = _upsert_utm_session(item)
                created_sessions += int(created)
                for order in item['orders']:
                    order.utm_session = utm_session
                    order.utm_source = 'facebook'
                    order.utm_medium = 'paid_social'
                    order.utm_campaign = None
                    order.utm_content = None
                    order.utm_term = None
                    orders.append(order)

            if orders:
                Order.objects.bulk_update(orders, ['utm_session', *ORDER_UTM_FIELDS])

        self.stdout.write(
            _format_report(
                report,
                dry_run=False,
                updated_orders=len(orders),
                created_sessions=created_sessions,
            )
        )
