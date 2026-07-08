"""
W2-10 / AN-051 / NEW-404: retention-чистка аналитических данных.

UTMSession и UserAction никогда не чистились — таблицы растут бесконечно
и содержат PII (IP, гео). Команда удаляет:
  - UserAction старше --actions-days (по умолчанию 365),
    КРОМЕ действий, связанных с заказами (purchase/lead с order_id);
  - UTMSession старше --sessions-days (по умолчанию 730),
    КРОМЕ конверсионных (is_converted=True);
  - orphan UserAction (DB-003): order_id указывает на несуществующий заказ;
  - CheckoutCapture старше --captures-days (по умолчанию 60) — W3-11/NEW-510:
    ФИО/телефон/email для abandoned-cart recovery, копились вечно.

Установка в crontab (см. docs/OPS.md):
  15 4 * * 0 cd ~/TWC/TwoComms_Site/twocomms && \
    ~/virtualenv/.../bin/python manage.py cleanup_analytics_data >> ~/logs/cleanup_analytics.log 2>&1

Всегда сначала прогоняйте с --dry-run на проде.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from storefront.models import UTMSession, UserAction

BATCH_SIZE = 5000


class Command(BaseCommand):
    help = 'Retention-чистка UTMSession/UserAction (AN-051) + orphan-очистка (DB-003)'

    def add_arguments(self, parser):
        parser.add_argument('--actions-days', type=int, default=365,
                            help='UserAction старше N дней удаляются (кроме order-связанных)')
        parser.add_argument('--sessions-days', type=int, default=730,
                            help='UTMSession старше N дней удаляются (кроме конверсионных)')
        parser.add_argument('--captures-days', type=int, default=60,
                            help='CheckoutCapture старше N дней удаляются (W3-11, PII retention)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Только посчитать, ничего не удалять')
        parser.add_argument('--skip-orphans', action='store_true',
                            help='Не чистить orphan UserAction.order_id')

    def handle(self, *args, **options):
        dry = options['dry_run']
        now = timezone.now()

        # 1. Старые UserAction (кроме связанных с заказами)
        actions_cutoff = now - timedelta(days=options['actions_days'])
        old_actions = UserAction.objects.filter(
            timestamp__lt=actions_cutoff,
        ).exclude(order_id__isnull=False)
        self._purge(old_actions, 'old UserAction', dry)

        # 2. Старые неконверсионные UTMSession
        # NB: у UTMSession нет created_at — используем last_seen
        # (неактивна дольше N дней = можно удалять)
        sessions_cutoff = now - timedelta(days=options['sessions_days'])
        old_sessions = UTMSession.objects.filter(
            last_seen__lt=sessions_cutoff,
            is_converted=False,
        )
        self._purge(old_sessions, 'old non-converted UTMSession', dry)

        # 3. Orphan UserAction.order_id (DB-003)
        if not options['skip_orphans']:
            from orders.models import Order
            existing_ids = set(Order.objects.values_list('id', flat=True))
            orphans = UserAction.objects.filter(order_id__isnull=False).exclude(
                order_id__in=existing_ids
            )
            self._purge(orphans, 'orphan UserAction (dead order_id)', dry)

        # 4. CheckoutCapture старше N дней (W3-11 / NEW-510, PII retention).
        # recover_checkouts работает с записями последних дней — 60 дней
        # хватает с запасом; конвертированные тоже чистим (данные уже в Order).
        from orders.models import CheckoutCapture
        captures_cutoff = now - timedelta(days=options['captures_days'])
        old_captures = CheckoutCapture.objects.filter(updated_at__lt=captures_cutoff)
        self._purge(old_captures, 'old CheckoutCapture (PII)', dry)

        self.stdout.write(self.style.SUCCESS('cleanup_analytics_data finished'))

    def _purge(self, queryset, label, dry):
        total = queryset.count()
        if dry:
            self.stdout.write(f'[dry-run] would delete {total} {label}')
            return
        deleted = 0
        # Батчами, чтобы не держать длинный lock на shared-хостинге
        while True:
            batch_ids = list(queryset.values_list('pk', flat=True)[:BATCH_SIZE])
            if not batch_ids:
                break
            count, _ = queryset.model.objects.filter(pk__in=batch_ids).delete()
            deleted += count
        self.stdout.write(f'deleted {deleted} {label} (estimated {total})')
