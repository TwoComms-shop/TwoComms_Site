"""
Django management команда для обновления статусов посылок
"""
import logging
from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone
from management.services.ig_task_health import task_heartbeat
from orders.nova_poshta_service import NovaPoshtaService

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Обновляет статусы посылок через API Новой Почты'

    def add_arguments(self, parser):
        parser.add_argument(
            '--order-number',
            type=str,
            help='Обновить статус конкретного заказа по номеру',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Показать что будет обновлено без фактического обновления',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Подробный вывод информации',
        )

    def handle(self, *args, **options):
        # Настраиваем логирование
        if options['verbose']:
            logging.basicConfig(level=logging.DEBUG)
        else:
            logging.basicConfig(level=logging.INFO)

        scheduled_batch = not options['order_number'] and not options['dry_run']
        heartbeat = (
            task_heartbeat('nova_poshta_tracking')
            if scheduled_batch
            else nullcontext()
        )
        with heartbeat as heartbeat_state:
            # Проверяем наличие API ключа
            if not getattr(settings, 'NOVA_POSHTA_API_KEY', ''):
                message = (
                    "NOVA_POSHTA_API_KEY не настроен в settings. "
                    "Добавьте ключ API Новой Почты в переменные окружения."
                )
                logger.error("NOVA_POSHTA_API_KEY not configured")
                raise CommandError(message)

            service = NovaPoshtaService()

            if options['order_number']:
                # Обновляем конкретный заказ
                self._update_single_order(service, options['order_number'], options['dry_run'])
            elif options['dry_run']:
                self._update_all_orders(service, True, heartbeat_state=heartbeat_state)
            else:
                self._update_all_orders(service, False, heartbeat_state=heartbeat_state)

    def _update_single_order(self, service, order_number, dry_run):
        """Обновляет статус одного заказа"""
        from orders.models import Order

        try:
            order = Order.objects.get(order_number=order_number)

            if not order.tracking_number:
                self.stdout.write(
                    self.style.WARNING(f"Заказ {order_number} не имеет ТТН")
                )
                logger.warning(f"Order {order_number} has no tracking number")
                return

            self.stdout.write(f"Обновление статуса для заказа {order_number}...")
            logger.info(f"Updating tracking status for order {order_number}")

            if dry_run:
                tracking_info = service.get_tracking_info(order.tracking_number)
                if tracking_info:
                    status = tracking_info.get('Status', '')
                    status_code = tracking_info.get('StatusCode')
                    status_desc = tracking_info.get('StatusDescription', '')

                    self.stdout.write(
                        f"  Текущий статус: {status} (код: {status_code})"
                    )
                    if status_desc:
                        self.stdout.write(f"  Описание: {status_desc}")

                    logger.info(
                        f"Order {order_number}: Status={status}, "
                        f"StatusCode={status_code}, Description={status_desc}"
                    )
                else:
                    self.stdout.write("Не удалось получить информацию о статусе")
                    logger.warning(f"Failed to get tracking info for order {order_number}")
            else:
                if service.update_order_tracking_status(order):
                    self.stdout.write(
                        self.style.SUCCESS(f"✅ Статус заказа {order_number} обновлен")
                    )
                    logger.info(f"Order {order_number} status updated successfully")
                else:
                    self.stdout.write(
                        self.style.WARNING(f"⚠ Статус заказа {order_number} не изменился")
                    )
                    logger.info(f"Order {order_number} status unchanged")

        except Order.DoesNotExist:
            self.stdout.write(
                self.style.ERROR(f"❌ Заказ {order_number} не найден")
            )
            logger.error(f"Order {order_number} not found")

    def _update_all_orders(self, service, dry_run, *, heartbeat_state=None):
        """Обновляет статусы всех заказов"""
        orders_with_ttn = service.get_orders_with_tracking_queryset()

        total_orders = orders_with_ttn.count()

        if total_orders == 0:
            if heartbeat_state is not None:
                heartbeat_state.mark_skipped()
            self.stdout.write("Нет заказов с ТТН для обновления")
            logger.info("No orders with tracking numbers to update")
            return

        self.stdout.write(f"Найдено {total_orders} заказов с ТТН")
        logger.info(f"Found {total_orders} orders with tracking numbers")

        if dry_run:
            self.stdout.write("DRY RUN - показываем что будет обновлено:")
            logger.info("Running in DRY RUN mode")

            for order in orders_with_ttn:
                tracking_info = service.get_tracking_info(order.tracking_number)
                if tracking_info:
                    status = tracking_info.get('Status', '')
                    status_code = tracking_info.get('StatusCode')
                    status_desc = tracking_info.get('StatusDescription', '')
                    current_status = order.shipment_status or 'Неизвестно'
                    new_status = f"{status} - {status_desc}" if status_desc else status

                    if current_status.strip() != new_status.strip():
                        self.stdout.write(
                            f"  {order.order_number}: {current_status} → {new_status} (код: {status_code})"
                        )
                        logger.debug(
                            f"Order {order.order_number} would be updated: "
                            f"{current_status} → {new_status}"
                        )
                    else:
                        self.stdout.write(
                            f"  {order.order_number}: статус не изменился ({current_status}, код: {status_code})"
                        )
                else:
                    self.stdout.write(f"  {order.order_number}: ошибка получения статуса")
                    logger.warning(f"Failed to get tracking info for order {order.order_number}")
        else:
            timestamp_start = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
            self.stdout.write(f"[{timestamp_start}] Начало обновления статусов...")
            logger.info("Starting tracking status update")

            result = service.update_all_tracking_statuses()

            timestamp_end = timezone.now().strftime('%Y-%m-%d %H:%M:%S')

            summary = (
                f"[{timestamp_end}] Обновление завершено:\n"
                f"  Всего заказов с ТТН: {result['total_orders']}\n"
                f"  Обработано: {result['processed']}\n"
                f"  Обновлено статусов: {result['updated']}\n"
                f"  Ошибок: {result['errors']}\n"
                f"  Временные ошибки провайдера: "
                f"{result.get('transient_provider_errors', result.get('provider_errors', 0))}\n"
                f"  Критические ошибки провайдера: {result.get('fatal_provider_errors', 0)}\n"
                f"  Ошибки строк: {result.get('row_errors', 0)}\n"
                f"  Ошибки приложения: {result.get('application_errors', 0)}"
            )

            if result['updated'] > 0:
                self.stdout.write(self.style.SUCCESS(summary))
                logger.info(
                    f"Update completed: {result['updated']}/{result['total_orders']} updated, "
                    f"{result['errors']} errors"
                )
            else:
                self.stdout.write(self.style.WARNING(summary))
                logger.warning(
                    f"Update completed with no updates: {result['total_orders']} total, "
                    f"{result['errors']} errors"
                )

            provider_errors = int(
                result.get('transient_provider_errors', result.get('provider_errors', 0)) or 0
            )
            fatal_provider_errors = int(result.get('fatal_provider_errors', 0) or 0)
            row_errors = int(result.get('row_errors', 0) or 0)
            application_errors = int(result.get('application_errors', 0) or 0)
            classified_errors = (
                provider_errors
                + fatal_provider_errors
                + application_errors
                + row_errors
            )
            unclassified_errors = max(0, int(result.get('errors', 0) or 0) - classified_errors)
            if fatal_provider_errors:
                raise CommandError(
                    "Nova Poshta tracking fatal provider batch completed with "
                    f"{fatal_provider_errors} error(s)"
                )
            if application_errors:
                raise CommandError(
                    "Nova Poshta tracking application batch completed with "
                    f"{application_errors} error(s)"
                )
            if unclassified_errors:
                raise CommandError(
                    "Nova Poshta tracking batch completed with "
                    f"{unclassified_errors} unclassified error(s)"
                )
            if provider_errors or row_errors:
                # Provider and per-order failures are deferred for retry. Keep
                # the cron run completed so heartbeat health can classify the
                # outcome as degraded and alert only after repeated failures.
                if heartbeat_state is not None:
                    heartbeat_state.mark_degraded("nova_poshta_provider_degraded")
                logger.warning(
                    "Nova Poshta tracking provider batch completed with %s "
                    "provider error(s) and %s row error(s); failed rows were "
                    "deferred for retry",
                    provider_errors,
                    row_errors,
                )
