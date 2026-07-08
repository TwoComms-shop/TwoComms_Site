"""
Middleware для резервного обновления статусов Nova Poshta

Этот middleware обеспечивает fallback механизм обновления статусов посылок
на случай если cron job не срабатывает.

Логика работы:
1. При каждом запросе проверяет, когда было последнее обновление
2. Если прошло больше 15 минут с последнего обновления, запускает обновление
3. Использует блокировку через кеш чтобы избежать дублирования запросов
4. Обновление выполняется в фоновом режиме (не блокирует запрос)
"""
import logging
import threading
from django.core.cache import cache
from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)


class NovaPoshtaFallbackMiddleware:
    """
    Middleware для резервного обновления статусов Nova Poshta

    Добавьте в MIDDLEWARE в settings.py:
    'orders.nova_poshta_middleware.NovaPoshtaFallbackMiddleware',
    """

    def __init__(self, get_response):
        self.get_response = get_response

        # Проверяем включен ли fallback через настройки
        self.enabled = getattr(
            settings,
            'NOVA_POSHTA_FALLBACK_ENABLED',
            True  # по умолчанию включен
        )

        # Настройки блокировки берем из сервиса (чтобы использовать единые ключи)
        try:
            from orders.nova_poshta_service import NovaPoshtaService
            self.lock_key = NovaPoshtaService.UPDATE_LOCK_CACHE_KEY
            self.lock_timeout = NovaPoshtaService.UPDATE_LOCK_TIMEOUT
        except Exception as e:
            logger.warning(
                f"Could not load NovaPoshtaService lock settings: {e}. Using defaults"
            )
            self.lock_key = 'nova_poshta_update_lock'
            self.lock_timeout = 5 * 60

        if self.enabled:
            logger.info("Nova Poshta fallback middleware enabled")
        else:
            logger.info("Nova Poshta fallback middleware disabled")

    def __call__(self, request):
        try:
            host = request.get_host().split(':')[0].lower()
        except Exception:
            host = ""

        # DTF subdomain does not require Nova Poshta storefront fallback checks.
        if host.startswith('dtf.'):
            return self.get_response(request)

        # Проверяем нужно ли запустить fallback обновление
        if self.enabled and self._should_trigger_update():
            self._trigger_update_async()

        response = self.get_response(request)
        return response

    def _should_trigger_update(self):
        """
        Проверяет нужно ли запустить обновление

        Returns:
            bool: True если нужно запустить обновление
        """
        try:
            from orders.nova_poshta_service import NovaPoshtaService

            # Проверяем нужно ли обновление
            if not NovaPoshtaService.should_trigger_fallback_update():
                return False

            # Проверяем не выполняется ли уже обновление
            if cache.get(self.lock_key):
                logger.debug("Nova Poshta update already in progress")
                return False

            return True

        except Exception as e:
            logger.error(f"Error checking if fallback update needed: {e}")
            return False

    def _trigger_update_async(self):
        """
        Запускает обновление статусов в отдельном потоке

        Это не блокирует текущий запрос
        """
        try:
            # Устанавливаем блокировку
            if not cache.add(self.lock_key, True, self.lock_timeout):
                # Блокировка уже установлена
                logger.debug("Could not acquire lock for Nova Poshta update")
                return

            logger.info("Triggering fallback Nova Poshta status update")

            # Запускаем обновление в отдельном потоке
            thread = threading.Thread(target=self._run_update, daemon=True)
            thread.start()

        except Exception as e:
            logger.error(f"Error triggering fallback update: {e}")
            # Снимаем блокировку в случае ошибки
            cache.delete(self.lock_key)

    def _run_update(self):
        """
        Выполняет обновление статусов

        Выполняется в отдельном потоке
        """
        try:
            from orders.nova_poshta_service import NovaPoshtaService

            # Поток живёт дольше HTTP-запроса: без этого MySQL-соединение
            # протухает и даёт "MySQL server has gone away" (2006).
            close_old_connections()

            logger.info("Starting fallback Nova Poshta status update")

            service = NovaPoshtaService()
            result = service.update_all_tracking_statuses()

            logger.info(
                f"Fallback update completed: "
                f"{result['updated']}/{result['total_orders']} updated, "
                f"{result['errors']} errors"
            )

        except Exception as e:
            logger.exception(f"Error in fallback Nova Poshta update: {e}")

        finally:
            close_old_connections()
            # Снимаем блокировку
            cache.delete(self.lock_key)
            logger.debug("Released lock for Nova Poshta update")


class NovaPoshtaFallbackSimpleMiddleware:
    """
    Упрощенная версия middleware для серверов без поддержки threading

    Эта версия запускает обновление синхронно, но только раз в N запросов
    чтобы не перегружать сервер
    """

    REQUEST_COUNTER_KEY = 'nova_poshta_request_counter'
    REQUESTS_THRESHOLD = 100  # Проверять каждые N запросов

    def __init__(self, get_response):
        self.get_response = get_response

        self.enabled = getattr(
            settings,
            'NOVA_POSHTA_FALLBACK_ENABLED',
            True
        )

        if self.enabled:
            logger.info("Nova Poshta fallback simple middleware enabled")

    def __call__(self, request):
        # Увеличиваем счетчик запросов
        if self.enabled:
            counter = cache.get(self.REQUEST_COUNTER_KEY, 0)
            counter += 1
            cache.set(self.REQUEST_COUNTER_KEY, counter, timeout=3600)

            # Проверяем только каждый N-й запрос
            if counter % self.REQUESTS_THRESHOLD == 0:
                self._check_and_update()

        response = self.get_response(request)
        return response

    def _check_and_update(self):
        """
        Проверяет и запускает обновление если нужно
        """
        try:
            from orders.nova_poshta_service import NovaPoshtaService

            # Проверяем нужно ли обновление
            if not NovaPoshtaService.should_trigger_fallback_update():
                return

            # Загружаем настройки из сервиса
            lock_key = NovaPoshtaService.UPDATE_LOCK_CACHE_KEY
            lock_timeout = NovaPoshtaService.UPDATE_LOCK_TIMEOUT

            # Проверяем блокировку
            if cache.get(lock_key):
                return

            # Устанавливаем блокировку
            if not cache.add(lock_key, True, lock_timeout):
                return

            logger.info("Triggering simple fallback Nova Poshta status update")

            try:
                close_old_connections()
                service = NovaPoshtaService()
                result = service.update_all_tracking_statuses()

                logger.info(
                    f"Simple fallback update completed: "
                    f"{result['updated']}/{result['total_orders']} updated, "
                    f"{result['errors']} errors"
                )
            finally:
                close_old_connections()
                cache.delete(lock_key)

        except Exception as e:
            logger.exception(f"Error in simple fallback update: {e}")
            # Пытаемся снять блокировку в случае ошибки
            try:
                from orders.nova_poshta_service import NovaPoshtaService
                cache.delete(NovaPoshtaService.UPDATE_LOCK_CACHE_KEY)
            except Exception:
                pass
