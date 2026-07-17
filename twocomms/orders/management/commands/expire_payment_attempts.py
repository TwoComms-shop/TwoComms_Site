from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta

from orders.models import PaymentAttempt


class Command(BaseCommand):
    help = 'Mark stale unpaid checkout attempts as expired without creating orders.'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=24)
        parser.add_argument('--limit', type=int, default=500)

    def handle(self, *args, **options):
        hours = max(1, options['hours'])
        limit = max(1, min(options['limit'], 5000))
        cutoff = timezone.now() - timedelta(hours=hours)
        ids = list(
            PaymentAttempt.objects.filter(
                status__in=(PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.PROCESSING),
                created__lt=cutoff,
            ).filter(
                Q(invoice_expires_at__lt=timezone.now())
                | Q(invoice_expires_at__isnull=True)
            ).order_by('pk').values_list('pk', flat=True)[:limit]
        )
        if not ids:
            self.stdout.write('No stale payment attempts found.')
            return
        updated = PaymentAttempt.objects.filter(
            pk__in=ids,
            status__in=(PaymentAttempt.Status.INITIATED, PaymentAttempt.Status.PROCESSING),
        ).update(
            status=PaymentAttempt.Status.EXPIRED,
            error_reason='invoice_expired',
            last_status_at=timezone.now(),
        )
        self.stdout.write(self.style.SUCCESS(f'Expired {updated} payment attempts.'))
