"""Recover deterministic historical click-ID attribution for user actions."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from storefront.models import SiteSession, UTMSession, UserAction
from storefront.utm_utils import CLICK_ID_ATTRIBUTION, infer_click_id_attribution


UTM_FIELDS = ('utm_campaign', 'utm_content', 'utm_term')
CLICK_ID_FIELDS = tuple(field for field, _source, _medium in CLICK_ID_ATTRIBUTION)


def _deterministic_attribution(payload):
    present = [
        field
        for field in CLICK_ID_FIELDS
        if str((payload or {}).get(field) or '').strip()
    ]
    if len(present) != 1:
        return None
    source, medium = infer_click_id_attribution(payload)
    return (source, medium) if source and medium else None


def _candidate_sites(*, lock=False):
    click_filter = Q()
    for field in CLICK_ID_FIELDS:
        click_filter |= Q(**{f'first_touch_data__{field}__isnull': False})
    queryset = SiteSession.objects.filter(click_filter).order_by('pk')
    if lock:
        queryset = queryset.select_for_update()
    return list(queryset)


def _collect_plan(*, lock=False):
    sites = _candidate_sites(lock=lock)
    site_ids = [site.pk for site in sites]
    session_keys = [site.session_key for site in sites]

    utm_queryset = UTMSession.objects.filter(
        Q(session_id__in=site_ids) | Q(session_key__in=session_keys)
    ).order_by('pk')
    if lock:
        utm_queryset = utm_queryset.select_for_update()
    utm_rows = list(utm_queryset)
    utm_by_site = {row.session_id: row for row in utm_rows if row.session_id}
    utm_by_key = {row.session_key: row for row in utm_rows}

    action_queryset = UserAction.objects.filter(
        site_session_id__in=site_ids,
        utm_session__isnull=True,
    ).order_by('pk')
    if lock:
        action_queryset = action_queryset.select_for_update()
    actions_by_site = {}
    for action in action_queryset:
        actions_by_site.setdefault(action.site_session_id, []).append(action)

    plan = []
    ambiguous_sessions = 0
    for site in sites:
        payload = site.first_touch_data if isinstance(site.first_touch_data, dict) else {}
        attribution = _deterministic_attribution(payload)
        if attribution is None:
            ambiguous_sessions += 1
            continue

        actions = actions_by_site.get(site.pk, [])
        if not actions:
            continue

        relation_match = utm_by_site.get(site.pk)
        key_match = utm_by_key.get(site.session_key)
        if relation_match and key_match and relation_match.pk != key_match.pk:
            ambiguous_sessions += 1
            continue
        existing = relation_match or key_match
        if existing and existing.session_id not in (None, site.pk):
            ambiguous_sessions += 1
            continue

        plan.append(
            {
                'site': site,
                'utm_session': existing,
                'actions': actions,
                'source': attribution[0],
                'medium': attribution[1],
                'payload': payload,
            }
        )

    return {'items': plan, 'ambiguous_sessions': ambiguous_sessions}


def _counts(plan):
    items = plan['items']
    return {
        'sessions': len(items),
        'actions': sum(len(item['actions']) for item in items),
        'create_sessions': sum(item['utm_session'] is None for item in items),
        'reuse_sessions': sum(item['utm_session'] is not None for item in items),
        'ambiguous_sessions': plan['ambiguous_sessions'],
    }


def _new_utm_session(item):
    site = item['site']
    payload = item['payload']
    values = {
        'session': site,
        'session_key': site.session_key,
        'visitor_id': site.visitor_id,
        'utm_source': item['source'],
        'utm_medium': item['medium'],
        'ip_address': site.ip_address,
        'user_agent': site.user_agent,
        'referrer': str(payload.get('referrer') or '')[:512] or None,
        'landing_page': str(payload.get('landing_path') or '')[:512] or None,
    }
    for field in UTM_FIELDS + CLICK_ID_FIELDS:
        values[field] = str(payload.get(field) or '')[:255] or None
    return UTMSession.objects.create(**values)


class Command(BaseCommand):
    help = (
        'Dry-run or recover UTMSession links for historical SiteSession rows '
        'with exactly one supported advertising click ID.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Persist the guarded backfill. Default is read-only.',
        )
        parser.add_argument('--expect-sessions', type=int)
        parser.add_argument('--expect-actions', type=int)

    def _write_report(self, counts, *, dry_run, updated=None):
        parts = [f'{label}={value}' for label, value in counts.items()]
        if updated is not None:
            parts.extend(
                [
                    f"updated_sessions={updated['sessions']}",
                    f"updated_actions={updated['actions']}",
                ]
            )
        parts.append(f'dry_run={dry_run}')
        self.stdout.write(' '.join(parts))

    def _validate_guards(self, counts, options):
        missing = [
            name
            for name in ('expect_sessions', 'expect_actions')
            if options.get(name) is None
        ]
        if missing:
            flags = ', '.join(f"--{name.replace('_', '-')}" for name in missing)
            raise CommandError(f'Apply requires exact dry-run guards: {flags}')

        mismatches = []
        for label, option_name in (
            ('sessions', 'expect_sessions'),
            ('actions', 'expect_actions'),
        ):
            if counts[label] != options[option_name]:
                mismatches.append(
                    f"{label}: expected {options[option_name]}, found {counts[label]}"
                )
        if mismatches:
            raise CommandError('Candidate counts changed; aborting: ' + '; '.join(mismatches))

    def handle(self, *args, **options):
        if not options['apply']:
            self._write_report(_counts(_collect_plan()), dry_run=True)
            return

        with transaction.atomic():
            plan = _collect_plan(lock=True)
            counts = _counts(plan)
            self._validate_guards(counts, options)

            actions = []
            for item in plan['items']:
                utm_session = item['utm_session']
                if utm_session is None:
                    utm_session = _new_utm_session(item)
                elif utm_session.session_id is None:
                    utm_session.session = item['site']
                    utm_session.save(update_fields=['session'])

                for action in item['actions']:
                    action.utm_session = utm_session
                    actions.append(action)

            if actions:
                UserAction.objects.bulk_update(actions, ['utm_session'])

            self._write_report(
                counts,
                dry_run=False,
                updated={'sessions': len(plan['items']), 'actions': len(actions)},
            )
