"""Recover only missing UTM -> SiteSession joins from exact session keys."""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from storefront.models import SiteSession, UTMSession


def _apply_link(utm_id, session_key, site_id):
    # Match SimpleAnalyticsMiddleware's lock order. The SiteSession lock also
    # serializes competing writers for its one-to-one attribution relation.
    with transaction.atomic():
        site = SiteSession.objects.select_for_update().filter(
            pk=site_id, session_key=session_key,
        ).first()
        if site is None:
            return False
        utm = UTMSession.objects.select_for_update().filter(
            pk=utm_id, session_key=session_key, session__isnull=True,
        ).first()
        if utm is None or UTMSession.objects.filter(session_id=site.pk).exists():
            return False
        return bool(UTMSession.objects.filter(
            pk=utm.pk, session__isnull=True, session_key=session_key,
        ).update(session_id=site.pk))


class Command(BaseCommand):
    help = (
        "Recover existing NULL UTMSession links by exact session_key only. "
        "Default is a read-only dry-run over the last seven days."
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Persist only unambiguous missing joins.')
        parser.add_argument('--days', type=int, default=7, help='UTM first_seen lookback in days (default: 7).')
        parser.add_argument('--batch-size', type=int, default=500, help='Rows per scan batch, 1-2000 (default: 500).')

    def handle(self, *args, **options):
        days = options['days']
        batch_size = options['batch_size']
        if days <= 0:
            raise CommandError('--days must be a positive integer')
        if not 1 <= batch_size <= 2000:
            raise CommandError('--batch-size must be between 1 and 2000')
        now = timezone.now()
        try:
            cutoff = now - timedelta(days=days)
        except OverflowError as exc:
            raise CommandError('--days exceeds the supported datetime range') from exc
        candidates = UTMSession.objects.filter(
            first_seen__gte=cutoff, first_seen__lte=now, session__isnull=True,
        )
        # Bound this invocation to the initial population; new traffic must not
        # extend a repair indefinitely. Keyset pages keep scan memory bounded.
        maximum_pk = candidates.order_by('-pk').values_list('pk', flat=True).first()
        counts = {
            'scanned': 0, 'recoverable': 0, 'missing_site_session': 0,
            'already_linked_site_session': 0, 'updated': 0, 'changed_before_apply': 0,
        }
        last_pk = 0
        while maximum_pk is not None:
            rows = list(candidates.filter(pk__gt=last_pk, pk__lte=maximum_pk)
                        .order_by('pk').values_list('pk', 'session_key')[:batch_size])
            if not rows:
                break
            last_pk = rows[-1][0]
            keys = {key for _, key in rows if key and key.strip()}
            sites = dict(SiteSession.objects.filter(session_key__in=keys)
                         .values_list('session_key', 'pk'))
            occupied = set(UTMSession.objects.filter(session_id__in=sites.values())
                           .values_list('session_id', flat=True))
            for utm_id, key in rows:
                counts['scanned'] += 1
                site_id = sites.get(key)
                if site_id is None:
                    counts['missing_site_session'] += 1
                    continue
                if site_id in occupied:
                    counts['already_linked_site_session'] += 1
                    continue
                counts['recoverable'] += 1
                if options['apply']:
                    if _apply_link(utm_id, key, site_id):
                        counts['updated'] += 1
                        occupied.add(site_id)
                    else:
                        counts['changed_before_apply'] += 1

        summary = ' '.join(f'{name}={value}' for name, value in counts.items())
        self.stdout.write(
            f'{summary} days={days} batch_size={batch_size} dry_run={not options["apply"]}'
        )
