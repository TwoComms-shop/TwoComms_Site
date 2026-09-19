"""Read-only diagnostics for finance cron/durable-task coverage."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from finance.models import IntegrationConnection
from finance.models_settings import NotificationLog


class Command(BaseCommand):
    help = 'Показывает состояние последних финансовых уведомлений и синхронизаций'

    def handle(self, *args, **options):
        latest_notification = NotificationLog.objects.order_by('-created_at').first()
        integrations = IntegrationConnection.objects.filter(provider='monobank').exclude(status='disconnected')
        self.stdout.write(f'now={timezone.localtime().isoformat()}')
        if latest_notification:
            self.stdout.write(
                f'latest_notification={latest_notification.created_at.isoformat()} '
                f'type={latest_notification.notification_type} success={latest_notification.success}'
            )
        else:
            self.stdout.write('latest_notification=none')
        self.stdout.write(f'monobank_connections={integrations.count()}')
        self.stdout.write(f'monobank_auto_sync={integrations.filter(auto_sync=True).count()}')
        if not integrations.filter(auto_sync=True).exists():
            self.stdout.write(self.style.WARNING('Нет активных Monobank auto-sync подключений'))
